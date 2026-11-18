"""HEARTH hub: collects its own node, accepts LAN agents, stores history, serves the UI."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .collect import EVENTS, HISTORY, INTERVAL, NODE_ID, Collector, node_id
from .store import RANGES, Store

log = logging.getLogger("hearth")

TOKEN = os.getenv("HEARTH_TOKEN", "").strip()
DB_PATH = os.getenv("HEARTH_DB", "/data/hearth.db")
RETENTION_DAYS = int(os.getenv("HEARTH_RETENTION_DAYS", "30"))
LOST_AFTER = float(os.getenv("HEARTH_LOST_AFTER", "15"))
DISK_SAMPLE_EVERY = 600
STATIC = Path(__file__).resolve().parent.parent / "static"
# Agents are LAN-only: RFC1918, loopback, link-local and IPv6 ULA. Override with HEARTH_AGENT_CIDRS.
LAN_CIDRS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
             "::1/128", "fc00::/7", "fe80::/10")
AGENT_CIDRS = [ipaddress.ip_network(c.strip(), strict=False)
               for c in (os.getenv("HEARTH_AGENT_CIDRS") or ",".join(LAN_CIDRS)).split(",") if c.strip()]


def lan_allowed(host: str | None) -> bool:
    try:
        a = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return any(a.version == n.version and a in n for n in AGENT_CIDRS)


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


class MinuteAcc:
    """Averages 1 Hz snapshots into one row per minute for the history DB."""

    def __init__(self) -> None:
        self.minute: int | None = None
        self._reset()

    def _reset(self) -> None:
        self.n, self.cpu, self.cpu_max, self.mem, self.rx, self.tx = 0, 0.0, 0.0, 0.0, 0.0, 0.0
        self.hot: float | None = None

    def add(self, snap: dict) -> tuple | None:
        m = int(time.time() // 60)
        row = None
        if self.minute is not None and m != self.minute and self.n:
            n = self.n
            row = (self.minute * 60, round(self.cpu / n, 2), self.cpu_max, round(self.mem / n, 2),
                   self.hot, round(self.rx / n), round(self.tx / n))
        if m != self.minute:
            self.minute = m
            self._reset()
        net = snap["net"]
        prim = next((i for i in net["ifaces"] if i["name"] == net.get("primary")), None) or \
            (net["ifaces"][0] if net["ifaces"] else None)
        self.n += 1
        self.cpu += snap["cpu"]["total"]
        self.cpu_max = max(self.cpu_max, snap["cpu"]["total"])
        self.mem += snap["mem"]["pct"]
        if snap.get("hot"):
            self.hot = max(self.hot or 0.0, snap["hot"]["c"])
        if prim:
            self.rx += prim["rx"]
            self.tx += prim["tx"]
        return row


class NodeState:
    def __init__(self, nid: str, local: bool = False) -> None:
        self.id = nid
        self.local = local
        self.latest: dict | None = None
        self.last_seen = 0.0
        self.online = False
        self.ever_lost = False
        self.addr: str | None = None
        self.boot: str | None = None
        self.last_ev = 0
        self.acc = MinuteAcc()
        self.disk_at = 0.0
        self.forecast: dict = {}
        self.cpu: deque = deque(maxlen=HISTORY)
        self.mem: deque = deque(maxlen=HISTORY)
        self.hot: deque = deque(maxlen=HISTORY)
        self.net: dict[str, tuple[deque, deque]] = {}
        self.temp: dict[str, deque] = {}

    def record(self, s: dict) -> None:
        self.cpu.append(s["cpu"]["total"])
        self.mem.append(s["mem"]["pct"])
        self.hot.append(s["hot"]["c"] if s.get("hot") else None)
        names = set()
        for i in s["net"]["ifaces"]:
            h = self.net.setdefault(i["name"], (deque(maxlen=HISTORY), deque(maxlen=HISTORY)))
            h[0].append(i["rx"])
            h[1].append(i["tx"])
            names.add(i["name"])
        for gone in set(self.net) - names:
            del self.net[gone]
        ids = set()
        for t in s["temps"]:
            self.temp.setdefault(t["id"], deque(maxlen=HISTORY)).append(t["c"])
            ids.add(t["id"])
        for gone in set(self.temp) - ids:
            del self.temp[gone]

    def history(self) -> dict:
        return {"cpu": list(self.cpu), "mem": list(self.mem), "hot": list(self.hot),
                "net": {k: {"rx": list(v[0]), "tx": list(v[1])} for k, v in self.net.items()},
                "temp": {k: list(v) for k, v in self.temp.items()}}

    def summary(self) -> dict:
        s = self.latest or {}
        dk = s.get("docker") or {}
        bad = sum(1 for c in dk.get("containers", []) if c.get("health") == "unhealthy" or c.get("state") in ("restarting", "dead"))
        alerts = sum(1 for t in s.get("temps", []) if t["lvl"] != "ok") + bad + \
            sum(1 for d in (s.get("smart") or {}).get("drives", []) if d.get("lvl") in ("warn", "crit"))
        host = s.get("host") or {}
        return {
            "id": self.id, "name": host.get("name", self.id), "local": self.local, "online": self.online,
            "addr": self.addr, "arch": host.get("arch"), "age": round(time.monotonic() - self.last_seen) if self.last_seen else None,
            "cpu": (s.get("cpu") or {}).get("total"), "mem": (s.get("mem") or {}).get("pct"),
            "hot": (s.get("hot") or {}).get("c"), "running": dk.get("running"), "total": dk.get("total"),
            "alerts": alerts,
        }


class Hub:
    def __init__(self) -> None:
        self.collector = Collector()
        self.local = NodeState(NODE_ID, local=True)
        self.local.online = True
        self.nodes: dict[str, NodeState] = {NODE_ID: self.local}
        self.clients: dict[WebSocket, str] = {}
        self.agents: dict[str, WebSocket] = {}
        self.store: Store | None = None
        try:
            self.store = Store(DB_PATH)
            EVENTS.seed(self.store.recent_events(100))
            EVENTS.listeners.append(self._persist_event)
        except Exception as e:  # noqa: BLE001
            log.warning("history store disabled (%s): %s", DB_PATH, e)
        self._pruned = 0.0

    def _persist_event(self, e: dict) -> None:
        if self.store:
            self.store.add_event(e)

    def resolve(self, nid: str | None) -> str:
        return nid if nid in self.nodes else self.local.id

    def fleet(self) -> list[dict]:
        return [n.summary() for n in sorted(self.nodes.values(), key=lambda n: (not n.local, n.id))]

    # ── ingest ──
    async def ingest(self, node: NodeState, snap: dict) -> None:
        if not node.local:
            self._ingest_events(node, snap)
            if not node.online:
                node.online = True
                if node.ever_lost:
                    EVENTS.add("ok", "fleet", f"{node.id} back online", node=node.id, rec=True)
        snap.pop("events", None)
        node.latest = snap
        node.last_seen = time.monotonic()
        node.record(snap)
        await asyncio.to_thread(self._persist, node, snap)
        for d in snap.get("disks", []):
            d["forecast"] = node.forecast.get(d["mount"])
        msg = dumps({"type": "tick", "node": node.id, "data": snap})
        await asyncio.gather(*(self._send(ws, msg) for ws, sel in list(self.clients.items()) if sel == node.id))

    def _ingest_events(self, node: NodeState, snap: dict) -> None:
        evs = snap.get("events") or []
        top = max((e.get("id", 0) for e in evs), default=0)
        if snap.get("boot") != node.boot:
            first_contact = node.boot is None
            node.boot, node.last_ev = snap.get("boot"), 0
            if first_contact:  # don't replay an agent's backlog on hub start
                node.last_ev = top
        for e in evs:
            if e.get("id", 0) > node.last_ev and e.get("lvl") in ("ok", "info", "warn", "crit"):
                EVENTS.add(e["lvl"], str(e.get("src", "?"))[:16], str(e.get("msg", ""))[:300], node=node.id,
                           rec=bool(e.get("rec")))
        node.last_ev = max(node.last_ev, top)

    def _persist(self, node: NodeState, snap: dict) -> None:
        if not self.store:
            return
        try:
            row = node.acc.add(snap)
            if row:
                self.store.add_sample(node.id, row)
            now = time.monotonic()
            if snap.get("disks") and now - node.disk_at >= DISK_SAMPLE_EVERY:
                node.disk_at = now
                self.store.add_disks(node.id, int(time.time()), snap["disks"])
                node.forecast = self.store.forecast(node.id)
            if now - self._pruned > 3600:
                self._pruned = now
                self.store.prune(RETENTION_DAYS)
        except Exception:  # noqa: BLE001
            log.exception("persist failed for %s", node.id)

    def _check_lost(self) -> None:
        now = time.monotonic()
        for n in self.nodes.values():
            if not n.local and n.online and now - n.last_seen > LOST_AFTER:
                n.online = False
                n.ever_lost = True
                EVENTS.add("crit", "fleet", f"{n.id} signal lost", node=n.id)

    # ── loop ──
    async def run(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                snap = await asyncio.to_thread(self.collector.collect)
                await self.ingest(self.local, snap)
                self._check_lost()
                msg = dumps({"type": "fleet", "nodes": self.fleet(), "events": EVENTS.recent(60)})
                await asyncio.gather(*(self._send(ws, msg) for ws in list(self.clients)))
            except Exception:  # noqa: BLE001
                log.exception("hub tick failed")
            await asyncio.sleep(max(0.05, INTERVAL - (time.monotonic() - t0)))

    async def hello(self, ws: WebSocket, nid: str) -> None:
        node = self.nodes[nid]
        data = node.latest
        if data:
            for d in data.get("disks", []):
                d["forecast"] = node.forecast.get(d["mount"])
        await ws.send_text(dumps({"type": "hello", "node": nid, "history": node.history(), "data": data,
                                  "fleet": self.fleet(), "events": EVENTS.recent(80),
                                  "store": self.store is not None}))

    async def _send(self, ws: WebSocket, msg: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(msg), timeout=3)
        except Exception:  # noqa: BLE001
            self.clients.pop(ws, None)


hub = Hub()


@asynccontextmanager
async def lifespan(_: FastAPI):
    EVENTS.add("ok", "hearth", f"hub online — node {NODE_ID}")
    if not TOKEN:
        log.info("HEARTH_TOKEN not set: agent connections are disabled")
    hub.collector.start()
    task = asyncio.create_task(hub.run())
    yield
    task.cancel()


app = FastAPI(title="HEARTH", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def revalidate_assets(request, call_next):
    """Make browsers revalidate (cheap 304 via ETag) so an upgraded HEARTH never runs with stale JS/CSS."""
    resp = await call_next(request)
    resp.headers.setdefault("Cache-Control", "no-cache")
    return resp


@app.get("/healthz")
async def healthz():
    return {"ok": hub.local.latest is not None, "mode": "hub", "nodes": len(hub.nodes)}


@app.get("/api/snapshot")
async def api_snapshot(node: str | None = None):
    return JSONResponse(hub.nodes[hub.resolve(node)].latest or {})


@app.get("/api/fleet")
async def api_fleet():
    return hub.fleet()


@app.get("/api/history")
async def api_history(node: str | None = None, range: str = Query("1h")):  # noqa: A002
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    if not hub.store:
        raise HTTPException(503, "history store unavailable")
    return await asyncio.to_thread(hub.store.history, hub.resolve(node), range)


@app.get("/api/summary")
async def api_summary(node: str | None = None, hours: int = Query(24, ge=1, le=720)):
    if not hub.store:
        raise HTTPException(503, "history store unavailable")
    return await asyncio.to_thread(hub.store.summary, hub.resolve(node), hours)


@app.websocket("/ws")
async def ws_browser(ws: WebSocket):
    await ws.accept()
    sel = hub.resolve(ws.query_params.get("node"))
    await hub.hello(ws, sel)
    hub.clients[ws] = sel
    try:
        while True:
            try:
                m = json.loads(await ws.receive_text())
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("type") == "select":
                sel = hub.resolve(str(m.get("node")))
                await hub.hello(ws, sel)
                hub.clients[ws] = sel
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(ws, None)


@app.websocket("/agent")
async def ws_agent(ws: WebSocket):
    host = ws.client.host if ws.client else None
    if not TOKEN:
        await ws.close(code=1008)
        return
    if not lan_allowed(host):
        EVENTS.add("warn", "fleet", f"refused agent from non-LAN address {host}")
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
    except Exception:  # noqa: BLE001
        await ws.close(code=1008)
        return
    if not (isinstance(first, dict) and first.get("type") == "auth"
            and hmac.compare_digest(str(first.get("token", "")).encode(), TOKEN.encode())):
        EVENTS.add("warn", "fleet", f"rejected agent at {host}: bad token")
        await ws.close(code=1008)
        return
    nid = node_id(str(first.get("node") or host))
    if nid == hub.local.id:
        EVENTS.add("warn", "fleet", f"rejected agent at {host}: name '{nid}' clashes with the hub")
        await ws.close(code=1008)
        return
    node = hub.nodes.get(nid)
    if node is None:
        node = hub.nodes[nid] = NodeState(nid)
        EVENTS.add("info", "fleet", f"{nid} joined the fleet ({host})", node=nid)
        if hub.store:
            node.forecast = await asyncio.to_thread(hub.store.forecast, nid)
    node.addr = host
    old = hub.agents.get(nid)
    if old is not None:
        try:
            await old.close(code=1000)
        except Exception:  # noqa: BLE001
            pass
    hub.agents[nid] = ws
    await ws.send_text(dumps({"type": "welcome", "node": nid}))
    try:
        while True:
            m = json.loads(await ws.receive_text())
            if isinstance(m, dict) and m.get("type") == "tick" and isinstance(m.get("data"), dict):
                try:
                    await hub.ingest(node, m["data"])
                except (KeyError, TypeError) as e:
                    log.warning("malformed snapshot from %s: %r", nid, e)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("agent %s disconnected: %s", nid, e)
    finally:
        if hub.agents.get(nid) is ws:
            del hub.agents[nid]


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
