"""GATE 転送門: the service portal.

Builds a launcher of every web UI in the fleet from data HEARTH already has:

  * containers with published ports (any machine in the fleet), plus Docker labels:
      hearth.gate.name / url / port / scheme / path / group / icon / description / hide
    and domains from Traefik (`Host(...)` router rules) and caddy-docker-proxy (`caddy` labels)
  * HTTP beacons from HEARTH_CHECKS
  * bookmarks: HEARTH_GATE_LINKS="Router=http://192.168.1.1, ISP=https://status.example" and links
    added from the page (stored in SQLite)

It also fetches favicons server-side (so the page never loads anything cross-origin), watches the
expiry date of every HTTPS certificate it links to, and sends Wake-on-LAN packets.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__
from .collect import EVENTS, node_id

log = logging.getLogger("hearth")

LOCAL = "__HOST__"  # placeholder for "the host the browser used to reach the hub"
UA = f"HEARTH-gate/{__version__}"
CERT_WARN_DAYS = float(os.getenv("HEARTH_CERT_WARN_DAYS", "14"))
CERT_CRIT_DAYS = 3.0
CERT_EVERY = 6 * 3600
CERT_RETRY = 3600
ICON_TTL, ICON_MISS_TTL, ICON_MAX = 12 * 3600, 1800, 256 * 1024
PAGE_MAX = 512 * 1024
WOL_COOLDOWN = 5.0

NOT_WEB = {21, 22, 23, 25, 53, 110, 123, 143, 389, 445, 465, 554, 587, 636, 993, 995, 1194, 1883, 1900, 1935,
           3306, 3389, 3478, 5353, 5432, 5900, 6379, 6881, 7359, 8554, 8883, 9100, 11211, 25565, 27017, 51413,
           51820}
WEB_PRIVATE = {80, 443, 3000, 5000, 5001, 5055, 7575, 8000, 8080, 8081, 8096, 8123, 8443, 8888, 9000, 9443}
TLS_PORTS = {443, 5001, 8443, 9443}
MAC_RE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.I)
LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r"""([a-zA-Z-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")

_TLS = ssl.create_default_context()
_TLS.check_hostname = False
_TLS.verify_mode = ssl.CERT_NONE


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _hostfmt(h: str) -> str:
    return f"[{h}]" if ":" in h and not h.startswith("[") else h


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False


def valid_url(u: str) -> bool:
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc) and len(u) <= 500


def pick_port(c: dict) -> int | None:
    """Best guess at the published port that serves the web UI."""
    pairs = [tuple(p) for p in c.get("pmap") or []] or [(p, 0) for p in c.get("ports") or []]
    pairs = [p for p in pairs if p[0] not in NOT_WEB and p[1] not in NOT_WEB]
    if not pairs:
        return None
    for want in (443, 80):
        for pub, priv in pairs:
            if priv == want:
                return pub
    for pub, priv in pairs:
        if priv in WEB_PRIVATE or pub in WEB_PRIVATE:
            return pub
    return min(p[0] for p in pairs)


def _scheme(c: dict, port: int) -> str:
    priv = next((p[1] for p in c.get("pmap") or [] if p[0] == port), 0)
    return "https" if port in TLS_PORTS or priv in TLS_PORTS else "http"


def ct_status(c: dict, online: bool) -> tuple[str, str]:
    if not online:
        return "lost", "machine offline"
    st, h = c.get("state"), c.get("health")
    if st == "running":
        if h == "unhealthy":
            return "crit", "unhealthy"
        if h == "starting":
            return "warn", "starting"
        return "up", "healthy" if h == "healthy" else "running"
    if st in ("restarting", "dead"):
        return "crit", st
    if st == "paused":
        return "warn", "paused"
    return "off", st or "stopped"


# ───────────── certificates: just enough DER to read the validity period ─────────────
def _tlv(b: bytes, i: int) -> tuple[int, int, int]:
    tag, ln = b[i], b[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(b[i:i + n], "big")
        i += n
    return tag, i, i + ln


def _der_time(tag: int, raw: bytes) -> float:
    s = raw.decode("ascii").rstrip("Z")
    if tag == 0x17:  # UTCTime YYMMDDHHMMSS
        y = int(s[:2])
        s = f"{1900 + y if y >= 50 else 2000 + y}{s[2:]}"
    return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()


def cert_info(der: bytes) -> dict:
    _, s, _ = _tlv(der, 0)          # Certificate
    _, s, e = _tlv(der, s)          # TBSCertificate
    fields = []
    while s < e and len(fields) < 7:
        tag, cs, ce = _tlv(der, s)
        fields.append((tag, cs, ce))
        s = ce
    if fields and fields[0][0] == 0xA0:  # explicit [0] version
        fields = fields[1:]
    _, issuer_s, issuer_e = fields[2]
    _, vs, ve = fields[3]
    _, subj_s, subj_e = fields[4]
    t1, a, b = _tlv(der, vs)
    t2, c, d = _tlv(der, b)
    return {"not_before": _der_time(t1, der[a:b]), "not_after": _der_time(t2, der[c:d]),
            "self_signed": der[issuer_s:issuer_e] == der[subj_s:subj_e]}


def fetch_cert(host: str, port: int, sni: str | None) -> dict:
    with socket.create_connection((host.strip("[]"), port), timeout=4) as sock:
        with _TLS.wrap_socket(sock, server_hostname=sni if sni and not _is_ip(sni) else None) as t:
            der = t.getpeercert(binary_form=True)
    if not der:
        raise ValueError("no certificate")
    return cert_info(der)


# ───────────── favicons ─────────────
def sniff(data: bytes) -> str | None:
    head = data[:512].lstrip()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"):
        return "image/x-icon"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[:5].lower() in (b"<?xml", b"<svg ", b"<svg>", b"<!--") and b"<svg" in data[:2048].lower():
        return "image/svg+xml"
    return None


def _get(url: str, limit: int) -> tuple[str, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,image/*,*/*;q=0.5"})
    with urllib.request.urlopen(req, timeout=4, context=_TLS) as r:  # noqa: S310 (http/https only)
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return r.geturl(), r.headers.get_content_type(), data


def _icon_links(page: str, base: str) -> list[str]:
    found = []
    for tag in LINK_TAG.findall(page[:200_000]):
        a = {k.lower(): v.strip("'\"") for k, v in ATTR.findall(tag)}
        rel, href = a.get("rel", "").lower(), a.get("href")
        if not href or "icon" not in rel or "mask-icon" in rel:
            continue
        size = max((int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))), default=0)
        score = 3000 if "apple-touch-icon" in rel else 2000 if href.lower().endswith(".svg") else size or 16
        found.append((score, urllib.parse.urljoin(base, href)))
    return [u for _, u in sorted(found, key=lambda x: -x[0])]


def find_icon(page_url: str, explicit: str | None = None) -> tuple[str, bytes] | None:
    candidates = [explicit] if explicit else []
    final = page_url
    if not explicit:
        try:
            final, ctype, data = _get(page_url, PAGE_MAX)
            if ctype in ("text/html", "application/xhtml+xml"):
                candidates += _icon_links(data.decode("utf-8", "replace"), final)
        except Exception:  # noqa: BLE001
            pass
        candidates.append(urllib.parse.urljoin(final, "/favicon.ico"))
    for u in candidates[:4]:
        try:
            if u.startswith("data:image/") and ";base64," in u[:80]:
                data = base64.b64decode(u.split(",", 1)[1])[:ICON_MAX]
            elif valid_url(u):
                data = _get(u, ICON_MAX)[2]
            else:
                continue
        except Exception:  # noqa: BLE001
            continue
        t = sniff(data)
        if t:
            return t, data
    return None


# ───────────── wake-on-LAN ─────────────
def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", mac)) * 16


def _broadcasts() -> set[str]:
    out = {"255.255.255.255"}
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith(("lo", "docker", "br-", "veth", "virbr", "tailscale", "wg", "tun")):
                continue
            for a in addrs:
                if a.family == socket.AF_INET and a.broadcast:
                    out.add(a.broadcast)
    except Exception:  # noqa: BLE001
        pass
    return out


def send_wol(mac: str, bcast: str | None = None) -> int:
    pkt, sent = magic_packet(mac), 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in ([bcast] if bcast else []) + sorted(_broadcasts()):
            for port in (9, 7):
                try:
                    s.sendto(pkt, (addr, port))
                    sent += 1
                except OSError:
                    pass
    return sent


class GateError(Exception):
    pass


class Gate:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.hosts = {}  # node id -> host name to use in links (HEARTH_GATE_HOSTS=nas=nas.lan,pi=pi.lan)
        for part in re.split(r"[,\s]+", os.getenv("HEARTH_GATE_HOSTS", "")):
            k, sep, v = part.partition("=")
            if sep and k and v:
                self.hosts[node_id(k)] = v.strip()
        self.env_links = []
        for part in (p.strip() for p in re.split(r",(?=[^,=]+=)|\n", os.getenv("HEARTH_GATE_LINKS", ""))):
            name, sep, url = part.partition("=")
            if sep and name.strip() and valid_url(url.strip()):
                self.env_links.append({"id": "env/" + node_id(name.strip()), "name": name.strip()[:60], "url": url.strip()})
            elif part:
                log.warning("HEARTH_GATE_LINKS: skipping %r (expected name=http(s)://…)", part)
        self.env_wol = []
        for part in re.split(r"[,\s]+", os.getenv("HEARTH_WOL", "")):
            name, sep, rest = part.partition("=")
            mac, _, bc = rest.partition("@")
            if sep and name and MAC_RE.match(mac):
                self.env_wol.append({"id": "wol/" + node_id(name), "name": name[:48], "mac": mac.lower(), "bcast": bc or None})
            elif part:
                log.warning("HEARTH_WOL: skipping %r (expected name=aa:bb:cc:dd:ee:ff[@broadcast])", part)
        self.lock = threading.Lock()
        self.seen: dict[str, tuple[str, str]] = {}  # service id -> (node, json of the container's linkable bits)
        self.macs: dict[str, str] = {}               # node id -> MAC of its primary interface
        self.targets: dict[str, dict] = {}            # entry id -> {"url", "icon", "probe": (host, port, sni)}
        self.certs: dict[str, dict] = {}              # "host:port" -> cert state
        self.cert_lvl: dict[str, str] = {}
        self.icons: dict[str, tuple[float, str | None, bytes | None]] = {}
        self.woke: dict[str, float] = {}
        st = hub.store
        if st:
            try:
                self.seen = st.gate_seen()
                self.macs = {k[4:]: v for k, v in st.kv_get("wol:").items()}
            except Exception:  # noqa: BLE001
                log.exception("gate: could not load remembered services")

    def start(self) -> None:
        threading.Thread(target=self._cert_loop, name="gate-certs", daemon=True).start()

    # ── building the service list ──
    def _url_for(self, node, c: dict) -> tuple[str | None, list[dict]]:
        g = c.get("gate") or {}
        host = _hostfmt(self.hosts.get(node.id) or (LOCAL if node.local else (node.addr or c.get("_addr") or LOCAL)))
        path = g.get("path") or ""
        if path and not path.startswith("/"):
            path = "/" + path
        extra = []
        if g.get("url") and valid_url(g["url"].replace(LOCAL, "h")):
            url = g["url"]
        elif g.get("domains"):
            url = f"{g.get('scheme') or 'https'}://{g['domains'][0]}{path}"
        else:
            port = int(g["port"]) if str(g.get("port", "")).isdigit() else pick_port(c)
            if not port:
                return None, []
            url = f"{g.get('scheme') or _scheme(c, port)}://{host}:{port}{path}"
        main_port = urllib.parse.urlsplit(url.replace(LOCAL, "h")).port
        for pub, _priv in [tuple(p) for p in c.get("pmap") or []]:
            if pub != main_port and pub not in NOT_WEB and len(extra) < 4 and all(x["port"] != pub for x in extra):
                extra.append({"port": pub, "url": f"{_scheme(c, pub)}://{host}:{pub}"})
        return url, extra

    def _container_entries(self, prune: list, remember: list) -> list[dict]:
        out = []
        nodes = list(self.hub.nodes.values())
        present: set[str] = set()
        for n in nodes:
            dk = (n.latest or {}).get("docker") or {}
            online = n.online and bool(n.latest)
            for c in dk.get("containers") or []:
                sid = f"{n.id}/{c['name']}"
                present.add(sid)
                keep = {k: c.get(k) for k in ("name", "image", "project", "service", "pmap", "ports", "gate") if c.get(k)}
                if not n.local and n.addr:
                    keep["_addr"] = n.addr  # so links still point at the right machine while it's offline
                if c.get("state") == "running" and (keep.get("pmap") or keep.get("gate")):
                    blob = json.dumps(keep, sort_keys=True)
                    if self.seen.get(sid, (None, None))[1] != blob:
                        self.seen[sid] = (n.id, blob)
                        remember.append((sid, n.id, blob))
                elif sid in self.seen and not keep.get("pmap"):
                    try:  # stopped containers lose their port bindings: use the last ones we saw
                        keep = {**json.loads(self.seen[sid][1]), **{k: v for k, v in keep.items() if k != "ports"}}
                    except ValueError:
                        pass
                e = self._entry(n, {**c, **keep}, online, sid)
                if e:
                    out.append(e)
            if online and dk.get("online"):
                for sid, (nid, _) in list(self.seen.items()):
                    if nid == n.id and sid not in present:
                        del self.seen[sid]
                        prune.append(sid)
        for sid, (nid, blob) in list(self.seen.items()):  # machines that are offline / not connected yet
            if sid in present:
                continue
            node = self.hub.nodes.get(nid)
            if node is not None and node.online and node.latest and ((node.latest.get("docker") or {}).get("online")):
                continue
            try:
                c = json.loads(blob)
            except ValueError:
                continue
            e = self._entry(node or _Ghost(nid), {**c, "state": "unknown"}, False, sid)
            if e:
                out.append(e)
        return out

    def _entry(self, node, c: dict, online: bool, sid: str) -> dict | None:
        url, extra = self._url_for(node, c)
        if not url:
            return None
        g = c.get("gate") or {}
        st, detail = ct_status(c, online)
        return {"id": sid, "kind": "container", "name": g.get("name") or c.get("service") or c["name"],
                "ct": c["name"], "node": node.id, "url": url, "extra": extra, "group": g.get("group") or c.get("project"),
                "icon": g.get("icon"), "desc": g.get("description"), "image": c.get("image"),
                "status": st, "detail": detail, "hidden": _truthy(g.get("hide", "")), "pinned": False}

    def services(self) -> dict:
        prune, remember = [], []
        with self.lock:
            items = self._container_entries(prune, remember)
        st = self.hub.store
        if st and (prune or remember):
            try:
                if remember:
                    st.gate_seen_put(remember)
                if prune:
                    st.gate_seen_del(prune)
            except Exception:  # noqa: BLE001
                log.exception("gate: could not save remembered services")
        for b in self.hub.beacons.snapshot():
            if b["kind"] == "http":
                items.append({"id": f"beacon/{b['name']}", "kind": "beacon", "name": b["name"], "node": None,
                              "url": b["target"], "extra": [], "group": "BEACONS", "icon": None, "desc": None,
                              "status": "up" if b["up"] else "lost" if b["up"] is None else "crit",
                              "detail": "beacon up" if b["up"] else (b["error"] or "waiting for first probe"),
                              "hidden": False, "pinned": False})
        for lk in self.env_links:
            items.append({"id": lk["id"], "kind": "link", "name": lk["name"], "node": None, "url": lk["url"],
                          "extra": [], "group": "LINKS", "icon": None, "desc": None, "status": "none", "detail": "bookmark",
                          "hidden": False, "pinned": False})
        saved = st.gate_items() if st else []
        for it in saved:
            if it["kind"] == "link":
                items.append({"id": it["id"], "kind": "link", "name": it["name"], "node": None, "url": it["url"],
                              "extra": [], "group": it["grp"] or "LINKS", "icon": it["icon"], "desc": None,
                              "status": "none", "detail": "bookmark", "hidden": bool(it["hidden"]),
                              "pinned": bool(it["pinned"]), "created": it["created"]})
        meta = {it["id"]: it for it in saved if it["kind"] == "meta"}
        for e in items:
            m = meta.get(e["id"])
            if m:
                e["auto"] = {"name": e["name"], "url": e["url"], "group": e["group"], "icon": e["icon"]}
                e["custom"] = True
                for k, col in (("name", "name"), ("url", "url"), ("group", "grp"), ("icon", "icon")):
                    if m[col]:
                        e[k] = m[col]
                e["hidden"], e["pinned"] = bool(m["hidden"]), bool(m["pinned"])
        targets = {}
        for e in items:
            probe = urllib.parse.urlsplit(e["url"].replace(LOCAL, "127.0.0.1"))
            explicit = e["icon"] if e["icon"] and valid_url(e["icon"].replace(LOCAL, "127.0.0.1")) else None
            targets[e["id"]] = {"name": e["name"], "url": e["url"].replace(LOCAL, "127.0.0.1"),
                                "icon": explicit.replace(LOCAL, "127.0.0.1") if explicit else None,
                                "probe": (probe.hostname, probe.port or 443, probe.hostname) if probe.scheme == "https" else None}
            if probe.scheme == "https" and probe.hostname:
                c = self.certs.get(f"{probe.hostname}:{probe.port or 443}")
                if c and c.get("until"):
                    e["cert"] = {"days": round((c["until"] - time.time()) / 86400, 1), "until": int(c["until"] * 1000),
                                 "self": c.get("self_signed", False)}
                elif c and c.get("error"):
                    e["cert"] = {"error": c["error"]}
        self.targets = targets
        return {"services": items, "machines": self.machines(), "edit": st is not None,
                "hub": self.hub.local.id, "t": int(time.time() * 1000)}

    # ── machines / wake-on-LAN ──
    def machines(self) -> list[dict]:
        out, seen = [], set()
        for n in sorted(self.hub.nodes.values(), key=lambda n: (not n.local, n.id)):
            net = (n.latest or {}).get("net") or {}
            prim = next((i for i in net.get("ifaces", []) if i["name"] == net.get("primary")), None)
            mac = (prim or {}).get("mac")
            if mac and MAC_RE.match(mac) and mac != "00:00:00:00:00:00" and self.macs.get(n.id) != mac.lower():
                self.macs[n.id] = mac.lower()
                if self.hub.store:
                    self.hub.store.kv_put(f"wol:{n.id}", mac.lower())
            host = (n.latest or {}).get("host") or {}
            out.append({"id": n.id, "name": host.get("name", n.id), "kind": "node", "local": n.local,
                        "online": n.online, "addr": n.addr, "mac": self.macs.get(n.id),
                        "age": round(time.monotonic() - n.last_seen) if n.last_seen else None})
            seen.add(n.id)
        for nid, mac in self.macs.items():
            if nid not in seen:
                out.append({"id": nid, "name": nid, "kind": "node", "local": False, "online": False,
                            "addr": None, "mac": mac, "age": None})
        beacons = {b["name"]: b for b in self.hub.beacons.snapshot()}
        for w in self.env_wol:
            b = beacons.get(node_id(w["name"]))
            out.append({"id": w["id"], "name": w["name"], "kind": "wol", "local": False,
                        "online": None if b is None or b["up"] is None else bool(b["up"]),
                        "addr": b["host"] if b else None, "mac": w["mac"], "age": None})
        return out

    def wake(self, mid: str) -> dict:
        m = next((x for x in self.machines() if x["id"] == mid), None)
        if m is None:
            raise GateError(f"unknown machine {mid!r}")
        if m["local"]:
            raise GateError("the hub is already awake")
        if not m["mac"]:
            raise GateError(f"no MAC address known for {m['name']} yet")
        now = time.monotonic()
        if now - self.woke.get(mid, 0) < WOL_COOLDOWN:
            raise GateError("a wake packet was just sent, give it a moment")
        self.woke[mid] = now
        bc = next((w["bcast"] for w in self.env_wol if w["id"] == mid), None)
        n = send_wol(m["mac"], bc)
        if not n:
            raise GateError("could not send the magic packet (no broadcast route)")
        return {"ok": True, "name": m["name"], "mac": m["mac"], "packets": n}

    # ── edits ──
    def save(self, body: dict) -> dict:
        st = self.hub.store
        if not st:
            raise GateError("the archive (/data) is unavailable, so GATE can't save changes")

        def text(k: str, n: int) -> str | None:
            v = body.get(k)
            if v is None:
                return None
            v = re.sub(r"[\x00-\x1f\x7f]", "", str(v)).strip()
            return v[:n] or None

        iid = text("id", 200) or ""
        name, url, grp, icon = text("name", 60), text("url", 500), text("group", 40), text("icon", 500)
        if url and not valid_url(url):
            raise GateError("URL must start with http:// or https://")
        if icon and len(icon) > 8 and not valid_url(icon):
            raise GateError("icon must be an emoji / up to 8 characters, or an http(s) image URL")
        hidden, pinned = int(bool(body.get("hidden"))), int(bool(body.get("pinned")))
        old = {it["id"]: it for it in st.gate_items()}
        if not iid or iid.startswith("link/"):
            if not name or not url:
                raise GateError("a bookmark needs a name and a URL")
            iid = iid or "link/" + os.urandom(5).hex()
            created = (old.get(iid) or {}).get("created") or int(time.time())
            st.gate_put({"id": iid, "kind": "link", "name": name, "grp": grp, "url": url, "icon": icon,
                         "hidden": hidden, "pinned": pinned, "created": created})
        else:
            if iid not in self.targets:
                raise GateError("unknown service")
            st.gate_put({"id": iid, "kind": "meta", "name": name, "grp": grp, "url": url, "icon": icon,
                         "hidden": hidden, "pinned": pinned, "created": int(time.time())})
        self.icons.pop(iid, None)
        return {"ok": True, "id": iid}

    def delete(self, iid: str) -> dict:
        if not self.hub.store:
            raise GateError("the archive (/data) is unavailable")
        self.hub.store.gate_del(iid)
        self.icons.pop(iid, None)
        return {"ok": True}

    # ── favicons ──
    def icon(self, iid: str) -> tuple[str, bytes] | None:
        hit = self.icons.get(iid)
        if hit and hit[0] > time.time():
            return (hit[1], hit[2]) if hit[2] else None
        t = self.targets.get(iid)
        if not t:
            return None
        res = find_icon(t["url"], t.get("icon"))
        if len(self.icons) > 400:
            self.icons.clear()
        self.icons[iid] = (time.time() + (ICON_TTL if res else ICON_MISS_TTL), *(res or (None, None)))
        return res

    # ── certificate watch ──
    def _cert_loop(self) -> None:
        time.sleep(20)
        while True:
            try:
                self._check_certs()
            except Exception:  # noqa: BLE001
                log.exception("gate: certificate check failed")
            time.sleep(60)

    def _check_certs(self) -> None:
        self.services()  # refresh the target list even when nobody has GATE open
        names: dict[tuple, list[str]] = {}
        for t in list(self.targets.values()):
            if t["probe"] and t["probe"][0]:
                names.setdefault(t["probe"], []).append(t["name"])
        for (host, port, sni), who in names.items():
            key = f"{host}:{port}"
            c = self.certs.get(key) or {}
            if time.time() - c.get("checked", 0) < (CERT_EVERY if c.get("until") else CERT_RETRY):
                continue
            try:
                info = fetch_cert(host, port, sni)
                c = {"checked": time.time(), "until": info["not_after"], "self_signed": info["self_signed"]}
            except Exception as e:  # noqa: BLE001
                c = {"checked": time.time(), "error": (str(e) or e.__class__.__name__)[:120]}
            self.certs[key] = c
            if not c.get("until"):
                continue
            days = (c["until"] - time.time()) / 86400
            lvl = "crit" if days <= CERT_CRIT_DAYS else "warn" if days <= CERT_WARN_DAYS else "ok"
            prev = self.cert_lvl.get(key)
            self.cert_lvl[key] = lvl
            label = ", ".join(sorted(set(who))[:3])
            if lvl != "ok" and lvl != prev:
                where = "" if host in ("127.0.0.1", "::1") else f" ({host})"
                msg = f"{label} certificate EXPIRED{where}" if days < 0 else f"{label} certificate expires in {days:.0f} days{where}"
                EVENTS.add(lvl, "gate", msg)
            elif lvl == "ok" and prev in ("warn", "crit"):
                EVENTS.add("ok", "gate", f"{label} certificate renewed, valid {days:.0f} more days", rec=True)


class _Ghost:
    """Stand-in for a machine that isn't connected to the hub right now."""

    def __init__(self, nid: str) -> None:
        self.id, self.local, self.addr, self.online, self.latest = nid, False, None, False, None
