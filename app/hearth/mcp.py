"""HEARTH as an MCP server: `/mcp` speaks Model Context Protocol (Streamable HTTP, JSON responses).

Any MCP client (RyuCore, Claude Code, …) can then ask about the fleet: what's online, what went
wrong last night, which container keeps restarting. Write tools (restart a container, update an
image, open a maintenance window, add a journal note) are only offered in `control` mode, which
needs both `HEARTH_MCP_TOKEN` and `HEARTH_ALLOW_CONTROL=1`. They're marked `destructiveHint`, so
clients that ask before running such tools (RyuCore does) will.

HEARTH_MCP        off | read (default) | control
HEARTH_MCP_TOKEN  bearer token; required for every MCP call when set, and always for `control`
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime

from . import __version__
from .collect import ALLOW_CONTROL, EVENTS
from .control import ControlError
from .store import RANGES

log = logging.getLogger("hearth")

MODE = (os.getenv("HEARTH_MCP") or "read").strip().lower()
TOKEN = os.getenv("HEARTH_MCP_TOKEN", "").strip()
if MODE not in ("off", "read", "control"):
    log.warning("HEARTH_MCP=%r is not off/read/control; using read", MODE)
    MODE = "read"
if MODE == "control" and not (TOKEN and ALLOW_CONTROL):
    log.warning("HEARTH_MCP=control needs HEARTH_MCP_TOKEN and HEARTH_ALLOW_CONTROL=1; MCP stays read-only")
    MODE = "read"
CONTROL = MODE == "control"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LEVELS = {"info": 0, "ok": 0, "warn": 1, "crit": 2}

INSTRUCTIONS = """HEARTH is a homelab monitor. It watches every machine ("node") in the fleet: CPU, memory,
temperatures, disks, S.M.A.R.T. health, Docker containers, image updates and backups, and keeps a
30-day archive and an event log. Start with fleet_status for an overview, then drill into a node with
node_snapshot, metric_history, events and insights. Node names are the ids shown by fleet_status.
Times are local to the hub. Quote numbers from tool results; don't guess."""


class ToolError(Exception):
    pass


def _h(b) -> str | None:
    if b is None:
        return None
    b = float(b)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(b) < 1024 or u == "PB":
            return f"{b:.0f} {u}" if u == "B" or abs(b) >= 100 else f"{b:.1f} {u}"
        b /= 1024
    return None


