"""Push alerts: forwards warn/crit events and recoveries to ntfy, Gotify, Discord, Slack or a JSON webhook.

HEARTH_NOTIFY is a comma-separated list of URLs. The service is worked out from the URL, or you can
force it with a prefix:

  ntfy+https://ntfy.sh/my-topic                 gotify+https://gotify.lan/message?token=…
  discord+https://discord.com/api/webhooks/…    slack+https://hooks.slack.com/services/…
  webhook+https://anything.lan/hook             (POSTs a JSON body)

user:pass@ in a URL is sent as HTTP basic auth. Duplicate alerts (same node, source and message,
ignoring numbers) are held back for HEARTH_NOTIFY_COOLDOWN seconds, and at most HEARTH_NOTIFY_BURST
alerts go out per minute.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

from . import __version__

log = logging.getLogger("hearth")

KINDS = ("ntfy", "gotify", "discord", "slack", "webhook")
RANK = {"info": 0, "ok": 0, "warn": 1, "crit": 2}
LEVEL_NAMES = {0: "info", 1: "warn", 2: "crit"}
UA = f"HEARTH/{__version__}"
COLORS = {"crit": 0xFF3B5C, "warn": 0xFFB63E, "rec": 0x5DFF9E, "info": 0x3EF3FF, "ok": 0x3EF3FF}


def _bool(v: str | None, default: bool) -> bool:
    if v is None or not v.strip():
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Target:
    def __init__(self, spec: str) -> None:
        kind, sep, rest = spec.partition("+")
        if sep and kind.lower() in KINDS:
            spec, kind = rest, kind.lower()
        else:
            kind = ""
        u = urllib.parse.urlsplit(spec)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("not an http(s) URL")
        self.auth: str | None = None
        host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        if u.username:
            cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
            self.auth = "Basic " + base64.b64encode(cred.encode()).decode()
            u = u._replace(netloc=host + (f":{u.port}" if u.port else ""))
        self.url = urllib.parse.urlunsplit(u)
        self.host = u.hostname.lower()
        if not kind:
            if self.host.endswith(("discord.com", "discordapp.com")) and "/webhooks/" in u.path:
                kind = "discord"
            elif self.host == "hooks.slack.com":
                kind = "slack"
            elif u.path.rstrip("/").endswith("/message") and "token=" in u.query:
                kind = "gotify"
            elif "ntfy" in self.host:
                kind = "ntfy"
            else:
                kind = "webhook"
        self.kind = kind
        self.last_ok: float | None = None
        self.last_error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.kind} · {self.host}"

    def payload(self, n: dict) -> tuple[bytes, dict]:
        h = {"User-Agent": UA}
        if self.auth:
            h["Authorization"] = self.auth
        lvl = "rec" if n["rec"] else n["lvl"]
        if self.kind == "ntfy":
            h["Title"] = n["title"].encode("latin-1", "replace").decode("latin-1")
            h["Priority"] = {"crit": "5", "warn": "4"}.get(lvl, "3")
            h["Tags"] = {"crit": "rotating_light", "warn": "warning", "rec": "white_check_mark"}.get(lvl, "fire")
            return n["body"].encode(), h
        h["Content-Type"] = "application/json"
        if self.kind == "gotify":
            body: dict = {"title": n["title"], "message": n["body"], "priority": {"crit": 8, "warn": 5}.get(lvl, 2)}
        elif self.kind == "discord":
            body = {"username": "KAGARI // HEARTH", "embeds": [{
                "title": n["title"], "description": n["body"], "color": COLORS.get(lvl, COLORS["info"]),
                "timestamp": datetime.fromtimestamp(n["t"] / 1000, tz=timezone.utc).isoformat()}]}
        elif self.kind == "slack":
            body = {"text": f"*{n['title']}*\n{n['body']}"}
        else:
            body = {"title": n["title"], "message": n["body"], "level": n["lvl"], "recovery": n["rec"],
                    "node": n["node"], "source": n["src"], "time": n["t"]}
        return json.dumps(body).encode(), h

    def send(self, n: dict) -> None:
        data, headers = self.payload(n)
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (http/https only, checked above)
                r.read(512)
        except urllib.error.HTTPError as e:
            self.last_error = f"HTTP {e.code}"
            raise
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{e.__class__.__name__}: {getattr(e, 'reason', e)}"[:160]
            raise
        self.last_ok, self.last_error = time.time(), None


class Notifier:
    def __init__(self) -> None:
        self.targets: list[Target] = []
        for i, spec in enumerate(s for s in re.split(r"[,\s]+", os.getenv("HEARTH_NOTIFY", "")) if s):
            try:
                self.targets.append(Target(spec))
            except ValueError as e:
                log.warning("HEARTH_NOTIFY entry #%d skipped: %s", i + 1, e)
        self.min_rank = RANK.get(os.getenv("HEARTH_NOTIFY_LEVEL", "warn").strip().lower(), 1)
        self.recoveries = _bool(os.getenv("HEARTH_NOTIFY_RECOVERY"), True)
        self.cooldown = float(os.getenv("HEARTH_NOTIFY_COOLDOWN", "300"))
        self.burst = max(1, int(os.getenv("HEARTH_NOTIFY_BURST", "10")))
        self.q: queue.Queue = queue.Queue(maxsize=256)
        self.recent: dict[str, float] = {}
        self.window: deque = deque()
        self.sent = self.suppressed = self.dropped = 0
        self.lock = threading.Lock()
        if self.targets:
            threading.Thread(target=self._worker, name="notify", daemon=True).start()
            log.info("push alerts → %s", ", ".join(t.label for t in self.targets))

    @staticmethod
    def shape(e: dict) -> dict:
        rec, lvl = bool(e.get("rec")), e.get("lvl", "info")
        label = "RECOVERED" if rec else {"crit": "CRITICAL", "warn": "WARNING"}.get(lvl, "INFO")
        return {"title": f"HEARTH {e.get('node')}: {label}", "body": f"[{e.get('src')}] {e.get('msg')}",
                "lvl": lvl, "rec": rec, "node": e.get("node"), "src": e.get("src"),
                "t": e.get("t") or int(time.time() * 1000)}

    def on_event(self, e: dict) -> None:
        """EVENTS listener. Runs on whichever thread logged the event, so it only filters and queues."""
        if not self.targets:
            return
        rec = bool(e.get("rec"))
        if rec:
            if not self.recoveries:
                return
        elif RANK.get(e.get("lvl", ""), 0) < self.min_rank:
            return
        key = f"{e.get('node')}|{e.get('src')}|{rec}|{re.sub(r'[0-9]+(?:[.][0-9]+)?', '#', str(e.get('msg')))}"
        now = time.monotonic()
        with self.lock:
            if now - self.recent.get(key, -1e12) < self.cooldown:
                self.suppressed += 1
                return
            self.recent[key] = now
            if len(self.recent) > 512:
                self.recent = {k: v for k, v in self.recent.items() if now - v < self.cooldown}
        try:
            self.q.put_nowait(e)
        except queue.Full:
            self.dropped += 1

    def _worker(self) -> None:
        while True:
            e = self.q.get()
            now = time.monotonic()
            while self.window and now - self.window[0] > 60:
                self.window.popleft()
            if len(self.window) >= self.burst:
                self.dropped += 1
                continue
            self.window.append(now)
            n = self.shape(e)
            for t in self.targets:
                try:
                    t.send(n)
                    self.sent += 1
                except Exception:  # noqa: BLE001
                    log.warning("push alert to %s failed: %s", t.label, t.last_error)

    def test(self, node: str) -> list[dict]:
        n = self.shape({"lvl": "warn", "src": "hearth", "node": node,
                        "msg": "Test alert from KAGARI. Push notifications are working! (ﾉ◕ヮ◕)ﾉ"})
        n["title"] = f"HEARTH {node}: TEST"
        out = []
        for t in self.targets:
            try:
                t.send(n)
                out.append({"target": t.label, "ok": True})
            except Exception:  # noqa: BLE001
                out.append({"target": t.label, "ok": False, "error": t.last_error})
        return out

    def status(self) -> dict:
        return {
            "targets": [{"kind": t.kind, "host": t.host, "ok": t.last_ok, "error": t.last_error} for t in self.targets],
            "level": LEVEL_NAMES.get(self.min_rank, "warn"), "recoveries": self.recoveries,
            "cooldown": self.cooldown, "burst": self.burst,
            "sent": self.sent, "suppressed": self.suppressed, "dropped": self.dropped,
        }
