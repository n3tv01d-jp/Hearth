"""Host telemetry collectors — shared by the hub and by agents.

Everything here reads the *local* machine: CPU / memory / network / thermal /
disk usage via psutil and /proc, containers via the Docker socket, and drive
health via smartctl. `Collector.collect()` returns one JSON-able snapshot.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import psutil

try:
    import docker  # type: ignore
except ImportError:  # pragma: no cover
    docker = None

log = logging.getLogger("hearth")

INTERVAL = float(os.getenv("HEARTH_INTERVAL", "1"))
HISTORY = int(os.getenv("HEARTH_HISTORY", "120"))
HOSTFS = os.getenv("HEARTH_HOSTFS", "/hostfs").rstrip("/")
EXCLUDE = tuple(p.strip() for p in os.getenv("HEARTH_IFACE_EXCLUDE", "lo,veth").split(",") if p.strip())
DOCKER_HOST = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")
SMART_INTERVAL = float(os.getenv("HEARTH_SMART_INTERVAL", "600"))
# Opt-in per machine: lets the hub start / stop / restart containers and read their logs here.
ALLOW_CONTROL = os.getenv("HEARTH_ALLOW_CONTROL", "").strip().lower() in ("1", "true", "yes", "on")
BOOT_ID = secrets.token_hex(4)


def now_ms() -> int:
    return int(time.time() * 1000)


def read_text(*paths: str) -> str | None:
    for p in paths:
        try:
            return Path(p).read_text().strip()
        except OSError:
            continue
    return None


def node_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name or "").strip("-.")[:48] or "node"


def _self_container() -> str | None:
    """Short id of the container HEARTH itself runs in (so it never stops itself)."""
    for path in ("/proc/self/mountinfo", "/proc/self/cgroup"):
        m = re.search(r"(?:containers/|docker[-/])([0-9a-f]{64})", read_text(path) or "")
        if m:
            return m.group(1)[:12]
    return None


SELF_CONTAINER = _self_container()


# ───────────────────────────── host info ─────────────────────────────
def _host_os() -> str:
    for line in (read_text(f"{HOSTFS}/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return "Linux"


def _cpu_model() -> str:
    for line in (read_text("/proc/cpuinfo") or "").splitlines():
        if line.lower().startswith(("model name", "hardware", "cpu model")):
            return re.sub(r"\s+", " ", line.split(":", 1)[1]).strip()
    return os.uname().machine


HOST = {
    "name": os.getenv("HEARTH_NODE") or os.getenv("HEARTH_HOSTNAME") or read_text(f"{HOSTFS}/etc/hostname")
    or socket.gethostname(),
    "os": _host_os(),
    "kernel": os.uname().release,
    "arch": os.uname().machine,
    "cpu": _cpu_model(),
    "threads": psutil.cpu_count() or 1,
    "cores": psutil.cpu_count(logical=False) or psutil.cpu_count() or 1,
    "boot_time": int(psutil.boot_time()),
}
NODE_ID = node_id(HOST["name"])


# ───────────────────────────── event log ─────────────────────────────
class EventLog:
    """Ring buffer of notable events. `listeners` are called for each new event."""

    def __init__(self) -> None:
        self.items: deque[dict] = deque(maxlen=200)
        self.seq = 0
        self.lock = threading.Lock()
        self.listeners: list = []

    def add(self, lvl: str, src: str, msg: str, node: str | None = None, rec: bool = False) -> dict:
        with self.lock:
            self.seq += 1
            e = {"id": self.seq, "t": now_ms(), "lvl": lvl, "src": src, "msg": msg, "node": node or NODE_ID}
            if rec:
                e["rec"] = True
            self.items.append(e)
        for fn in self.listeners:
            try:
                fn(e)
            except Exception:  # noqa: BLE001
                log.exception("event listener failed")
        return e

    def seed(self, items: list[dict]) -> None:
        with self.lock:
            self.items.extend(items)
            self.seq = max([self.seq] + [i["id"] for i in items])

    def recent(self, n: int = 40) -> list[dict]:
        with self.lock:
            return list(self.items)[-n:]


EVENTS = EventLog()


# ───────────────────────────── network ─────────────────────────────
def iface_kind(name: str) -> str:
    if name.startswith(("docker", "br-", "virbr", "cni", "podman", "lxc", "lxd")):
        return "bridge"
    if name.startswith(("tun", "tap", "wg", "tailscale", "zt", "ppp", "nebula")):
        return "vpn"
    if name.startswith(("wl", "wifi", "ath")):
        return "wifi"
    return "phys"


def default_iface() -> str | None:
    best: tuple[str, int] | None = None
    try:
        with open("/proc/net/route") as f:
            next(f, None)
            for line in f:
                p = line.split()
                if len(p) > 6 and p[1] == "00000000" and int(p[3], 16) & 2:
                    if best is None or int(p[6]) < best[1]:
                        best = (p[0], int(p[6]))
    except OSError:
        pass
    return best[0] if best else None


class NetCollector:
    def __init__(self) -> None:
        self.prev: dict = {}
        self.prev_t: float | None = None
        self.up: dict[str, bool] = {}

    def sample(self) -> dict:
        now = time.monotonic()
        counters = psutil.net_io_counters(pernic=True)
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        dt = (now - self.prev_t) if self.prev_t else None
        primary = default_iface()
        out = []
        for name, c in counters.items():
            if name.startswith(EXCLUDE):
                continue
            p = self.prev.get(name)
            rx = tx = 0.0
            if p is not None and dt:
                rx = max(0.0, (c.bytes_recv - p.bytes_recv) / dt)
                tx = max(0.0, (c.bytes_sent - p.bytes_sent) / dt)
            st = stats.get(name)
            up = bool(st and st.isup)
            kind = iface_kind(name)
            fam = addrs.get(name, [])
            was = self.up.get(name)
            if was is not None and was != up and kind != "bridge":
                EVENTS.add("ok" if up else "warn", "net", f"{name} link {'UP' if up else 'DOWN'}", rec=up)
            self.up[name] = up
            out.append({
                "name": name, "kind": kind, "up": up,
                "ip": next((a.address for a in fam if a.family == socket.AF_INET), None),
                "mac": next((a.address for a in fam if a.family == psutil.AF_LINK), None),
                "speed": st.speed if st else 0, "mtu": st.mtu if st else 0,
                "rx": round(rx), "tx": round(tx), "total_rx": c.bytes_recv, "total_tx": c.bytes_sent,
                "err": c.errin + c.errout, "drop": c.dropin + c.dropout,
            })
        self.prev, self.prev_t = counters, now
        out.sort(key=lambda i: (i["name"] != primary, i["kind"] == "bridge", -(i["total_rx"] + i["total_tx"])))
        return {"primary": primary, "ifaces": out}


TCP_STATES = {
    "01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
    "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING",
}
CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _hex_ip(h: str) -> str:
    b = bytes.fromhex(h)
    if len(b) == 4:
        return socket.inet_ntop(socket.AF_INET, b[::-1])
    ip = ipaddress.IPv6Address(b"".join(b[i:i + 4][::-1] for i in range(0, 16, 4)))
    return str(ip.ipv4_mapped) if ip.ipv4_mapped else str(ip)


def ip_scope(ip: str) -> str:
    a = ipaddress.ip_address(ip)
    if a.is_loopback:
        return "loop"
    if a.version == 4 and a in CGNAT:
        return "vpn"
    if a.is_private or a.is_link_local:
        return "lan"
    return "wan"


def connections() -> dict:
    states: dict[str, int] = {}
    peers: dict[str, dict] = {}
    listen: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f, None)
                for line in f:
                    p = line.split()
                    if len(p) < 4:
                        continue
                    st = TCP_STATES.get(p[3], "OTHER")
                    states[st] = states.get(st, 0) + 1
                    if st == "LISTEN":
                        listen.add(int(p[1].rsplit(":", 1)[1], 16))
                    elif st == "ESTABLISHED":
                        rip, rport = p[2].rsplit(":", 1)
                        ip = _hex_ip(rip)
                        sc = ip_scope(ip)
                        if sc == "loop":
                            continue
                        e = peers.setdefault(ip, {"ip": ip, "n": 0, "scope": sc, "ports": set()})
                        e["n"] += 1
                        e["ports"].add(int(rport, 16))
        except OSError:
            continue
    udp = 0
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                udp += max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass
    top = sorted(peers.values(), key=lambda e: (-e["n"], e["ip"]))[:14]
    for e in top:
        e["ports"] = sorted(e["ports"])[:4]
    return {"states": states, "established": states.get("ESTABLISHED", 0), "udp": udp,
            "listen": sorted(listen)[:48], "peers": top, "peer_count": len(peers)}


def arp_table() -> list[dict]:
    out = []
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        p = line.split()
        if len(p) >= 6 and p[2] != "0x0" and p[3] != "00:00:00:00:00:00" and not p[5].startswith("veth"):
            out.append({"ip": p[0], "mac": p[3], "dev": p[5]})
    out.sort(key=lambda e: ipaddress.ip_address(e["ip"]))
    return out


# ───────────────────────────── thermal ─────────────────────────────
CPU_CHIPS = ("k10temp", "coretemp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal", "x86_pkg_temp")


class ThermalCollector:
    def __init__(self) -> None:
        self.level: dict[str, str] = {}

    def sample(self) -> tuple[list[dict], list[dict], dict | None]:
        temps: list[dict] = []
        try:
            raw = psutil.sensors_temperatures()
        except Exception:  # noqa: BLE001
            raw = {}
        for chip, entries in raw.items():
            counts: dict[str, int] = {}
            for e in entries:
                counts[e.label] = counts.get(e.label, 0) + 1
            seen: dict[str, int] = {}
            for i, e in enumerate(entries):
                if e.current is None or not (0 < e.current < 150):
                    continue
                if e.label and counts[e.label] > 1:
                    seen[e.label] = seen.get(e.label, 0) + 1
                    label = f"{e.label} {seen[e.label]}"
                else:
                    label = e.label or (chip if len(entries) == 1 else f"temp{i + 1}")
                crit = e.critical if e.critical and e.critical > 40 else 95.0
                warn = e.high if e.high and 40 < e.high < crit else crit - 10
                sid = f"{chip}/{label}/{i}"
                c = round(e.current, 1)
                lvl = "crit" if c >= crit else "warn" if c >= warn else "ok"
                prev = self.level.get(sid)
                if prev is not None and prev != lvl:
                    if lvl == "crit":
                        EVENTS.add("crit", "thermal", f"{chip} {label} CRITICAL {c}°C")
                    elif lvl == "warn" and prev == "ok":
                        EVENTS.add("warn", "thermal", f"{chip} {label} high {c}°C")
                    elif lvl == "ok":
                        EVENTS.add("ok", "thermal", f"{chip} {label} cooled to {c}°C", rec=True)
                self.level[sid] = lvl
                temps.append({"id": sid, "chip": chip, "label": label, "c": c,
                              "warn": round(warn, 1), "crit": round(crit, 1), "lvl": lvl})
        fans = []
        try:
            for chip, entries in (psutil.sensors_fans() or {}).items():
                for i, e in enumerate(entries):
                    fans.append({"chip": chip, "label": e.label or f"fan{i + 1}", "rpm": e.current})
        except Exception:  # noqa: BLE001
            pass
        pool = [t for t in temps if t["chip"] in CPU_CHIPS] or temps
        hot = max(pool, key=lambda t: t["c"]) if pool else None
        return temps, fans, hot


# ───────────────────────────── storage ─────────────────────────────
REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "bcachefs", "ntfs", "ntfs3", "exfat",
           "vfat", "fuseblk", "nfs", "nfs4", "cifs", "smb3", "fuse.mergerfs", "fuse.shfs"}
SKIP_MOUNTS = ("/var/lib/docker", "/var/lib/containers", "/snap", "/run", "/proc", "/sys", "/dev", "/boot/efi")
WHOLE_DISK = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")


def disks() -> list[dict]:
    hostfs = bool(HOSTFS) and os.path.isdir(HOSTFS)
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return []
    best: dict[str, dict] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mp, fs = parts[0], parts[1].replace("\\040", " "), parts[2]
        if fs not in REAL_FS:
            continue
        if hostfs:
            if mp != HOSTFS and not mp.startswith(HOSTFS + "/"):
                continue
            host_mp = mp[len(HOSTFS):] or "/"
        else:
            host_mp = mp
        if host_mp.startswith(SKIP_MOUNTS):
            continue
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            continue
        used = total - st.f_bfree * st.f_frsize
        cur = best.get(dev)
        if cur and len(cur["mount"]) <= len(host_mp):
            continue
        best[dev] = {"dev": dev.rsplit("/", 1)[-1], "mount": host_mp, "fs": fs, "total": total,
                     "used": used, "free": st.f_bavail * st.f_frsize, "pct": round(used / total * 100, 1)}
    return sorted(best.values(), key=lambda d: d["mount"])


class DiskIO:
    def __init__(self) -> None:
        self.prev: tuple[int, int] | None = None
        self.t: float | None = None

    def sample(self) -> dict:
        now = time.monotonic()
        try:
            per = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:  # noqa: BLE001
            per = {}
        r = sum(v.read_bytes for k, v in per.items() if WHOLE_DISK.match(k))
        w = sum(v.write_bytes for k, v in per.items() if WHOLE_DISK.match(k))
        out = {"r": 0, "w": 0}
        if self.prev and self.t:
            dt = now - self.t
            out = {"r": round(max(0, r - self.prev[0]) / dt), "w": round(max(0, w - self.prev[1]) / dt)}
        self.prev, self.t = (r, w), now
        return out


# ───────────────────────────── S.M.A.R.T. ─────────────────────────────
class SmartCollector:
    """Polls smartctl in a background thread every HEARTH_SMART_INTERVAL seconds."""

    def __init__(self) -> None:
        self.data: dict = {"available": False, "reason": "scanning…", "drives": []}
        self.last = 0.0
        self.busy = False
        self.level: dict[str, str] = {}

    def maybe_refresh(self) -> None:
        if self.busy or (self.last and time.monotonic() - self.last < SMART_INTERVAL):
            return
        self.busy = True
        threading.Thread(target=self._run, name="smart", daemon=True).start()

    def _run(self) -> None:
        try:
            self.data = self._scan()
            for d in self.data["drives"]:
                key, lvl = d.get("serial") or d["dev"], d.get("lvl", "ok")
                prev = self.level.get(key)
                if prev is not None and prev != lvl and lvl in ("warn", "crit"):
                    EVENTS.add(lvl, "smart", f"{d.get('model', d['dev'])} health {lvl.upper()}: {d.get('issue') or '?'}")
                self.level[key] = lvl
        except Exception as e:  # noqa: BLE001
            self.data = {"available": False, "reason": str(e)[:200], "drives": []}
        finally:
            self.last = time.monotonic()
            self.busy = False

    @staticmethod
    def _json(cmd: list[str]) -> dict:
        p = subprocess.run(cmd, capture_output=True, timeout=30, text=True)  # noqa: S603
        try:
            return json.loads(p.stdout or "{}")
        except ValueError:
            return {}

    def _scan(self) -> dict:
        exe = shutil.which("smartctl")
        if not exe:
            return {"available": False, "reason": "smartctl not installed", "drives": []}
        devs = self._json([exe, "--scan", "-j"]).get("devices") or []
        if not devs:
            return {"available": False, "reason": "no drives passed into the container", "drives": []}
        drives = []
        for d in devs:
            name, typ = d.get("name"), d.get("type")
            if not name:
                continue
            cmd = [exe, "-a", "-j"] + (["-d", typ] if typ else []) + [name]
            drives.append(self.shape(name, self._json(cmd)))
        return {"available": True, "reason": None, "drives": drives}

    @staticmethod
    def shape(name: str, j: dict) -> dict:
        dev = name.rsplit("/", 1)[-1]
        errors = [m.get("string", "") for m in ((j.get("smartctl") or {}).get("messages") or [])
                  if m.get("severity") == "error"]
        if not (j.get("model_name") or j.get("model_family")):
            return {"dev": dev, "lvl": "unknown", "error": (errors[0] if errors else "no SMART data")[:160]}
        nv = j.get("nvme_smart_health_information_log") or {}
        ata = {a.get("id"): a for a in ((j.get("ata_smart_attributes") or {}).get("table") or [])}

        def raw(i: int):
            a = ata.get(i)
            return (a.get("raw") or {}).get("value") if a else None

        passed = (j.get("smart_status") or {}).get("passed")
        wear = spare = media = realloc = pending = written = None
        crit_warn = 0
        if nv:
            kind = "nvme"
            wear = nv.get("percentage_used")
            spare = nv.get("available_spare")
            media = nv.get("media_errors")
            crit_warn = nv.get("critical_warning") or 0
            if nv.get("data_units_written") is not None:
                written = nv["data_units_written"] * 512_000
            spare_min = nv.get("available_spare_threshold") or 10
        else:
            kind = "hdd" if (j.get("rotation_rate") or 0) > 0 else "ssd"
            for i in (177, 231, 233, 202):  # normalised value counts down from 100 = life remaining
                a = ata.get(i)
                if a and isinstance(a.get("value"), int) and 0 < a["value"] <= 100:
                    wear = 100 - a["value"]
                    break
            realloc, pending = raw(5), raw(197)
            lbas = raw(241)
            if lbas:
                written = lbas * (j.get("logical_block_size") or 512)
            spare_min = 0
        issues = []
        if passed is False:
            issues.append("SMART self-assessment FAILED")
        if crit_warn:
            issues.append(f"critical warning 0x{crit_warn:02x}")
        if spare is not None and spare <= spare_min:
            issues.append(f"spare blocks {spare}%")
        if wear is not None and wear >= 70:
            issues.append(f"{wear}% worn")
        if media:
            issues.append(f"{media} media errors")
        if realloc:
            issues.append(f"{realloc} reallocated sectors")
        if pending:
            issues.append(f"{pending} pending sectors")
        crit = passed is False or crit_warn or (wear is not None and wear >= 90) or (spare is not None and spare <= spare_min)
        lvl = "crit" if crit else "warn" if issues else "ok"
        return {
            "dev": dev, "kind": kind, "model": j.get("model_name") or j.get("model_family"),
            "serial": (j.get("serial_number") or "")[-6:], "capacity": (j.get("user_capacity") or {}).get("bytes"),
            "passed": passed, "temp": (j.get("temperature") or {}).get("current"),
            "hours": (j.get("power_on_time") or {}).get("hours"), "wear": wear, "spare": spare,
            "media": media, "realloc": realloc, "pending": pending, "written": written,
            "lvl": lvl, "issue": ", ".join(issues) or None,
        }


# ───────────────────────────── docker ─────────────────────────────
DOCKER_ACTIONS = {
    "stop": ("info", "■ stopped"), "restart": ("warn", "↻ restarted"), "oom": ("crit", "✖ OUT OF MEMORY"),
    "create": ("info", "+ created"), "destroy": ("info", "− removed"), "pause": ("info", "‖ paused"),
    "unpause": ("info", "▶ resumed"),
}
RECOVERY_WINDOW = 1800  # a start/healthy within 30 min of a failure counts as a recovery
GATE_KEYS = ("name", "url", "port", "scheme", "path", "group", "icon", "hide", "description")
TRAEFIK_HOST = re.compile(r"Host\(\s*`([^`]+)`")


def gate_labels(labels: dict) -> dict:
    """GATE hints from container labels: hearth.gate.* plus domains from Traefik / caddy-docker-proxy."""
    out = {}
    for k in GATE_KEYS:
        v = labels.get(f"hearth.gate.{k}")
        if v:
            out[k] = str(v)[:300]
    domains = []
    for k, v in labels.items():
        if k.startswith("traefik.http.routers.") and k.endswith(".rule"):
            domains += TRAEFIK_HOST.findall(v or "")
        elif k == "caddy" or re.fullmatch(r"caddy_\d+", k):
            domains += [d.strip() for d in re.split(r"[\s,]+", v or "") if "." in d and "*" not in d]
    domains = [d.split("://")[-1].split("/")[0] for d in domains]
    if domains:
        out["domains"] = list(dict.fromkeys(domains))[:4]
    return out


class DockerWatch:
    def __init__(self) -> None:
        self.online = False
        self.version: str | None = None
        self.error: str | None = None
        self.containers: list[dict] = []
        self.stats: dict[str, dict] = {}
        self.streams: set[str] = set()
        self.running: set[str] = set()
        self.bad_at: dict[str, float] = {}
        self.lock = threading.Lock()

    def start(self) -> None:
        if docker is None:
            self.error = "docker SDK not installed"
            return
        threading.Thread(target=self._list_loop, name="docker-list", daemon=True).start()
        threading.Thread(target=self._event_loop, name="docker-events", daemon=True).start()

    @staticmethod
    def _api(timeout: int | None = 10):
        return docker.APIClient(base_url=DOCKER_HOST, timeout=timeout, version="auto")

    @staticmethod
    def _shape(r: dict) -> dict:
        status = r.get("Status") or ""
        health = ("unhealthy" if "(unhealthy)" in status else "healthy" if "(healthy)" in status
                  else "starting" if "health: starting" in status else None)
        labels = r.get("Labels") or {}
        image = r.get("Image") or ""
        if image.startswith("sha256:"):
            image = image[7:19]
        ports = sorted({p["PublicPort"] for p in (r.get("Ports") or []) if p.get("PublicPort")})
        # published TCP port -> container port, for GATE's guess at which port is the web UI
        pmap = sorted({(p["PublicPort"], p.get("PrivatePort") or 0) for p in (r.get("Ports") or [])
                       if p.get("PublicPort") and p.get("Type", "tcp") == "tcp"})
        out = {
            "id": r["Id"][:12], "name": ((r.get("Names") or ["/?"])[0]).lstrip("/"), "image": image,
            "state": r.get("State") or "unknown", "status": status, "health": health, "created": r.get("Created"),
            "project": labels.get("com.docker.compose.project"), "ports": ports[:6],
            "service": labels.get("com.docker.compose.service"),
        }
        if pmap:
            out["pmap"] = [list(p) for p in pmap[:12]]
        gate = gate_labels(labels)
        if gate:
            out["gate"] = gate
        return out

    def _list_loop(self) -> None:
        api = None
        was_online: bool | None = None
        while True:
            try:
                if api is None:
                    api = self._api()
                    self.version = api.version().get("Version")
                items = [self._shape(r) for r in api.containers(all=True)]
                running = {i["id"] for i in items if i["state"] == "running"}
                with self.lock:
                    self.containers = items
                    self.running = running
                    for k in list(self.stats):
                        if k not in running:
                            del self.stats[k]
                for cid in running - self.streams:
                    self.streams.add(cid)
                    threading.Thread(target=self._stream, args=(cid,), name=f"stats-{cid}", daemon=True).start()
                if was_online is False:
                    EVENTS.add("ok", "docker", "engine link restored", rec=True)
                self.online, self.error, was_online = True, None, True
                time.sleep(2)
            except Exception as e:  # noqa: BLE001
                if was_online is not False:
                    EVENTS.add("crit", "docker", f"engine unreachable ({e.__class__.__name__})")
                self.online, self.error, was_online = False, str(e)[:240], False
                try:
                    if api is not None:
                        api.close()
                except Exception:  # noqa: BLE001
                    pass
                api = None
                time.sleep(8)

    def _stream(self, cid: str) -> None:
        api = None
        prev_net: tuple[float, int, int] | None = None
        try:
            api = self._api(timeout=None)
            for s in api.stats(cid, stream=True, decode=True):
                if cid not in self.running:
                    break
                cpu, pre = s.get("cpu_stats") or {}, s.get("precpu_stats") or {}
                cd = (cpu.get("cpu_usage") or {}).get("total_usage", 0) - (pre.get("cpu_usage") or {}).get("total_usage", 0)
                sd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
                ncpu = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
                pct = (cd / sd) * ncpu * 100 if sd > 0 and cd >= 0 else 0.0
                mem = s.get("memory_stats") or {}
                mst = mem.get("stats") or {}
                used = max(0, mem.get("usage", 0) - mst.get("inactive_file", mst.get("total_inactive_file", 0)))
                nets = s.get("networks") or {}
                rx = sum(n.get("rx_bytes", 0) for n in nets.values())
                tx = sum(n.get("tx_bytes", 0) for n in nets.values())
                t = time.monotonic()
                rrx = rtx = 0.0
                if prev_net and t > prev_net[0]:
                    rrx = max(0, rx - prev_net[1]) / (t - prev_net[0])
                    rtx = max(0, tx - prev_net[2]) / (t - prev_net[0])
                prev_net = (t, rx, tx)
                with self.lock:
                    self.stats[cid] = {"cpu": round(pct, 2), "mem": used, "mem_limit": mem.get("limit", 0),
                                       "rx": round(rrx), "tx": round(rtx), "pids": (s.get("pids_stats") or {}).get("current")}
        except Exception as e:  # noqa: BLE001
            log.debug("stats stream %s ended: %s", cid, e)
        finally:
            self.streams.discard(cid)
            if api is not None:
                try:
                    api.close()
                except Exception:  # noqa: BLE001
                    pass

    def _recovered(self, name: str) -> bool:
        t = self.bad_at.pop(name, None)
        return t is not None and time.monotonic() - t < RECOVERY_WINDOW

    def _event_loop(self) -> None:
        while True:
            api = None
            try:
                api = self._api(timeout=None)
                for ev in api.events(decode=True, filters={"type": "container"}):
                    action = ev.get("Action") or ev.get("status") or ""
                    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
                    name = attrs.get("name", "?")
                    if action == "die":
                        code = attrs.get("exitCode", "?")
                        clean = code in ("0", "143")
                        if not clean:
                            self.bad_at[name] = time.monotonic()
                        EVENTS.add("info" if clean else "warn", "docker", f"{name} ✖ exited ({code})")
                    elif action == "start":
                        rec = self._recovered(name)
                        EVENTS.add("ok", "docker", f"{name} ▶ {'recovered' if rec else 'started'}", rec=rec)
                    elif action.startswith("health_status"):
                        st = action.split(":", 1)[-1].strip()
                        if st == "healthy":
                            rec = self._recovered(name)
                            EVENTS.add("ok", "docker", f"{name} ♥ healthy{' again' if rec else ''}", rec=rec)
                        else:
                            self.bad_at[name] = time.monotonic()
                            EVENTS.add("crit", "docker", f"{name} ♥ {st}")
                    elif action in DOCKER_ACTIONS:
                        lvl, verb = DOCKER_ACTIONS[action]
                        if action == "oom":
                            self.bad_at[name] = time.monotonic()
                        EVENTS.add(lvl, "docker", f"{name} {verb}")
            except Exception:  # noqa: BLE001
                pass
            finally:
                if api is not None:
                    try:
                        api.close()
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(10)

    def snapshot(self) -> dict:
        with self.lock:
            items = [dict(c, **(self.stats.get(c["id"]) or {})) for c in self.containers]
        if SELF_CONTAINER:
            for c in items:
                if c["id"] == SELF_CONTAINER:
                    c["self"] = True
        return {"online": self.online, "version": self.version, "error": self.error, "containers": items,
                "control": ALLOW_CONTROL,
                "running": sum(1 for c in items if c["state"] == "running"), "total": len(items)}


# ───────────────────────────── snapshot ─────────────────────────────
class Collector:
    def __init__(self) -> None:
        self.net = NetCollector()
        self.therm = ThermalCollector()
        self.dio = DiskIO()
        self.smart = SmartCollector()
        self.dock = DockerWatch()
        from . import arsenal  # late import: arsenal builds on this module
        self.arsenal = arsenal.get()
        self.tick = 0
        self._conns: dict = {}
        self._arp: list = []
        self._disks: list = []
        self._cpu_level = "ok"
        psutil.cpu_percent(percpu=True)

    def start(self) -> None:
        self.dock.start()
        self.arsenal.start()

    def collect(self) -> dict:
        t = self.tick
        self.tick += 1
        cores = psutil.cpu_percent(percpu=True)
        total = round(sum(cores) / len(cores), 1) if cores else 0.0
        try:
            freq = psutil.cpu_freq()
            mhz = round(freq.current) if freq else None
        except Exception:  # noqa: BLE001
            mhz = None
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        lvl = "crit" if total >= 95 else "ok" if total < 80 else self._cpu_level
        if lvl != self._cpu_level:
            EVENTS.add("warn" if lvl == "crit" else "ok", "cpu",
                       f"CPU load {'saturated' if lvl == 'crit' else 'back to normal'} {total}%")
            self._cpu_level = lvl
        if t % 2 == 0:
            self._conns = connections()
        if t % 5 == 0:
            self._arp = arp_table()
        if t % 10 == 0:
            self._disks = disks()
        self.smart.maybe_refresh()
        net = self.net.sample()
        net["conns"] = self._conns
        net["lan"] = self._arp
        temps, fans, hot = self.therm.sample()
        return {
            "t": now_ms(), "node": NODE_ID, "boot": BOOT_ID,
            "host": dict(HOST, uptime=round(time.time() - psutil.boot_time())),
            "cpu": {"total": total, "cores": [round(c) for c in cores], "mhz": mhz,
                    "load": [round(x, 2) for x in os.getloadavg()]},
            "mem": {"pct": round(vm.percent, 1), "used": vm.total - vm.available, "total": vm.total,
                    "swap_pct": round(sw.percent, 1), "swap_used": sw.used, "swap_total": sw.total},
            "net": net, "temps": temps, "fans": fans, "hot": hot,
            "disks": [dict(d) for d in self._disks], "diskio": self.dio.sample(),
            "smart": self.smart.data, "docker": self.dock.snapshot(), "arsenal": self.arsenal.snapshot(),
            "events": EVENTS.recent(),
        }
