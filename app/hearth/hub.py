"""HEARTH hub: collects its own node, accepts LAN agents, stores history, serves the UI."""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, control, metrics
from .arsenal import PRUNES, STACK_ACTS, Ops
from .checks import Beacons
from .collect import ALLOW_CONTROL, EVENTS, HISTORY, INTERVAL, NODE_ID, Collector, node_id
from .control import ControlError
from .gate import Gate, GateError
from .notify import Notifier
from .store import RANGES, Store

log = logging.getLogger("hearth")

TOKEN = os.getenv("HEARTH_TOKEN", "").strip()
DB_PATH = os.getenv("HEARTH_DB", "/data/hearth.db")
RETENTION_DAYS = int(os.getenv("HEARTH_RETENTION_DAYS", "30"))
LOST_AFTER = float(os.getenv("HEARTH_LOST_AFTER", "15"))
DISK_SAMPLE_EVERY = 600
SLOW_EVERY = 600  # container usage flush + uptime records
METRICS = os.getenv("HEARTH_METRICS", "1").strip().lower() not in ("0", "false", "no", "off")
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
        self.slow_at = time.monotonic()
        self.rec_at = 0.0
        self.ct: dict[str, list] = {}  # container name -> [cpu core-seconds, peak memory] since the last flush
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

    def account(self, snap: dict, dt: float) -> None:
        if dt <= 0:
            return
        for c in (snap.get("docker") or {}).get("containers") or []:
            if c.get("state") == "running" and c.get("cpu") is not None:
                a = self.ct.setdefault(c["name"], [0.0, 0])
                a[0] += c["cpu"] / 100 * dt
                a[1] = max(a[1], c.get("mem") or 0)

    def boot_key(self) -> str | None:
        host = (self.latest or {}).get("host") or {}
        bt = host.get("boot_time") or ((self.latest or {}).get("t", 0) / 1000 - host.get("uptime", 0))
        return f"boot:{int(bt) // 60}" if host.get("uptime") else None

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
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.store: Store | None = None
        try:
            self.store = Store(DB_PATH)
            EVENTS.seed(self.store.recent_events(100))
            EVENTS.listeners.append(self._persist_event)
        except Exception as e:  # noqa: BLE001
            log.warning("history store disabled (%s): %s", DB_PATH, e)
        self.notifier = Notifier()
        self.ops = Ops(self.store)
        # maintenance windows mute push alerts (the log and the dashboard still show everything)
        EVENTS.listeners.append(lambda e: None if self.ops.muted(e) else self.notifier.on_event(e))
        self.ev_counts: Counter = Counter()
        EVENTS.listeners.append(lambda e: self.ev_counts.update([(e["lvl"], e["src"])]))
        self.beacons = Beacons(on_result=self._beacon_result)
        self.gate = Gate(self)
        self.started = time.time()
        self.test_at = 0.0
        self._pruned = 0.0
        self._uptime_at = 0.0

    def _beacon_result(self, name: str, ts: int, up: bool, rtt: float | None) -> None:
        if self.store:
            self.store.add_beacon(name, ts, up, rtt)

    def _beacon_uptime(self) -> None:
        since = int(time.time()) - 86400
        for b in self.beacons.items:
            b.uptime = self.store.beacon_uptime(b.name, since)

    def features(self) -> dict:
        return {"version": __version__, "hub": self.local.id, "control": ALLOW_CONTROL, "metrics": METRICS,
                "store": self.store is not None, "retention": RETENTION_DAYS, "notify": self.notifier.status(),
                "beacons": len(self.beacons.items), "started": int(self.started * 1000)}

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
        now = time.monotonic()
        dt = min(5.0, now - node.last_seen) if node.last_seen else 0.0
        node.latest = snap
        node.last_seen = now
        node.record(snap)
        await asyncio.to_thread(self._persist, node, snap, dt)
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

    def _persist(self, node: NodeState, snap: dict, dt: float = 0.0) -> None:
        if not self.store:
            return
        try:
            node.account(snap, dt)
            row = node.acc.add(snap)
            if row:
                self.store.add_sample(node.id, row)
            now = time.monotonic()
            if snap.get("disks") and now - node.disk_at >= DISK_SAMPLE_EVERY:
                node.disk_at = now
                self.store.add_disks(node.id, int(time.time()), snap["disks"])
                node.forecast = self.store.forecast(node.id)
            if now - node.slow_at >= SLOW_EVERY and node.ct:
                node.slow_at = now
                rows, node.ct = [(k, v[0], v[1]) for k, v in node.ct.items()], {}
                self.store.add_ct_usage(node.id, datetime.now().strftime("%Y-%m-%d"), rows)
            if now - node.rec_at >= SLOW_EVERY:
                node.rec_at = now
                key, up = node.boot_key(), (snap.get("host") or {}).get("uptime")
                if key and up:
                    self.store.record_max(node.id, key, up)
            if self.beacons.items and now - self._uptime_at >= 300:
                self._uptime_at = now
                self._beacon_uptime()
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
                m = {"type": "fleet", "nodes": self.fleet(), "events": EVENTS.recent(60), "maint": self.ops.active()}
                if self.beacons.items:
                    m["beacons"] = self.beacons.snapshot()
                msg = dumps(m)
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
                                  "store": self.store is not None, "features": self.features(),
                                  "beacons": self.beacons.snapshot(), "maint": self.ops.active()}))

    # ── container control ──
    async def command(self, nid: str, action: str, cid: str, lines: int = 200, arg: str | None = None) -> dict:
        if nid == self.local.id:
            return await asyncio.to_thread(control.run, action, cid, lines, arg)
        ws = self.agents.get(nid)
        if ws is None:
            raise ControlError(f"{nid} is not connected")
        rid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = (nid, fut)
        try:
            await ws.send_text(dumps({"type": "cmd", "id": rid, "action": action, "cid": cid, "lines": lines, "arg": arg}))
            res = await asyncio.wait_for(fut, timeout=30)
        except asyncio.TimeoutError:
            raise ControlError(f"{nid} did not answer in time") from None
        finally:
            self.pending.pop(rid, None)
        if not res.get("ok"):
            raise ControlError(str(res.get("error") or "failed")[:300])
        return {k: v for k, v in res.items() if k in ("ok", "logs", "job")}

    def fail_pending(self, nid: str) -> None:
        for n, fut in list(self.pending.values()):
            if n == nid and not fut.done():
                fut.set_exception(ControlError(f"{nid} disconnected"))

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
    hub.beacons.start()
    hub.gate.start()
    hub.ops.start()
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


