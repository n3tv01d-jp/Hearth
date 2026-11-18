"""SQLite persistence for the hub: per-node minute samples, disk usage, events."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
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
"""


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

    def prune(self, days: int) -> None:
        cut = int(time.time()) - days * 86400
        for table in ("samples", "disks", "events"):
            self.q(f"DELETE FROM {table} WHERE ts < ?", (cut,))  # noqa: S608 (fixed table names)

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