def _when(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else None


def _dur(s) -> str | None:
    if s is None:
        return None
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def _ev(e: dict) -> dict:
    return {"time": _when(e.get("t")), "node": e.get("node"), "level": e.get("lvl"), "source": e.get("src"),
            "message": e.get("msg"), **({"recovery": True} if e.get("rec") else {})}


def _obj(props: dict, req: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": req or [], "additionalProperties": False}


NODE = {"type": "string", "description": "node id from fleet_status (default: the hub's own machine)"}
RANGE = {"type": "string", "enum": list(RANGES), "description": "time window (default 24h)"}
READ = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


class MCP:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.tools: dict[str, tuple] = {}
        self._register()

    # ── tool table ──
    def _register(self) -> None:
        t = self.tools
        t["fleet_status"] = (self.fleet_status, ("Overview of the whole fleet: every node with online state, CPU, "
                             "memory, hottest temperature, container counts and alert count, plus active maintenance "
                             "windows, beacons (ping/HTTP checks) and warnings from the last 6 hours."), _obj({}), READ)
        t["node_snapshot"] = (self.node_snapshot, ("Current detailed state of one node: host, CPU, memory, temperatures, "
                              "disks with fill-up forecast, S.M.A.R.T. drives, containers and pending image updates."),
                              _obj({"node": NODE}), READ)
        t["metric_history"] = (self.metric_history, ("Archived CPU, memory, temperature and network history for a node, "
                               "with min/avg/max and peak times plus a downsampled series."),
                               _obj({"node": NODE, "range": RANGE}), READ)
        t["events"] = (self.events, ("Search the event log (container crashes and restarts, overheating, disk and "
                       "drive warnings, nodes lost and recovered, control actions). Newest first."),
                       _obj({"node": {"type": "string", "description": "node id; omit for every node"}, "range": RANGE,
                             "level": {"type": "string", "enum": ["info", "warn", "crit"],
                                       "description": "lowest level to include (default warn)"},
                             "query": {"type": "string", "description": "only events whose message or source contains this"},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 300}}), READ)
        t["report"] = (self.report, ("Daily/weekly report for a node: averages, peaks, hottest day, busiest containers, "
                       "disk growth and incidents over the last N days."),
                       _obj({"node": NODE, "days": {"type": "integer", "minimum": 1, "maximum": 30}}), READ)
        t["insights"] = (self.insights, ("What KAGARI remembers about a node: today's restart streaks, quiet or rough "
                         "nights, uptime records and calm stretches."), _obj({"node": NODE}), READ)
        t["maintenance_overview"] = (self.maintenance, ("ARSENAL maintenance bay: per node OS/kernel, pending reboots, "
                                     "compose stacks, image updates, backup ages and docker disk use, plus heartbeat "
                                     "checks, maintenance windows and the change journal."), _obj({}), READ)
        t["journal"] = (self.journal, "The change journal: notes about what was changed and when (newest first).",
                        _obj({"node": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
                        READ)
        if ALLOW_CONTROL:
            t["container_logs"] = (self.container_logs, "The last lines of a container's log.",
                                   _obj({"node": NODE, "container": {"type": "string", "description": "container name"},
                                         "lines": {"type": "integer", "minimum": 10, "maximum": 1000}}, ["container"]),
                                   READ)
        if not CONTROL:
            return
        w = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
        soft = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
        t["container_action"] = (self.container_action, "Start, stop or restart a container.",
                                 _obj({"node": NODE, "container": {"type": "string", "description": "container name"},
                                       "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
                                      ["container", "action"]), w)
        t["stack_action"] = (self.stack_action, "Start, stop or restart every container of a compose project.",
                             _obj({"node": NODE, "project": {"type": "string"},
                                   "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
                                  ["project", "action"]), w)
        t["update_container"] = (self.update_container, ("Pull the newer image for a container and recreate it with "
                                 "the same settings (only when maintenance_overview lists an update for it)."),
                                 _obj({"node": NODE, "container": {"type": "string"}}, ["container"]), w)
        t["open_maintenance"] = (self.open_maintenance, ("Open a maintenance window: push alerts are muted for that "
                                 "node (or the whole fleet) until it ends."),
                                 _obj({"node": {"type": "string", "description": "omit for the whole fleet"},
                                       "minutes": {"type": "integer", "minimum": 5, "maximum": 10080},
                                       "reason": {"type": "string"}}, ["minutes", "reason"]), soft)
        t["add_journal_note"] = (self.add_note, "Write a note in the change journal (what you changed and why).",
                                 _obj({"text": {"type": "string"}, "node": {"type": "string"}}, ["text"]), soft)

    def tool_list(self) -> list[dict]:
        return [{"name": n, "description": d, "inputSchema": s, "annotations": a}
                for n, (_fn, d, s, a) in self.tools.items()]

    # ── helpers ──
    def _node(self, nid) -> object:
        nodes = self.hub.nodes
        if nid in (None, ""):
            return self.hub.local
        nid = str(nid)
        if nid in nodes:
            return nodes[nid]
        low = {k.lower(): v for k, v in nodes.items()}
        low.update({str(((n.latest or {}).get("host") or {}).get("name", "")).lower(): n for n in nodes.values()})
        if nid.lower() in low:
            return low[nid.lower()]
        raise ToolError(f"unknown node {nid!r}; known nodes: {', '.join(nodes)}")

    def _store(self):
        if not self.hub.store:
            raise ToolError("the history store is unavailable on this hub")
        return self.hub.store

    def _container(self, node, name: str) -> dict:
        cts = ((node.latest or {}).get("docker") or {}).get("containers") or []
        c = next((c for c in cts if c.get("name") == name), None) or \
            next((c for c in cts if len(name) >= 6 and c.get("id", "").startswith(name)), None)
        if c is None:
            raise ToolError(f"no container {name!r} on {node.id}; containers: {', '.join(c['name'] for c in cts)}")
        return c

    # ── read tools ──
    def fleet_status(self, a: dict) -> dict:
        hub = self.hub
        nodes = []
        for n in sorted(hub.nodes.values(), key=lambda n: (not n.local, n.id)):
            s, host = n.summary(), ((n.latest or {}).get("host") or {})
            nodes.append({"node": n.id, "name": s["name"], "role": "hub" if n.local else "agent",
                          "online": s["online"], "last_seen_s": s["age"], "os": host.get("os"),
                          "uptime": _dur(host.get("uptime")), "cpu_pct": s["cpu"], "mem_pct": s["mem"],
                          "hottest_c": s["hot"], "containers_running": s["running"], "containers_total": s["total"],
                          "active_alerts": s["alerts"]})
        since = (time.time() - 6 * 3600) * 1000
        warn = [_ev(e) for e in reversed(EVENTS.recent(200)) if e["t"] >= since and e["lvl"] in ("warn", "crit")][:25]
        return {"now": _when(time.time() * 1000), "hub": hub.local.id, "nodes": nodes,
                "maintenance_windows": hub.ops.active(),
                "beacons": [{"name": b["name"], "target": b["target"], "up": b["up"], "rtt_ms": b["rtt"],
                             "uptime_24h": b["uptime"], "error": b["error"]} for b in hub.beacons.snapshot()],
                "warnings_last_6h": warn}

    def node_snapshot(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        s = n.latest or {}
        if not s:
            raise ToolError(f"no data from {n.id} yet")
        host, cpu, mem = s.get("host") or {}, s.get("cpu") or {}, s.get("mem") or {}
        temps = sorted(s.get("temps") or [], key=lambda t: -t["c"])
        dk = s.get("docker") or {}
        ars = s.get("arsenal") or {}
        net = s.get("net") or {}
        prim = next((i for i in net.get("ifaces", []) if i["name"] == net.get("primary")), None)
        return {
            "node": n.id, "online": n.online, "sampled": _when(s.get("t")),
            "host": {"name": host.get("name"), "os": host.get("os"), "kernel": host.get("kernel"),
                     "arch": host.get("arch"), "cpu_model": host.get("cpu"), "cores": host.get("cores"),
                     "threads": host.get("threads"), "uptime": _dur(host.get("uptime"))},
            "cpu": {"total_pct": cpu.get("total"), "load_1_5_15": cpu.get("load"), "mhz": cpu.get("mhz"),
                    "busiest_core_pct": max(cpu.get("cores") or [0])},
            "memory": {"used_pct": mem.get("pct"), "used": _h(mem.get("used")), "total": _h(mem.get("total")),
                       "swap_pct": mem.get("swap_pct")},
            "network": {"primary": prim["name"], "rx_per_s": _h(prim["rx"]), "tx_per_s": _h(prim["tx"])} if prim else None,
            "temperatures": [{"sensor": f"{t['chip']}/{t['label']}", "c": t["c"], "warn_c": t["warn"], "crit_c": t["crit"],
                              "level": t["lvl"]} for t in temps if t["lvl"] != "ok"] + [
                {"sensor": f"{t['chip']}/{t['label']}", "c": t["c"], "level": t["lvl"]} for t in temps if t["lvl"] == "ok"][:6],
            "disks": [{"mount": d["mount"], "used_pct": d["pct"], "used": _h(d["used"]), "free": _h(d["free"]),
                       "size": _h(d["total"]), "forecast": self._forecast(n.forecast.get(d["mount"]))}
                      for d in s.get("disks") or []],
            "drives": [{"dev": d.get("dev"), "model": d.get("model"), "level": d.get("lvl"), "issue": d.get("issue"),
                        "temp_c": d.get("temp"), "power_on_h": d.get("hours"), "wear_pct": d.get("wear"),
                        **({"error": d["error"]} if d.get("error") else {})}
                       for d in (s.get("smart") or {}).get("drives", [])],
            "docker": {"online": dk.get("online"), "error": dk.get("error"), "running": dk.get("running"),
                       "total": dk.get("total"),
                       "containers": [{"name": c.get("name"), "state": c.get("state"), "status": c.get("status"),
                                       "health": c.get("health"), "image": c.get("image"), "project": c.get("project"),
                                       "cpu_pct": c.get("cpu"), "mem": _h(c.get("mem")), "ports": c.get("ports")}
                                      for c in dk.get("containers") or []]},
            "image_updates": [{"container": u.get("name"), "image": u.get("image"), "state": u.get("state")}
                              for u in ars.get("updates") or [] if u.get("state") not in (None, "current")],
            "host_maintenance": ars.get("host"),
        }

    @staticmethod
    def _forecast(f: dict | None) -> str | None:
        if not f:
            return None
        if f.get("state") == "growing":
            return f"growing {_h(f['per_day'])}/day, full in ~{f['eta_days']} days"
        return f.get("state")

    def metric_history(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        rng = a.get("range") or "24h"
        if rng not in RANGES:
            raise ToolError(f"range must be one of {', '.join(RANGES)}")
        h = self._store().history(n.id, rng)
        keys = ("cpu", "cpu_max", "mem", "hot", "rx", "tx")
        stats = {}
        for k in keys:
            vals = [(v, t) for v, t in zip(h[k], h["ts"]) if v is not None]
            if not vals:
                continue
            mx = max(vals)
            stats[k] = {"min": min(v for v, _ in vals), "avg": round(sum(v for v, _ in vals) / len(vals), 2),
                        "max": mx[0], "max_at": _when(mx[1])}
        for k in ("rx", "tx"):
            if k in stats:
                stats[k] = {kk: (_h(v) + "/s" if kk != "max_at" and v is not None else v) for kk, v in stats[k].items()}
        # ≤ 48 rows: [time, cpu avg %, cpu peak %, mem %, hottest °C]
        n_pts, step = len(h["ts"]), max(1, len(h["ts"]) // 48)
        def agg(k: str, i: int, f) -> float | None:
            xs = [x for x in h[k][i:i + step] if x is not None]
            return round(f(xs), 1) if xs else None

        def avg(xs: list) -> float:
            return sum(xs) / len(xs)

        rows = [[_when(h["ts"][i]), agg("cpu", i, avg), agg("cpu_max", i, max), agg("mem", i, avg), agg("hot", i, max)]
                for i in range(0, n_pts, step)]
        return {"node": n.id, "range": rng, "minutes_with_data": h["points"] * h["step"] // 60, "stats": stats,
                "series_columns": ["time", "cpu_avg_pct", "cpu_peak_pct", "mem_pct", "hottest_c"],
                "series": [r for r in rows if any(v is not None for v in r[1:])]}

    def events(self, a: dict) -> dict:
        rng = a.get("range") or "24h"
        if rng not in RANGES:
            raise ToolError(f"range must be one of {', '.join(RANGES)}")
        node = self._node(a["node"]).id if a.get("node") else None
        lvl = LEVELS.get(a.get("level") or "warn", 1)
        q = (a.get("query") or "").strip().lower()
        limit = max(1, min(int(a.get("limit") or 60), 300))
        rows = self._store().search_events(node, int(time.time()) - RANGES[rng][0], 2000)
        out = []
        for e in rows:
            if LEVELS.get(e["lvl"], 0) < lvl and not (e.get("rec") and lvl <= 1):
                continue
            if q and q not in (e["msg"] or "").lower() and q not in (e["src"] or "").lower():
                continue
            out.append(_ev(e))
            if len(out) >= limit:
                break
        return {"node": node or "all", "range": rng, "count": len(out), "events": out}

    def report(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        days = max(1, min(int(a.get("days") or 7), 30))
        return self._store().report(n.id, days, [b.name for b in self.hub.beacons.items])

    def insights(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        return self._store().insights(n.id, n.boot_key())

    def maintenance(self, a: dict) -> dict:
        v = self.hub.arsenal_view()
        for n in v["nodes"]:
            ars = n.pop("arsenal", None) or {}
            n["image_updates"] = [{"container": u.get("name"), "image": u.get("image"), "state": u.get("state")}
                                  for u in ars.get("updates") or []]
            n["backups"] = ars.get("backups")
            n["docker_disk"] = ars.get("df")
            n["host"] = ars.get("host")
            n["uptime"] = _dur(n.get("uptime"))
        for j in v["journal"]:
            j["t"] = _when(j["t"])
        v["t"] = _when(v["t"])
        v["journal"] = v["journal"][:30]
        return v

    def journal(self, a: dict) -> dict:
        notes = self._store().notes(a.get("node") or None, 0, None, max(1, min(int(a.get("limit") or 50), 200)))
        return {"notes": [{"time": _when(x["t"]), "node": x["node"] or "fleet", "text": x["text"], "by": x["who"]}
                          for x in notes]}

    async def container_logs(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        c = self._container(n, a.get("container") or "")
        lines = max(10, min(int(a.get("lines") or 150), 1000))
        res = await self.hub.command(n.id, "logs", c["id"], lines)
        text = res.get("logs") or ""
        return {"node": n.id, "container": c["name"], "lines": lines, "logs": text[-30000:]}

    # ── write tools ──
    async def container_action(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        act = a.get("action")
        if act not in ("start", "stop", "restart"):
            raise ToolError("action must be start, stop or restart")
        c = self._container(n, a.get("container") or "")
        EVENTS.add("info", "control", f"{c['name']} ⚙ {act} requested via MCP", node=n.id)
        try:
            await self.hub.command(n.id, act, c["id"])
        except ControlError as e:
            EVENTS.add("warn", "control", f"{c['name']} ⚙ {act} failed: {e}", node=n.id)
            raise ToolError(str(e)) from None
        return {"ok": True, "node": n.id, "container": c["name"], "action": act}

    async def stack_action(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        act, proj = a.get("action"), str(a.get("project") or "")
        if act not in ("start", "stop", "restart"):
            raise ToolError("action must be start, stop or restart")
        cts = ((n.latest or {}).get("docker") or {}).get("containers") or []
        if not any(c.get("project") == proj for c in cts):
            raise ToolError(f"no compose project {proj!r} on {n.id}")
        EVENTS.add("info", "arsenal", f"⚙ stack {proj}:{act} requested via MCP", node=n.id)
        try:
            res = await self.hub.command(n.id, "stack", "", 0, f"{proj}:{act}")
        except ControlError as e:
            raise ToolError(str(e)) from None
        return {"ok": True, "node": n.id, "project": proj, "action": act, "job": res.get("job")}

    async def update_container(self, a: dict) -> dict:
        n = self._node(a.get("node"))
        c = self._container(n, a.get("container") or "")
        EVENTS.add("info", "arsenal", f"⚙ update {c['id']} ({c['name']}) requested via MCP", node=n.id)
        try:
            res = await self.hub.command(n.id, "update", c["id"], 0, None)
        except ControlError as e:
            raise ToolError(str(e)) from None
        return {"ok": True, "node": n.id, "container": c["name"], "job": res.get("job"),
                "note": "the update runs in the background; check maintenance_overview or events for the result"}

    def open_maintenance(self, a: dict) -> dict:
        node = self._node(a["node"]).id if a.get("node") else None
        minutes = max(5, min(int(a.get("minutes") or 60), 7 * 24 * 60))
        reason = re.sub(r"[\x00-\x1f\x7f]", "", str(a.get("reason") or "")).strip()[:200]
        return self.hub.ops.open_window(node, minutes, reason, "mcp")

    def add_note(self, a: dict) -> dict:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(a.get("text") or "")).strip()[:500]
        if not text:
            raise ToolError("text is empty")
        node = self._node(a["node"]).id if a.get("node") else None
        self._store()
        return self.hub.ops.add_note(text, node, "mcp")

    # ── JSON-RPC ──
    async def call(self, name: str, args: dict) -> dict:
        if name not in self.tools:
            return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
        fn = self.tools[name][0]
        try:
            res = await fn(args) if asyncio.iscoroutinefunction(fn) else await asyncio.to_thread(fn, args)
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (ControlError, PermissionError, LookupError, RuntimeError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (TypeError, ValueError) as e:
            return {"content": [{"type": "text", "text": f"bad arguments: {e}"}], "isError": True}
        except Exception as e:  # noqa: BLE001
            log.exception("mcp tool %s failed", name)
            return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, default=str)}]}

    async def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32600, "message": "invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:  # notification
            return None
        try:
            if method == "initialize":
                want = str(params.get("protocolVersion") or "")
                res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "hearth", "title": "HEARTH 炉心", "version": __version__},
                       "instructions": INSTRUCTIONS}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": self.tool_list()}
            elif method == "tools/call":
                args = params.get("arguments") or {}
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                res = await self.call(str(params.get("name")), args)
            elif method in ("resources/list", "prompts/list"):
                res = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except ValueError as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def authorized(header: str | None) -> bool:
    if not TOKEN:
        return True
    got = (header or "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(got.encode(), TOKEN.encode())