def _guard(request: Request) -> None:
    """State-changing calls need a custom header: browsers can't send one cross-site without CORS, which HEARTH never grants."""
    if request.headers.get("x-hearth") != "1":
        raise HTTPException(403, "missing X-Hearth header")


def _need_store() -> Store:
    if not hub.store:
        raise HTTPException(503, "history store unavailable")
    return hub.store


@app.get("/api/info")
async def api_info():
    return hub.features()


@app.get("/metrics")
async def prometheus():
    if not METRICS:
        raise HTTPException(404, "metrics disabled (HEARTH_METRICS=0)")
    return PlainTextResponse(metrics.render(hub), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/events")
async def api_events(node: str | None = None, range: str = Query("24h")):  # noqa: A002
    if range not in RANGES:
        raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
    span, step = RANGES[range]
    end = int(time.time()) // step * step + step
    return await asyncio.to_thread(_need_store().events_between, hub.resolve(node), end - span, end)


@app.get("/api/insights")
async def api_insights(node: str | None = None):
    n = hub.nodes[hub.resolve(node)]
    return await asyncio.to_thread(_need_store().insights, n.id, n.boot_key())


@app.get("/api/report")
async def api_report(node: str | None = None, days: int = Query(7, ge=1, le=30)):
    return await asyncio.to_thread(_need_store().report, hub.resolve(node), days, [b.name for b in hub.beacons.items])


@app.get("/api/beacons")
async def api_beacons():
    return hub.beacons.snapshot()


@app.post("/api/notify/test")
async def api_notify_test(request: Request):
    _guard(request)
    if not hub.notifier.targets:
        raise HTTPException(409, "no push targets configured (set HEARTH_NOTIFY)")
    if time.monotonic() - hub.test_at < 10:
        raise HTTPException(429, "one test every 10 seconds, please")
    hub.test_at = time.monotonic()
    return {"results": await asyncio.to_thread(hub.notifier.test, hub.local.id)}


@app.post("/api/container")
async def api_container(request: Request):
    _guard(request)
    if not ALLOW_CONTROL:
        raise HTTPException(403, "container control is disabled on the hub (set HEARTH_ALLOW_CONTROL=1)")
    try:
        body = await request.json()
        nid, cid, action = str(body["node"]), str(body["id"]), str(body["action"])
        lines = max(10, min(int(body.get("lines") or 200), 2000))
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected JSON {node, id, action}") from None
    if action not in control.ACTIONS:
        raise HTTPException(400, f"action must be one of {', '.join(control.ACTIONS)}")
    node = hub.nodes.get(nid)
    if node is None:
        raise HTTPException(404, f"unknown node {nid!r}")
    ct = next((c for c in ((node.latest or {}).get("docker") or {}).get("containers") or [] if c.get("id") == cid), None)
    if ct is None:
        raise HTTPException(404, "unknown container")
    who = request.client.host if request.client else "?"
    if action != "logs":
        EVENTS.add("info", "control", f"{ct['name']} ⚙ {action} requested from {who}", node=nid)
    try:
        return await hub.command(nid, action, cid, lines)
    except ControlError as e:
        if action != "logs":
            EVENTS.add("warn", "control", f"{ct['name']} ⚙ {action} failed: {e}", node=nid)
        raise HTTPException(409, str(e)) from None


# ── GATE 転送門 ──
async def _body(request: Request) -> dict:
    _guard(request)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "expected a JSON body") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    return body


