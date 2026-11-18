"""Beacons: reachability checks for devices that can't run an agent (routers, printers, IoT, web apps).

HEARTH_CHECKS="router=192.168.1.1, printer=tcp:192.168.1.50:631, ha=http://192.168.1.20:8123"

  name=HOST  or  name=ping:HOST   ICMP ping
  name=tcp:HOST:PORT              TCP connect
  name=http(s)://…                HTTP GET. Any response below 500 counts as up, and self-signed
                                  certificates are accepted.

A beacon is marked DOWN after two failed probes in a row, which keeps one dropped packet from paging you.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .collect import EVENTS, node_id, now_ms

log = logging.getLogger("hearth")

INTERVAL = max(5.0, float(os.getenv("HEARTH_CHECK_INTERVAL", "30")))
TIMEOUT = max(0.5, float(os.getenv("HEARTH_CHECK_TIMEOUT", "3")))
DOWN_AFTER = 2
HIST = 60
PING = shutil.which("ping")
RTT = re.compile(r"time[=<]\s*([\d.]+)\s*ms")
HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]-]+$")
_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


def _host(h: str) -> str:
    h = h.strip()
    if not h or h.startswith("-") or not HOST_RE.match(h):
        raise ValueError(f"bad host {h!r}")
    return h.strip("[]")


class Beacon:
    def __init__(self, name: str, spec: str) -> None:
        self.name = node_id(name)
        self.port: int | None = None
        self.url: str | None = None
        if spec.startswith(("http://", "https://")):
            u = urllib.parse.urlsplit(spec)
            if not u.hostname:
                raise ValueError("URL has no host")
            self.kind, self.url, self.host = "http", spec, u.hostname
            self.target = urllib.parse.urlunsplit(u._replace(netloc=u.netloc.rpartition("@")[2]))
        elif spec.startswith("tcp:"):
            host, _, port = spec[4:].rpartition(":")
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise ValueError("expected tcp:HOST:PORT")
            self.kind, self.host, self.port = "tcp", _host(host), int(port)
            self.target = f"{host}:{port}"
        else:
            self.kind = "ping"
            self.host = _host(spec[5:] if spec.startswith("ping:") else spec)
            self.target = self.host
        self.up: bool | None = None
        self.fails = 0
        self.rtt: float | None = None
        self.error: str | None = None
        self.since: int | None = None
        self.checked: int | None = None
        self.uptime: float | None = None
        self.hist: deque = deque(maxlen=HIST)  # rtt in ms, None (up, no timing) or -1 (probe failed)

    def probe(self) -> tuple[bool, float | None, str | None]:
        t0 = time.perf_counter()
        try:
            if self.kind == "tcp":
                with socket.create_connection((self.host, self.port), timeout=TIMEOUT):
                    pass
            elif self.kind == "http":
                req = urllib.request.Request(self.url, headers={"User-Agent": "HEARTH-beacon"})
                try:
                    with urllib.request.urlopen(req, timeout=TIMEOUT, context=_TLS) as r:  # noqa: S310
                        code = r.status
                except urllib.error.HTTPError as e:
                    code = e.code
                if code >= 500:
                    return False, None, f"HTTP {code}"
            else:
                if not PING:
                    return False, None, "ping not installed"
                p = subprocess.run([PING, "-c", "1", "-W", str(max(1, round(TIMEOUT))), "-n", self.host],  # noqa: S603
                                   capture_output=True, text=True, timeout=TIMEOUT + 3)
                if p.returncode != 0:
                    return False, None, "no reply"
                m = RTT.search(p.stdout)
                return True, round(float(m.group(1)), 2) if m else None, None
        except Exception as e:  # noqa: BLE001
            reason = getattr(e, "reason", None) or e
            return False, None, (str(reason) or e.__class__.__name__)[:120]
        return True, round((time.perf_counter() - t0) * 1000, 2), None


class Beacons:
    def __init__(self, on_result=None) -> None:
        self.items: list[Beacon] = []
        self.on_result = on_result
        self.lock = threading.Lock()
        for part in (p.strip() for p in re.split(r"[,\n]+", os.getenv("HEARTH_CHECKS", ""))):
            if not part:
                continue
            name, sep, spec = part.partition("=")
            if not sep or "/" in name or ":" in name:  # bare host or URL: name it after the host
                name, spec = re.sub(r"^\w+:(//)?", "", part).split("/")[0].split(":")[0], part
            try:
                b = Beacon(name.strip(), spec.strip())
            except ValueError as e:
                log.warning("HEARTH_CHECKS: skipping %r: %s", name.strip(), e)
                continue
            if any(x.name == b.name for x in self.items):
                log.warning("HEARTH_CHECKS: duplicate name %r skipped", b.name)
                continue
            self.items.append(b)

    def start(self) -> None:
        if self.items:
            log.info("beacons: %s", ", ".join(f"{b.name} ({b.kind} {b.target})" for b in self.items))
            threading.Thread(target=self._loop, name="beacons", daemon=True).start()

    def _loop(self) -> None:
        with ThreadPoolExecutor(max_workers=min(8, len(self.items)), thread_name_prefix="beacon") as pool:
            while True:
                t0 = time.monotonic()
                for b, res in zip(self.items, pool.map(lambda b: b.probe(), self.items)):
                    self._apply(b, *res)
                time.sleep(max(1.0, INTERVAL - (time.monotonic() - t0)))

    def _apply(self, b: Beacon, ok: bool, rtt: float | None, err: str | None) -> None:
        now = now_ms()
        event = None
        with self.lock:
            b.checked, b.rtt, b.error = now, (rtt if ok else None), err
            b.hist.append(rtt if ok else -1)
            if ok:
                b.fails = 0
                if b.up is not True:
                    if b.up is False:
                        event = ("ok", f"{b.name} back up ({b.target}{f', {rtt:g} ms' if rtt is not None else ''})", True)
                    b.up, b.since = True, now
            else:
                b.fails += 1
                if b.fails >= DOWN_AFTER and b.up is not False:
                    event = ("crit", f"{b.name} DOWN: {b.target} ({err})", False)
                    b.up, b.since = False, now
        if event:
            EVENTS.add(event[0], "beacon", event[1], rec=event[2])
        if self.on_result:
            try:
                self.on_result(b.name, now // 1000, ok, rtt)
            except Exception:  # noqa: BLE001
                log.exception("beacon result hook failed")

    def snapshot(self) -> list[dict]:
        with self.lock:
            return [{"name": b.name, "kind": b.kind, "target": b.target, "host": b.host, "up": b.up, "rtt": b.rtt,
                     "error": b.error, "since": b.since, "checked": b.checked, "uptime": b.uptime,
                     "hist": list(b.hist)} for b in self.items]
