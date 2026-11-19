"""SQLite persistence for the hub: per-node minute samples, disk usage, events."""
from __future__ import annotations

import logging
import sqlite3
import re
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("hearth")

# range -> (span seconds, bucket seconds)
RANGES = {
    "1h": (3600, 60),
    "6h": (6 * 3600, 300),
    "24h": (86400, 300),
    "7d": (7 * 86400, 1800),
    "30d": (30 * 86400, 7200),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  node TEXT NOT NULL, ts INTEGER NOT NULL,
  cpu REAL, cpu_max REAL, mem REAL, hot REAL, rx REAL, tx REAL,
  PRIMARY KEY (node, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS disks (
  node TEXT NOT NULL, mount TEXT NOT NULL, ts INTEGER NOT NULL,
  used INTEGER, total INTEGER, free INTEGER,
  PRIMARY KEY (node, mount, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, node TEXT, lvl TEXT, src TEXT, msg TEXT, rec INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_node_ts ON events (node, ts);
CREATE TABLE IF NOT EXISTS ct_usage (
  node TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL, cpu_s REAL, mem_max INTEGER,
  PRIMARY KEY (node, day, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS records (
  node TEXT NOT NULL, key TEXT NOT NULL, value REAL, ts INTEGER,
  PRIMARY KEY (node, key)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS beacons (
  name TEXT NOT NULL, ts INTEGER NOT NULL, up INTEGER, rtt REAL,
  PRIMARY KEY (name, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gate_items (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT, grp TEXT, url TEXT, icon TEXT,
  hidden INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, created INTEGER) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gate_seen (id TEXT PRIMARY KEY, node TEXT, data TEXT, ts INTEGER) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, node TEXT, text TEXT, who TEXT);
CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts INTEGER) WITHOUT ROWID;
"""
# "nginx ↻ restarted" / "nginx ✖ exited (137)": restarts and crashes, as logged by DockerWatch.
RESTART_RE = re.compile(r"^(\S+) (?:↻ restarted|✖ exited \((?!0\)|143\))[^)]*\))")


def _midnight(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(ts if ts is not None else time.time())
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def many(self, sql: str, rows: list[tuple]) -> None:
        with self.lock:
            self.db.executemany(sql, rows)

    # ── writes ──
    def add_sample(self, node: str, row: tuple) -> None:
        self.q("INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?)", (node, *row))

    def add_disks(self, node: str, ts: int, disks: list[dict]) -> None:
        self.many("INSERT OR REPLACE INTO disks VALUES (?,?,?,?,?,?)",
                  [(node, d["mount"], ts, d["used"], d["total"], d["free"]) for d in disks])

    def add_event(self, e: dict) -> None:
        self.q("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)",
               (e["id"], e["t"] // 1000, e["node"], e["lvl"], e["src"], e["msg"], int(bool(e.get("rec")))))

    def add_ct_usage(self, node: str, day: str, rows: list[tuple[str, float, int]]) -> None:
        self.many("INSERT INTO ct_usage VALUES (?,?,?,?,?) ON CONFLICT (node, day, name) DO UPDATE SET "
                  "cpu_s = cpu_s + excluded.cpu_s, mem_max = MAX(mem_max, excluded.mem_max)",
                  [(node, day, name, cpu_s, mem) for name, cpu_s, mem in rows])

    def record_max(self, node: str, key: str, value: float) -> None:
        self.q("INSERT INTO records VALUES (?,?,?,?) ON CONFLICT (node, key) DO UPDATE SET "
               "value = MAX(value, excluded.value), ts = excluded.ts", (node, key, value, int(time.time())))

    def add_beacon(self, name: str, ts: int, up: bool, rtt: float | None) -> None:
        self.q("INSERT OR REPLACE INTO beacons VALUES (?,?,?,?)", (name, ts, int(up), rtt))

    def prune(self, days: int) -> None:
        cut = int(time.time()) - days * 86400
        for table in ("samples", "disks", "events", "beacons"):
            self.q(f"DELETE FROM {table} WHERE ts < ?", (cut,))  # noqa: S608 (fixed table names)
        self.q("DELETE FROM ct_usage WHERE day < ?", (_day(cut),))

    # ── GATE: link overrides, bookmarks, remembered services, small key/values ──
    GATE_COLS = ("id", "kind", "name", "grp", "url", "icon", "hidden", "pinned", "created")

    def gate_items(self) -> list[dict]:
        return [dict(zip(self.GATE_COLS, r)) for r in self.q(f"SELECT {', '.join(self.GATE_COLS)} FROM gate_items")]

    def gate_put(self, item: dict) -> None:
        self.q("INSERT OR REPLACE INTO gate_items VALUES (?,?,?,?,?,?,?,?,?)",
               tuple(item.get(c) for c in self.GATE_COLS))

    def gate_del(self, iid: str) -> None:
        self.q("DELETE FROM gate_items WHERE id = ?", (iid,))

    def gate_seen(self) -> dict[str, tuple[str, str]]:
        return {r[0]: (r[1], r[2]) for r in self.q("SELECT id, node, data FROM gate_seen")}

    def gate_seen_put(self, rows: list[tuple[str, str, str]]) -> None:
        now = int(time.time())
        self.many("INSERT OR REPLACE INTO gate_seen VALUES (?,?,?,?)", [(i, n, d, now) for i, n, d in rows])

    def gate_seen_del(self, ids: list[str]) -> None:
        self.many("DELETE FROM gate_seen WHERE id = ?", [(i,) for i in ids])

    def kv_get(self, prefix: str) -> dict[str, str]:
        return dict(self.q("SELECT k, v FROM kv WHERE k LIKE ? || '%'", (prefix,)))

    def kv_put(self, k: str, v: str) -> None:
        self.q("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (k, v, int(time.time())))

    # ── change journal (kept forever, unlike events) ──
    def add_note(self, text: str, node: str | None, who: str) -> dict:
        ts = int(time.time())
        with self.lock:
            cur = self.db.execute("INSERT INTO journal (ts, node, text, who) VALUES (?,?,?,?)", (ts, node, text, who))
            nid = cur.lastrowid
        return {"id": nid, "t": ts * 1000, "node": node, "text": text, "who": who}

    def notes(self, node: str | None = None, start: int = 0, end: int | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT id, ts, node, text, who FROM journal WHERE ts >= ? AND ts < ?", [start, end or 2 ** 40]
        if node:
            sql += " AND (node = ? OR node IS NULL)"
            args.append(node)
        rows = self.q(sql + " ORDER BY ts DESC LIMIT ?", (*args, limit))
        return [{"id": r[0], "t": r[1] * 1000, "node": r[2], "text": r[3], "who": r[4]} for r in rows]

    def del_note(self, nid: int) -> bool:
        return bool(self.q("DELETE FROM journal WHERE id = ? RETURNING id", (nid,)))

    # ── reads ──
    def recent_events(self, n: int = 100) -> list[dict]:
        rows = self.q("SELECT id, ts, node, lvl, src, msg, rec FROM events ORDER BY id DESC LIMIT ?", (n,))
        out = []
        for r in reversed(rows):
            e = {"id": r[0], "t": r[1] * 1000, "node": r[2], "lvl": r[3], "src": r[4], "msg": r[5]}
            if r[6]:
                e["rec"] = True
            out.append(e)
        return out

    def history(self, node: str, rng: str) -> dict:
        span, step = RANGES[rng]
        end = int(time.time()) // step * step + step
        start = end - span
        rows = self.q(
            "SELECT (ts / ?) * ? AS b, AVG(cpu), MAX(cpu_max), AVG(mem), MAX(hot), AVG(rx), AVG(tx) "
            "FROM samples WHERE node = ? AND ts >= ? AND ts < ? GROUP BY b",
            (step, step, node, start, end))
        by = {r[0]: r for r in rows}
        keys = ("cpu", "cpu_max", "mem", "hot", "rx", "tx")
        out: dict = {"range": rng, "step": step, "ts": [], **{k: [] for k in keys}}
        for b in range(start, end, step):
            r = by.get(b)
            out["ts"].append(b * 1000)
            for i, k in enumerate(keys, 1):
                v = r[i] if r else None
                out[k].append(round(v, 2) if v is not None else None)
        out["points"] = len(rows)
        return out

    def forecast(self, node: str, days: int = 7) -> dict[str, dict]:
        """Linear fit of used bytes over the last `days` -> growth per day and days until full."""
        rows = self.q("SELECT mount, ts, used, free FROM disks WHERE node = ? AND ts >= ? ORDER BY mount, ts",
                      (node, int(time.time()) - days * 86400))
        series: dict[str, list[tuple]] = {}
        for m, ts, used, free in rows:
            series.setdefault(m, []).append((ts, used, free))
        out = {}
        for m, pts in series.items():
            if len(pts) < 4 or pts[-1][0] - pts[0][0] < 6 * 3600:
                out[m] = {"state": "learning"}
                continue
            n = len(pts)
            mx = sum(p[0] for p in pts) / n
            my = sum(p[1] for p in pts) / n
            den = sum((p[0] - mx) ** 2 for p in pts)
            slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else 0.0  # bytes / s
            per_day = slope * 86400
            if per_day < 50 * 1024 ** 2:  # under 50 MB/day growth counts as stable
                out[m] = {"state": "stable", "per_day": round(per_day)}
            else:
                out[m] = {"state": "growing", "per_day": round(per_day),
                          "eta_days": round(pts[-1][2] / slope / 86400, 1)}
        return out

    def summary(self, node: str, hours: int = 24) -> dict:
        since = int(time.time()) - hours * 3600
        n, avg, rx, tx = self.q("SELECT COUNT(*), AVG(cpu), SUM(rx) * 60, SUM(tx) * 60 FROM samples "
                                "WHERE node = ? AND ts >= ?", (node, since))[0]
        peak = self.q("SELECT cpu_max, ts FROM samples WHERE node = ? AND ts >= ? ORDER BY cpu_max DESC LIMIT 1",
                      (node, since))
        hot = self.q("SELECT hot, ts FROM samples WHERE node = ? AND ts >= ? AND hot IS NOT NULL "
                     "ORDER BY hot DESC LIMIT 1", (node, since))
        inc = self.q("SELECT COUNT(*) FROM events WHERE node = ? AND ts >= ? AND lvl IN ('warn', 'crit')",
                     (node, since))[0][0]
        return {
            "hours": hours, "minutes": n, "cpu_avg": round(avg, 1) if avg is not None else None,
            "cpu_peak": peak[0][0] if peak else None, "cpu_peak_t": peak[0][1] * 1000 if peak else None,
            "hot_peak": round(hot[0][0], 1) if hot else None, "hot_peak_t": hot[0][1] * 1000 if hot else None,
            "rx_bytes": round(rx or 0), "tx_bytes": round(tx or 0), "incidents": inc,
        }

    def events_between(self, node: str, start: int, end: int, limit: int = 400) -> list[dict]:
        """Warnings, criticals and recoveries for one node: the markers on the archive charts."""
        rows = self.q("SELECT id, ts, lvl, src, msg, rec FROM events WHERE node = ? AND ts >= ? AND ts < ? "
                      "AND (lvl IN ('warn', 'crit') OR rec = 1) ORDER BY id DESC LIMIT ?", (node, start, end, limit))
        out = [{"id": r[0], "t": r[1] * 1000, "lvl": "ok" if r[5] else r[2], "src": r[3], "msg": r[4]}
               for r in reversed(rows) if r[3] != "journal"]
        # change-journal notes show up on the archive charts as their own markers
        out += [{"id": -n["id"], "t": n["t"], "lvl": "note", "src": "journal", "msg": "✎ " + n["text"]}
                for n in self.notes(node, start, end)]
        return sorted(out, key=lambda e: e["t"])

    def beacon_uptime(self, name: str, since: int) -> float | None:
        v = self.q("SELECT AVG(up) FROM beacons WHERE name = ? AND ts >= ?", (name, since))[0][0]
        return round(v, 5) if v is not None else None

    def _restarts(self, node: str, since: int) -> list[dict]:
        rows = self.q("SELECT ts, msg FROM events WHERE node = ? AND src = 'docker' AND ts >= ? ORDER BY ts",
                      (node, since))
        out: dict[str, dict] = {}
        for ts, msg in rows:
            m = RESTART_RE.match(msg or "")
            if m:
                r = out.setdefault(m.group(1), {"name": m.group(1), "n": 0, "last": 0})
                r["n"] += 1
                r["last"] = ts * 1000
        return sorted(out.values(), key=lambda r: (-r["n"], r["name"]))

    def insights(self, node: str, boot_key: str | None) -> dict:
        """What KAGARI remembers: restart streaks, quiet nights, uptime records and calm stretches."""
        now = time.time()
        today = _midnight(now)
        out: dict = {"node": node, "restarts": [r for r in self._restarts(node, today) if r["n"] >= 2][:5]}
        night_end = today + 7 * 3600
        out["quiet_night"] = out["night_incidents"] = None
        if now >= night_end:
            covered = self.q("SELECT COUNT(*) FROM samples WHERE node = ? AND ts >= ? AND ts < ?",
                             (node, today, night_end))[0][0]
            if covered >= 300:  # watched at least 5 of the 7 night hours
                inc = self.q("SELECT COUNT(*) FROM events WHERE node = ? AND ts >= ? AND ts < ? "
                             "AND lvl IN ('warn', 'crit')", (node, today, night_end))[0][0]
                out["night_incidents"], out["quiet_night"] = inc, inc == 0
        prev = self.q("SELECT MAX(value), COUNT(*) FROM records WHERE node = ? AND key LIKE 'boot:%' AND key != ?",
                      (node, boot_key or ""))[0]
        out["uptime_best_prev"], out["boots"] = prev[0], prev[1]
        last_inc = self.q("SELECT MAX(ts) FROM events WHERE node = ? AND lvl IN ('warn', 'crit')", (node,))[0][0]
        first = self.q("SELECT MIN(ts) FROM samples WHERE node = ?", (node,))[0][0]
        since = last_inc or first
        out["calm_days"] = round((now - since) / 86400, 2) if since else None
        return out

    def report(self, node: str, days: int, beacons: list[str] | tuple = ()) -> dict:
        now = int(time.time())
        since = _midnight(now - (days - 1) * 86400) if days > 1 else now - 86400
        span_h = max(1, round((now - since) / 3600))
        out: dict = {"node": node, "days": days, "since": since * 1000, "summary": self.summary(node, span_h)}
        out["coverage"] = round(min(1.0, out["summary"]["minutes"] / max(1, (now - since) / 60)), 3)
        rows = self.q("SELECT date(ts, 'unixepoch', 'localtime') AS d, AVG(cpu), MAX(cpu_max), AVG(mem), MAX(hot), "
                      "SUM(rx) * 60, SUM(tx) * 60, COUNT(*) FROM samples WHERE node = ? AND ts >= ? "
                      "GROUP BY d ORDER BY d", (node, since))
        inc = dict(self.q("SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) FROM events "
                          "WHERE node = ? AND ts >= ? AND lvl IN ('warn', 'crit') GROUP BY d", (node, since)))
        out["daily"] = [{"day": r[0], "cpu_avg": round(r[1] or 0, 1), "cpu_max": r[2], "mem_avg": round(r[3] or 0, 1),
                         "hot": round(r[4], 1) if r[4] is not None else None, "rx": round(r[5] or 0),
                         "tx": round(r[6] or 0), "minutes": r[7], "incidents": inc.get(r[0], 0)} for r in rows]
        hot = [d for d in out["daily"] if d["hot"] is not None]
        out["hottest_day"] = max(hot, key=lambda d: d["hot"]) if hot else None
        out["busiest_day"] = max(out["daily"], key=lambda d: d["cpu_avg"]) if out["daily"] else None
        out["containers"] = [
            {"name": r[0], "cpu_h": round(r[1] / 3600, 2), "mem_max": r[2]}
            for r in self.q("SELECT name, SUM(cpu_s), MAX(mem_max) FROM ct_usage WHERE node = ? AND day >= ? "
                            "GROUP BY name ORDER BY 2 DESC LIMIT 8", (node, _day(since)))]
        series: dict[str, list] = {}
        for mount, used, total in self.q("SELECT mount, used, total FROM disks WHERE node = ? AND ts >= ? "
                                         "ORDER BY mount, ts", (node, since)):
            series.setdefault(mount, []).append((used, total))
        out["disks"] = sorted(({"mount": m, "delta": p[-1][0] - p[0][0], "used": p[-1][0], "total": p[-1][1]}
                               for m, p in series.items() if len(p) > 1), key=lambda d: -d["delta"])[:6]
        out["restarts"] = self._restarts(node, since)[:8]
        lv = dict(self.q("SELECT lvl, COUNT(*) FROM events WHERE node = ? AND ts >= ? AND lvl IN ('warn', 'crit') "
                         "GROUP BY lvl", (node, since)))
        out["incidents"] = {"warn": lv.get("warn", 0), "crit": lv.get("crit", 0)}
        out["beacons"] = [{"name": b, "uptime": self.beacon_uptime(b, since)} for b in beacons]
        return out