@app.get("/api/gate")
async def api_gate():
    return await asyncio.to_thread(hub.gate.services)


@app.get("/api/gate/icon")
async def api_gate_icon(id: str = Query(..., max_length=200)):  # noqa: A002
    res = await asyncio.to_thread(hub.gate.icon, id)
    if not res:
        return Response(status_code=404, headers={"Cache-Control": "max-age=600"})
    # served from the hub's origin, so lock it down: no scripts even if someone opens an SVG directly
    return Response(res[1], media_type=res[0], headers={
        "Cache-Control": "max-age=3600", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.post("/api/gate/item")
async def api_gate_item(request: Request):
    body = await _body(request)
    try:
        res = await asyncio.to_thread(hub.gate.save, body)
    except GateError as e:
        raise HTTPException(409, str(e)) from None
    who = request.client.host if request.client else "?"
    log.info("gate: %s saved %s", who, res["id"])
    return res


@app.post("/api/gate/item/delete")
async def api_gate_item_delete(request: Request):
    body = await _body(request)
    try:
        return await asyncio.to_thread(hub.gate.delete, str(body.get("id") or "")[:200])
    except GateError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/gate/wake")
async def api_gate_wake(request: Request):
    body = await _body(request)
    who = request.client.host if request.client else "?"
    try:
        res = await asyncio.to_thread(hub.gate.wake, str(body.get("id") or "")[:100])
    except GateError as e:
        raise HTTPException(409, str(e)) from None
    EVENTS.add("info", "gate", f"⏻ wake-on-LAN sent to {res['name']} ({res['mac']}) from {who}")
    return res


# ── ARSENAL 工廠 ──
@app.get("/api/arsenal")
async def api_arsenal():
    nodes = []
    for n in sorted(hub.nodes.values(), key=lambda n: (not n.local, n.id)):
        s = n.latest or {}
        dk = s.get("docker") or {}
        stacks: dict[str, dict] = {}
        for c in dk.get("containers") or []:
            if c.get("project"):
                st = stacks.setdefault(c["project"], {"name": c["project"], "running": 0, "total": 0, "bad": 0, "self": False})
                st["total"] += 1
                st["running"] += c.get("state") == "running"
                st["bad"] += c.get("health") == "unhealthy" or c.get("state") in ("restarting", "dead")
                st["self"] |= bool(c.get("self"))
        host = s.get("host") or {}
        nodes.append({"id": n.id, "name": host.get("name", n.id), "local": n.local, "online": n.online,
                      "os": host.get("os"), "kernel": host.get("kernel"), "uptime": host.get("uptime"),
                      "arch": host.get("arch"), "docker": dk.get("online"), "control": bool(dk.get("control")),
                      "arsenal": s.get("arsenal"), "stacks": sorted(stacks.values(), key=lambda x: x["name"])})
    notes = await asyncio.to_thread(hub.store.notes, None, 0, None, 100) if hub.store else []
    return {"nodes": nodes, "maint": hub.ops.active(), "heartbeats": hub.ops.heartbeats(), "journal": notes,
            "hub_control": ALLOW_CONTROL, "store": hub.store is not None, "t": int(time.time() * 1000)}


@app.post("/api/arsenal/action")
async def api_arsenal_action(request: Request):
    body = await _body(request)
    nid, action = str(body.get("node") or ""), str(body.get("action") or "")
    cid, arg = str(body.get("id") or ""), body.get("arg")
    arg = str(arg)[:200] if arg is not None else None
    if action not in ("check", "update", "prune", "stack"):
        raise HTTPException(400, "action must be check, update, prune or stack")
    if action != "check" and not ALLOW_CONTROL:
        raise HTTPException(403, "control is disabled on the hub (set HEARTH_ALLOW_CONTROL=1)")
    if action == "prune" and arg not in PRUNES:
        raise HTTPException(400, f"arg must be one of {', '.join(PRUNES)}")
    if action == "stack" and (arg or "").rpartition(":")[2] not in STACK_ACTS:
        raise HTTPException(400, "arg must be project:start|stop|restart")
    if nid not in hub.nodes:
        raise HTTPException(404, f"unknown node {nid!r}")
    who = request.client.host if request.client else "?"
    if action != "check":
        what = {"update": f"update {cid}", "prune": f"prune {arg}", "stack": f"stack {arg}"}[action]
        EVENTS.add("info", "arsenal", f"⚙ {what} requested from {who}", node=nid)
    try:
        return await hub.command(nid, action, cid, 0, arg)
    except ControlError as e:
        raise HTTPException(409, str(e)) from None


@app.post("/api/maint")
async def api_maint(request: Request):
    body = await _body(request)
    node = body.get("node") or None
    if node is not None and str(node) not in hub.nodes:
        raise HTTPException(404, "unknown node")
    try:
        minutes = max(5, min(int(body.get("minutes") or 60), 7 * 24 * 60))
    except (TypeError, ValueError):
        raise HTTPException(400, "minutes must be a number") from None
    reason = re.sub(r"[\x00-\x1f\x7f]", "", str(body.get("reason") or "")).strip()[:200]
    who = request.client.host if request.client else "?"
    return await asyncio.to_thread(hub.ops.open_window, str(node) if node else None, minutes, reason, who)


@app.post("/api/maint/stop")
async def api_maint_stop(request: Request):
    body = await _body(request)
    if not hub.ops.close_window(str(body.get("id") or "")):
        raise HTTPException(404, "no such maintenance window")
    return {"ok": True}


@app.get("/api/journal")
async def api_journal(node: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    return await asyncio.to_thread(_need_store().notes, node, 0, None, limit)


@app.post("/api/journal")
async def api_journal_add(request: Request):
    body = await _body(request)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(body.get("text") or "")).strip()[:500]
    if not text:
        raise HTTPException(400, "write something first")
    node = body.get("node") or None
    if node is not None and str(node) not in hub.nodes:
        raise HTTPException(404, "unknown node")
    _need_store()
    who = request.client.host if request.client else "?"
    return await asyncio.to_thread(hub.ops.add_note, text, str(node) if node else None, who)


@app.post("/api/journal/delete")
async def api_journal_del(request: Request):
    body = await _body(request)
    try:
        nid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "expected {id}") from None
    if not await asyncio.to_thread(_need_store().del_note, nid):
        raise HTTPException(404, "no such note")
    return {"ok": True}


@app.api_route("/api/ping/{name}", methods=["GET", "POST", "HEAD"])
@app.api_route("/api/ping/{name}/{result}", methods=["GET", "POST", "HEAD"])
async def api_ping(name: str, request: Request, result: str = "ok", msg: str | None = Query(None, max_length=200)):
    """Heartbeat for backup jobs: `curl -fsS http://hub:8088/api/ping/offsite` (append /fail on errors)."""
    host = request.client.host if request.client else None
    if not lan_allowed(host):
        raise HTTPException(403, "pings are accepted from LAN addresses only")
    if result not in ("ok", "fail"):
        raise HTTPException(404, "use /api/ping/<name> or /api/ping/<name>/fail")
    try:
        return hub.ops.ping(node_id(name), result == "ok", msg)
    except LookupError:
        raise HTTPException(404, f"unknown heartbeat {name!r} (add it to HEARTH_HEARTBEATS)") from None


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
            if not isinstance(m, dict):
                continue
            if m.get("type") == "tick" and isinstance(m.get("data"), dict):
                try:
                    await hub.ingest(node, m["data"])
                except (KeyError, TypeError) as e:
                    log.warning("malformed snapshot from %s: %r", nid, e)
            elif m.get("type") == "cmd_result":
                p = hub.pending.get(str(m.get("id")))
                if p and p[0] == nid and not p[1].done():
                    p[1].set_result(m)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        log.info("agent %s disconnected: %s", nid, e)
    finally:
        if hub.agents.get(nid) is ws:
            del hub.agents[nid]
            hub.fail_pending(nid)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
