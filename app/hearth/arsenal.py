"""ARSENAL 工廠: maintenance for every machine in the fleet.

Machine side (runs in the hub and in every agent, reported in each snapshot under "arsenal"):
  * image update radar: compares each container's image with its registry (anonymous token auth,
    Docker Hub / GHCR / lscr.io / Quay / any v2 registry), every HEARTH_UPDATE_INTERVAL seconds
  * update = pull + recreate the container with its exact config (watchtower-style), with rollback
  * Docker disk usage and prune (dangling / unused images, stopped containers, build cache)
  * compose stacks: start / stop / restart every container of a project
  * host: reboot required (Debian flag file, or a newer kernel installed than the one running),
    pending updates where the OS exposes a count, and when packages last changed
  * backup watch: HEARTH_BACKUPS="photos=/mnt/backup/restic@26h, dumps=/srv/dumps/*.sql.gz@2d"
    (restic and borg repositories are recognised; anything else uses the newest file's mtime)

Hub side (`Ops`): maintenance windows that mute push alerts, the change journal, and heartbeats
that backup jobs elsewhere can ping: HEARTH_HEARTBEATS="offsite=26h" → GET /api/ping/offsite
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

from . import __version__
from .collect import ALLOW_CONTROL, DOCKER_HOST, EVENTS, HOSTFS, SELF_CONTAINER, docker, node_id

log = logging.getLogger("hearth")

UPDATE_EVERY = max(600.0, float(os.getenv("HEARTH_UPDATE_INTERVAL", str(6 * 3600))))
DF_EVERY, HOST_EVERY, BACKUP_EVERY = 1800, 300, 60
UA = f"HEARTH-arsenal/{__version__}"
MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json", "application/vnd.oci.image.manifest.v1+json"))
PRUNES = {"images": "dangling images", "images-all": "all unused images", "containers": "stopped containers",
          "build": "build cache"}
STACK_ACTS = ("start", "stop", "restart")


def _hostpath(p: str) -> str:
    return HOSTFS + p if HOSTFS and os.path.isdir(HOSTFS) and p.startswith("/") else p


def parse_age(s: str, default: float) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", s or "")
    if not m:
        return default
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "": 3600}[m.group(2)]


# ───────────────────────────── registries ─────────────────────────────
def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """'lscr.io/linuxserver/sonarr:4' -> ('lscr.io', 'linuxserver/sonarr', '4'). None for digests / ids."""
    if not ref or "@" in ref or ref.startswith("sha256:"):
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg, repo = parts[0], "/".join(parts[1:])
    else:
        reg, repo = "docker.io", name
    if reg in ("docker.io", "index.docker.io"):
        reg = "registry-1.docker.io"
        if "/" not in repo:
            repo = "library/" + repo
    return reg, repo, tag


class Registry:
    def __init__(self) -> None:
        self.tokens: dict[str, tuple[float, str]] = {}

    def _token(self, challenge: str) -> str | None:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not realm or not challenge.lower().startswith("bearer"):
            return None
        key = realm + "?" + urllib.parse.urlencode(sorted(params.items()))
        hit = self.tokens.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        req = urllib.request.Request(key, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (https realm from the registry)
            j = json.loads(r.read(64 * 1024))
        tok = j.get("token") or j.get("access_token")
        if tok:
            self.tokens[key] = (time.time() + min(240, int(j.get("expires_in") or 240)), tok)
        return tok

    def digest(self, reg: str, repo: str, tag: str) -> str:
        url = f"https://{reg}/v2/{repo}/manifests/{tag}"
        auth = None
        for attempt in range(2):
            for method in ("HEAD", "GET"):
                h = {"User-Agent": UA, "Accept": MANIFEST_TYPES}
                if auth:
                    h["Authorization"] = f"Bearer {auth}"
                req = urllib.request.Request(url, headers=h, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                        d = r.headers.get("Docker-Content-Digest")
                        if d:
                            return d
                except urllib.error.HTTPError as e:
                    if e.code == 401 and attempt == 0:
                        auth = self._token(e.headers.get("WWW-Authenticate", ""))
                        if not auth:
                            raise ValueError("registry needs credentials") from None
                        break
                    if e.code == 404:
                        raise ValueError("tag not found in the registry") from None
                    if e.code == 429:
                        raise ValueError("registry rate limit, will retry later") from None
                    raise ValueError(f"registry HTTP {e.code}") from None
            else:
                raise ValueError("registry sent no digest")
        raise ValueError("registry needs credentials")


# ───────────────────────────── host ─────────────────────────────
def _kver(v: str) -> tuple:
    m = re.match(r"^([\d.]+)(?:-(\d+))?", v)
    return tuple(int(x) for x in re.findall(r"\d+", m.group(0))) if m else ()


def host_maint() -> dict:
    out: dict = {"reboot": False, "reason": None, "pending": None, "security": None, "pkg_changed": None}
    for p in ("/run/reboot-required", "/var/run/reboot-required"):
        if os.path.exists(_hostpath(p)):
            out["reboot"] = True
            try:
                pk = open(_hostpath(p + ".pkgs")).read().split()  # noqa: SIM115
                out["reason"] = "packages: " + ", ".join(sorted(set(pk))[:6]) if pk else "the OS asked for a reboot"
            except OSError:
                out["reason"] = "the OS asked for a reboot"
            break
    running = os.uname().release
    for d in ("/lib/modules", "/usr/lib/modules"):
        try:
            mods = [m for m in os.listdir(_hostpath(d)) if _kver(m)]
        except OSError:
            continue
        if not mods:
            continue
        newest = max(mods, key=_kver)
        if _kver(newest) > _kver(running):
            out["reboot"] = True
            out["reason"] = f"kernel {newest} installed, running {running}"
        out["kernel_new"] = newest
        break
    try:  # Ubuntu's motd counter
        txt = open(_hostpath("/var/lib/update-notifier/updates-available")).read()  # noqa: SIM115
        nums = [int(n) for n in re.findall(r"(\d+) (?:updates?|packages?) can be (?:applied|updated)", txt)]
        sec = re.findall(r"(\d+) (?:of these )?(?:are|is a)? ?(?:standard )?security update", txt)
        out["pending"] = nums[0] if nums else 0
        out["security"] = int(sec[0]) if sec else None
    except (OSError, ValueError):
        pass
    best = None
    for p in ("/var/lib/dpkg/status", "/usr/lib/sysimage/rpm/rpmdb.sqlite", "/var/lib/rpm/rpmdb.sqlite",
              "/var/lib/rpm/Packages", "/var/lib/pacman/local", "/lib/apk/db/installed"):
        try:
            t = os.stat(_hostpath(p)).st_mtime
            best = max(best or 0, t)
        except OSError:
            pass
    out["pkg_changed"] = int(best * 1000) if best else None
    return out


# ───────────────────────────── backups ─────────────────────────────
def _newest(path: str, depth: int = 3, budget: int = 20000) -> float | None:
    best, n = None, 0
    for root, dirs, files in os.walk(path):
        if root[len(path):].count(os.sep) >= depth:
            dirs[:] = []
        for f in files:
            n += 1
            if n > budget:
                return best
            try:
                t = os.lstat(os.path.join(root, f)).st_mtime
            except OSError:
                continue
            best = t if best is None or t > best else best
    return best


def backup_state(path: str) -> tuple[str, float | None, str | None]:
    """-> (kind, newest timestamp, detail)"""
    p = _hostpath(path)
    if any(ch in path for ch in "*?["):
        files = [f for f in glob.glob(p) if os.path.isfile(f)]
        if not files:
            return "files", None, "no files match"
        newest = max(files, key=os.path.getmtime)
        return "files", os.path.getmtime(newest), os.path.basename(newest)
    if os.path.isdir(p):
        if os.path.isfile(os.path.join(p, "config")) and os.path.isdir(os.path.join(p, "snapshots")):
            snaps = [os.path.join(p, "snapshots", f) for f in os.listdir(os.path.join(p, "snapshots"))]
            if not snaps:
                return "restic", None, "repository has no snapshots"
            return "restic", max(os.path.getmtime(s) for s in snaps), f"{len(snaps)} snapshots"
        hints = glob.glob(os.path.join(p, "hints.*")) + glob.glob(os.path.join(p, "integrity.*"))
        if hints and os.path.isfile(os.path.join(p, "config")):
            return "borg", max(os.path.getmtime(h) for h in hints), "borg repository"
        t = _newest(p)
        return "folder", t, None if t else "folder is empty"
    if os.path.isfile(p):
        return "file", os.path.getmtime(p), None
    return "missing", None, "path not found" + ("" if os.path.isdir(HOSTFS or "/nonexistent") else " (no /hostfs mount)")


def parse_backups(spec: str) -> list[dict]:
    out = []
    for part in (x.strip() for x in re.split(r",(?=\s*[^,=]+=)", spec or "")):
        name, sep, rest = part.partition("=")
        if not sep or not name.strip() or not rest.strip().startswith("/"):
            if part:
                log.warning("HEARTH_BACKUPS: skipping %r (expected name=/path[@26h])", part)
            continue
        path, _, age = rest.strip().rpartition("@") if re.search(r"@\d+(\.\d+)?[smhdw]?\s*$", rest) else (rest.strip(), "", "")
        out.append({"name": node_id(name.strip()), "path": path.strip(), "max": parse_age(age, 26 * 3600)})
    return out


# ───────────────────────────── recreate (update) ─────────────────────────────
STRIP_SAME = ("Cmd", "Entrypoint", "WorkingDir", "User", "Healthcheck", "StopSignal", "Shell", "OnBuild")


def build_create_body(c: dict, old_img: dict, ref: str) -> tuple[dict, list[tuple[str, dict]]]:
    """Create-request body for a fresh copy of container `c` on image `ref`, minus the old image's defaults."""
    oc = old_img.get("Config") or {}
    cfg = dict(c.get("Config") or {})
    for k in STRIP_SAME:
        if k in cfg and cfg.get(k) == oc.get(k):
            cfg.pop(k)
    if cfg.get("Env"):
        base = set(oc.get("Env") or [])
        cfg["Env"] = [e for e in cfg["Env"] if e not in base]
    if cfg.get("Labels"):
        base = oc.get("Labels") or {}
        cfg["Labels"] = {k: v for k, v in cfg["Labels"].items() if base.get(k) != v}
    for k in ("ExposedPorts", "Volumes"):
        if cfg.get(k):
            base = oc.get(k) or {}
            cfg[k] = {p: v for p, v in cfg[k].items() if p not in base} or None
    if cfg.get("Hostname") == c["Id"][:12]:
        cfg.pop("Hostname")
    cfg["Image"] = ref
    hc = dict(c.get("HostConfig") or {})
    if hc.get("MemorySwappiness") in (None, -1, 0):  # "unset": cgroup v2 runtimes refuse an explicit value
        hc.pop("MemorySwappiness", None)
    # keep anonymous volumes (e.g. a database's VOLUME) attached, like `docker compose up` does
    explicit = {b.split(":")[1] for b in hc.get("Binds") or [] if b.count(":") >= 1}
    explicit |= {m.get("Target") for m in hc.get("Mounts") or []}
    binds = list(hc.get("Binds") or [])
    for m in c.get("Mounts") or []:
        if m.get("Type") == "volume" and m.get("Destination") not in explicit and m.get("Name"):
            binds.append(f"{m['Name']}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro"))
    hc["Binds"] = binds or None
    body = {k: v for k, v in cfg.items() if v is not None}
    body["HostConfig"] = hc
    mode = hc.get("NetworkMode") or "default"
    nets = list(((c.get("NetworkSettings") or {}).get("Networks") or {}).items())
    extra: list[tuple[str, dict]] = []
    if mode not in ("host", "none") and not mode.startswith("container:") and nets:
        def ep(name: str, v: dict) -> dict:
            if name == "bridge":
                return {}
            return {"IPAMConfig": v.get("IPAMConfig"), "Links": v.get("Links"),
                    "Aliases": [a for a in v.get("Aliases") or [] if a not in (c["Id"][:12], c["Id"])] or None}
        first = next(((n, v) for n, v in nets if n == mode), nets[0])
        body["NetworkingConfig"] = {"EndpointsConfig": {first[0]: ep(*first)}}
        extra = [(n, ep(n, v)) for n, v in nets if n != first[0]]
    return body, extra


# ───────────────────────────── the machine-side service ─────────────────────────────
class Arsenal:
    def __init__(self) -> None:
        self.reg = Registry()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.updates: dict[str, dict] = {}      # container id -> update state
        self.refs: dict[str, dict] = {}         # image ref -> {"remote", "checked", "error"}
        self.checked: int | None = None
        self.checking = False
        self.df: dict | None = None
        self.host: dict = {}
        self.backups: list[dict] = []
        self.backup_cfg = parse_backups(os.getenv("HEARTH_BACKUPS", ""))
        self.backup_lvl: dict[str, str] = {}
        self.jobs: OrderedDict[str, dict] = OrderedDict()
        self.busy: set[str] = set()
        self.force_refs: set[str] = set()
        self._df_at = self._host_at = self._bk_at = self._up_at = 0.0
        self._df_now = False
        self._err: str | None = None

    def start(self) -> None:
        if docker is None:
            return
        threading.Thread(target=self._loop, name="arsenal", daemon=True).start()

    def _api(self, timeout: int | None = 60):
        return docker.APIClient(base_url=DOCKER_HOST, timeout=timeout, version="auto")

    def _loop(self) -> None:
        time.sleep(5)
        while True:
            now = time.monotonic()
            try:
                if now - self._host_at >= HOST_EVERY:
                    self._host_at = now
                    self.host = host_maint()
                if self.backup_cfg and now - self._bk_at >= BACKUP_EVERY:
                    self._bk_at = now
                    self._check_backups()
                if self._df_now or now - self._df_at >= DF_EVERY:
                    self._df_at, self._df_now = now, False
                    self._disk_usage()
                if self.force_refs or not self._up_at or now - self._up_at >= UPDATE_EVERY:
                    full = not self.force_refs or not self._up_at
                    if full:
                        self._up_at = now
                    self._check_updates(force=set(self.force_refs) if not full else None)
                self._err = None
            except Exception as e:  # noqa: BLE001
                msg = f"{e.__class__.__name__}: {e}"[:200]
                if msg != self._err:  # once per distinct problem (e.g. no docker socket), not every 30 s
                    self._err = msg
                    log.warning("arsenal: %s", msg)
            self.wake.wait(30)
            self.wake.clear()

    # ── update radar ──
    def _check_updates(self, force: set[str] | None = None) -> None:
        self.checking = True
        api = self._api()
        try:
            cts = api.containers(all=True)
            images: dict[str, dict | None] = {}
            for r in cts:
                ref = r.get("Image") or ""
                if ref in images:
                    continue
                try:
                    images[ref] = api.inspect_image(ref)
                except Exception:  # noqa: BLE001
                    images[ref] = None
            for ref, img in images.items():
                pr = parse_ref(ref)
                st = self.refs.get(ref) or {}
                if force is not None and ref not in force:
                    continue
                if pr is None:
                    self.refs[ref] = {"state": "pinned" if "@" in ref else "untagged", "checked": int(time.time() * 1000)}
                    continue
                if img is None:
                    self.refs[ref] = {"state": "missing", "checked": int(time.time() * 1000)}
                    continue
                if not img.get("RepoDigests"):
                    self.refs[ref] = {"state": "local", "checked": int(time.time() * 1000)}
                    continue
                try:
                    remote = self.reg.digest(*pr)
                    local = {d.split("@", 1)[1] for d in img["RepoDigests"] if "@" in d}
                    st = {"state": "current" if remote in local else "update", "remote": remote,
                          "image_id": img.get("Id"), "checked": int(time.time() * 1000)}
                except Exception as e:  # noqa: BLE001
                    st = {**{k: v for k, v in st.items() if k != "error"}, "error": str(e)[:160],
                          "checked": int(time.time() * 1000), "state": st.get("state") or "error"}
                self.refs[ref] = st
                time.sleep(0.3)  # be gentle with registries
            self.force_refs -= set(images) if force is None else force
            new = {}
            fresh = []
            for r in cts:
                ref, cid = r.get("Image") or "", r["Id"][:12]
                st = self.refs.get(ref) or {"state": "unknown"}
                state = st["state"]
                if state == "current" and st.get("image_id") and r.get("ImageID") and r["ImageID"] != st["image_id"]:
                    state = "pulled"  # a newer image is already on disk, the container just needs recreating
                name = ((r.get("Names") or ["/?"])[0]).lstrip("/")
                new[cid] = {"id": cid, "name": name, "image": ref, "state": state, "error": st.get("error"),
                            "checked": st.get("checked"), "running": r.get("State") == "running",
                            "project": (r.get("Labels") or {}).get("com.docker.compose.project"),
                            "self": bool(SELF_CONTAINER and cid == SELF_CONTAINER)}
                old = self.updates.get(cid)
                if state in ("update", "pulled") and old and old["state"] == "current":
                    fresh.append(name)
            with self.lock:
                self.updates = new
                self.checked = int(time.time() * 1000)
            if fresh:
                EVENTS.add("info", "arsenal", f"⬆ update available: {', '.join(sorted(fresh)[:5])}"
                           + (f" +{len(fresh) - 5} more" if len(fresh) > 5 else ""))
        finally:
            self.checking = False
            api.close()

    # ── disk usage ──
    def _disk_usage(self) -> None:
        api = self._api(timeout=180)
        try:
            d = api.df()
        finally:
            api.close()
        imgs = d.get("Images") or []

        def own(i: dict) -> int:
            return max(0, (i.get("Size") or 0) - max(0, i.get("SharedSize") or 0))

        dangling = [i for i in imgs if not i.get("RepoTags") or i["RepoTags"] == ["<none>:<none>"]]
        unused = [i for i in imgs if (i.get("Containers") or 0) == 0]
        cts = d.get("Containers") or []
        stopped = [c for c in cts if c.get("State") != "running"]
        vols = d.get("Volumes") or []
        cache = d.get("BuildCache") or []
        self.df = {
            "t": int(time.time() * 1000),
            "images": {"n": len(imgs), "size": d.get("LayersSize") or sum(own(i) for i in imgs),
                       "dangling": len(dangling), "dangling_size": sum(own(i) for i in dangling),
                       "unused": len(unused), "unused_size": sum(own(i) for i in unused)},
            "containers": {"n": len(cts), "stopped": len(stopped), "stopped_size": sum(c.get("SizeRw") or 0 for c in stopped)},
            "volumes": {"n": len(vols), "unused": sum(1 for v in vols if ((v.get("UsageData") or {}).get("RefCount") or 0) == 0),
                        "size": sum(max(0, (v.get("UsageData") or {}).get("Size") or 0) for v in vols)},
            "build": {"n": len(cache), "size": sum(b.get("Size") or 0 for b in cache),
                      "reclaim": sum(b.get("Size") or 0 for b in cache if not b.get("InUse") and not b.get("Shared"))},
        }

    # ── backups ──
    def _check_backups(self) -> None:
        out = []
        now = time.time()
        for b in self.backup_cfg:
            try:
                kind, t, detail = backup_state(b["path"])
            except Exception as e:  # noqa: BLE001
                kind, t, detail = "error", None, str(e)[:120]
            age = now - t if t else None
            lvl = "crit" if age is None or age > 2 * b["max"] else "warn" if age > b["max"] else "ok"
            out.append({"name": b["name"], "path": b["path"], "kind": kind, "last": int(t * 1000) if t else None,
                        "max": b["max"], "lvl": lvl, "detail": detail})
            prev = self.backup_lvl.get(b["name"])
            self.backup_lvl[b["name"]] = lvl
            if prev is not None and prev != lvl:
                if lvl == "ok":
                    EVENTS.add("ok", "backup", f"{b['name']} backup is fresh again", rec=True)
                else:
                    late = f"last run {round(age / 3600)}h ago" if age else detail or "nothing found"
                    EVENTS.add(lvl, "backup", f"{b['name']} backup {'is late' if lvl == 'warn' else 'is missing'} ({late})")
            elif prev is None and lvl != "ok":
                EVENTS.add(lvl, "backup", f"{b['name']} backup needs attention ({detail or 'too old'})")
        self.backups = out

    # ── jobs (update / prune / stack): run in the background, reported in the snapshot ──
    def _job(self, action: str, target: str, fn) -> dict:
        key = f"{action}:{target}"
        if key in self.busy:
            raise RuntimeError(f"{action} {target} is already running")
        jid = secrets.token_hex(4)
        job = {"id": jid, "action": action, "target": target, "state": "running", "msg": None, "t": int(time.time() * 1000)}
        with self.lock:
            self.jobs[jid] = job
            while len(self.jobs) > 12:
                self.jobs.popitem(last=False)
        self.busy.add(key)

        def run() -> None:
            try:
                job["msg"] = fn() or "done"
                job["state"] = "ok"
            except Exception as e:  # noqa: BLE001
                job["state"], job["msg"] = "failed", (str(getattr(e, "explanation", None) or e) or e.__class__.__name__)[:300]
                EVENTS.add("warn", "arsenal", f"{action} {target} failed: {job['msg']}")
            finally:
                job["done"] = int(time.time() * 1000)
                self.busy.discard(key)
                self._df_now = True
                self.wake.set()

        threading.Thread(target=run, name=f"arsenal-{action}", daemon=True).start()
        return {"ok": True, "job": jid}

    def request(self, action: str, cid: str | None, arg: str | None) -> dict:
        if action == "check":
            self._up_at = 0.0  # full re-check on the next loop
            self._df_now = True
            self.wake.set()
            return {"ok": True}
        if not ALLOW_CONTROL:
            raise PermissionError("container control is disabled on this machine (set HEARTH_ALLOW_CONTROL=1)")
        if action == "update":
            u = self.updates.get(cid or "")
            if not u:
                raise LookupError("unknown container")
            if u["self"]:
                raise PermissionError("HEARTH can't update itself: run `git pull && docker compose up -d --build`")
            if u["state"] in ("local", "untagged", "pinned", "missing"):
                raise ValueError(f"{u['name']} uses a {u['state']} image, there's nothing to pull")
            return self._job("update", u["name"], lambda: self._update(u["id"], u["image"]))
        if action == "prune":
            if arg not in PRUNES:
                raise ValueError(f"prune what? one of {', '.join(PRUNES)}")
            return self._job("prune", PRUNES[arg], lambda: self._prune(arg))
        if action == "stack":
            proj, _, act = (arg or "").rpartition(":")
            if act not in STACK_ACTS or not proj:
                raise ValueError("expected project:start|stop|restart")
            return self._job(act, f"stack {proj}", lambda: self._stack(proj, act))
        raise ValueError(f"unknown action {action!r}")

    def _update(self, cid: str, ref: str) -> str:
        api = self._api(timeout=600)
        try:
            c = api.inspect_container(cid)
            name = c["Name"].lstrip("/")
            old_img = api.inspect_image(c["Image"])
            reg_repo, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "latest")
            for line in api.pull(reg_repo, tag=tag, stream=True, decode=True):
                if isinstance(line, dict) and line.get("error"):
                    raise RuntimeError(f"pull failed: {line['error']}")
            new_img = api.inspect_image(ref)
            if new_img["Id"] == c["Image"]:
                self.force_refs.add(ref)
                return f"{name} is already on the newest image"
            body, extra = build_create_body(c, old_img, ref)
            was_running = bool((c.get("State") or {}).get("Running"))
            tmp = f"{name}-hearth-old-{secrets.token_hex(2)}"
            if was_running:
                api.stop(cid, timeout=int((c.get("Config") or {}).get("StopTimeout") or 10))
            api.rename(cid, tmp)
            new_id = None
            try:
                res = api._post_json(api._url("/containers/create"), data=body, params={"name": name})  # noqa: SLF001
                new_id = api._result(res, True)["Id"]  # noqa: SLF001
                for net, ep in extra:
                    links = [tuple(x.lstrip("/").split(":", 1)) if ":" in x else (x, None) for x in ep.get("Links") or []]
                    api.connect_container_to_network(new_id, net, aliases=ep.get("Aliases"),
                                                     links=links or None,
                                                     ipv4_address=(ep.get("IPAMConfig") or {}).get("IPv4Address"),
                                                     ipv6_address=(ep.get("IPAMConfig") or {}).get("IPv6Address"))
                if was_running:
                    api.start(new_id)
            except Exception:
                # roll back: put the old container back exactly as it was
                if new_id:
                    try:
                        api.remove_container(new_id, force=True)
                    except Exception:  # noqa: BLE001
                        pass
                api.rename(cid, name)
                if was_running:
                    api.start(cid)
                raise
            api.remove_container(cid, v=False)
            self.force_refs.add(ref)
            EVENTS.add("ok", "arsenal", f"{name} ⬆ updated to the newest {ref}")
            return f"{name} recreated on the new image"
        finally:
            api.close()

    def _prune(self, what: str) -> str:
        api = self._api(timeout=600)
        try:
            if what == "images":
                r = api.prune_images(filters={"dangling": True})
            elif what == "images-all":
                r = api.prune_images(filters={"dangling": False})
            elif what == "containers":
                r = api.prune_containers()
            else:
                r = api.prune_builds()
        finally:
            api.close()
        freed = (r or {}).get("SpaceReclaimed") or 0
        EVENTS.add("info", "arsenal", f"pruned {PRUNES[what]}: {freed / 1024 ** 3:.2f} GB freed")
        return f"{freed / 1024 ** 3:.2f} GB freed"

    def _stack(self, proj: str, act: str) -> str:
        api = self._api(timeout=120)
        try:
            cts = api.containers(all=True, filters={"label": f"com.docker.compose.project={proj}"})
            if not cts:
                raise LookupError(f"no containers in stack {proj}")
            done = 0
            for r in sorted(cts, key=lambda r: r["Names"][0]):
                cid = r["Id"][:12]
                if SELF_CONTAINER and cid == SELF_CONTAINER:
                    continue
                running = r.get("State") == "running"
                if act == "start" and not running:
                    api.start(cid)
                elif act == "stop" and running:
                    api.stop(cid, timeout=10)
                elif act == "restart":
                    api.restart(cid, timeout=10)
                else:
                    continue
                done += 1
        finally:
            api.close()
        EVENTS.add("info", "arsenal", f"stack {proj}: {act} ({done} containers)")
        return f"{act}: {done} containers"

    def snapshot(self) -> dict:
        with self.lock:
            ups = list(self.updates.values())
            jobs = list(self.jobs.values())
        return {"updates": ups, "checked": self.checked, "checking": self.checking, "every": UPDATE_EVERY,
                "df": self.df, "host": self.host, "backups": self.backups, "jobs": jobs, "control": ALLOW_CONTROL}


ARSENAL: Arsenal | None = None


def get() -> Arsenal:
    global ARSENAL  # noqa: PLW0603
    if ARSENAL is None:
        ARSENAL = Arsenal()
    return ARSENAL


# ───────────────────────────── hub side ─────────────────────────────
class Ops:
    """Maintenance windows, the change journal and backup heartbeats. Lives in the hub."""

    def __init__(self, store) -> None:
        self.store = store
        self.lock = threading.Lock()
        self.windows: list[dict] = []
        self.beats: dict[str, dict] = {}
        for part in re.split(r"[,\s]+", os.getenv("HEARTH_HEARTBEATS", "")):
            name, sep, age = part.partition("=")
            if name:
                self.beats[node_id(name)] = {"name": node_id(name), "max": parse_age(age, 26 * 3600) if sep else 26 * 3600,
                                             "last": None, "fail": None, "msg": None, "lvl": None}
        if store:
            try:
                kv = store.kv_get("ops:")
                self.windows = json.loads(kv.get("ops:maint") or "[]")
                for k, v in kv.items():
                    if k.startswith("ops:hb:") and k[7:] in self.beats:
                        self.beats[k[7:]].update(json.loads(v))
            except Exception:  # noqa: BLE001
                log.exception("ops: could not load state")
        self._prune_windows()

    def start(self) -> None:
        if self.beats:
            threading.Thread(target=self._beat_loop, name="heartbeats", daemon=True).start()

    # ── maintenance windows ──
    def _save_windows(self) -> None:
        if self.store:
            self.store.kv_put("ops:maint", json.dumps(self.windows))

    def _prune_windows(self) -> bool:
        now = time.time() * 1000
        with self.lock:
            before = len(self.windows)
            ended = [w for w in self.windows if w["until"] <= now]
            self.windows = [w for w in self.windows if w["until"] > now]
        for w in ended:
            EVENTS.add("info", "maint", f"maintenance window ended ({w['scope']})", node=w.get("node"))
        if len(self.windows) != before:
            self._save_windows()
        return bool(ended)

    def active(self) -> list[dict]:
        self._prune_windows()
        return list(self.windows)

    def muted(self, e: dict) -> bool:
        if e.get("src") in ("maint", "journal"):
            return False
        now = time.time() * 1000
        return any(w["until"] > now and (w.get("node") is None or w["node"] == e.get("node")) for w in self.windows)

    def open_window(self, node: str | None, minutes: int, reason: str, who: str) -> dict:
        w = {"id": secrets.token_hex(4), "node": node, "scope": node or "whole fleet",
             "start": int(time.time() * 1000), "until": int((time.time() + minutes * 60) * 1000),
             "reason": reason or None, "who": who}
        with self.lock:
            self.windows.append(w)
        self._save_windows()
        EVENTS.add("info", "maint", f"🔧 maintenance on {w['scope']} for {minutes} min"
                   + (f": {reason}" if reason else "") + " (push alerts muted)", node=node)
        if reason:
            self.add_note(f"maintenance: {reason}", node, who)
        return w

    def close_window(self, wid: str) -> bool:
        with self.lock:
            w = next((x for x in self.windows if x["id"] == wid), None)
            if w:
                w["until"] = int(time.time() * 1000)
        if w:
            self._prune_windows()
        return bool(w)

    # ── journal ──
    def add_note(self, text: str, node: str | None, who: str) -> dict:
        if not self.store:
            raise RuntimeError("the archive (/data) is unavailable, so notes can't be saved")
        n = self.store.add_note(text, node, who)
        EVENTS.add("info", "journal", f"✎ {text}", node=node)
        return n

    # ── heartbeats ──
    def ping(self, name: str, ok: bool, msg: str | None) -> dict:
        b = self.beats.get(name)
        if b is None:
            raise LookupError(name)
        now = int(time.time() * 1000)
        if ok:
            b["last"], b["msg"] = now, msg
        else:
            b["fail"], b["msg"] = now, msg
        if self.store:
            self.store.kv_put(f"ops:hb:{name}", json.dumps({k: b[k] for k in ("last", "fail", "msg", "lvl")}))
        self._eval_beat(b)
        return {"ok": True, "name": name}

    def _eval_beat(self, b: dict) -> None:
        now = time.time() * 1000
        if b["fail"] and (not b["last"] or b["fail"] > b["last"]):
            lvl = "crit"
        elif not b["last"]:
            lvl = "warn"
        else:
            age = (now - b["last"]) / 1000
            lvl = "ok" if age <= b["max"] else "warn" if age <= 2 * b["max"] else "crit"
        prev, b["lvl"] = b.get("lvl"), lvl
        if prev == lvl or (prev is None and lvl == "warn" and not b["last"]):
            return
        if lvl == "ok":
            if prev in ("warn", "crit"):
                EVENTS.add("ok", "backup", f"{b['name']} checked in", rec=True)
        elif lvl == "crit" and b["fail"] and (not b["last"] or b["fail"] > b["last"]):
            EVENTS.add("crit", "backup", f"{b['name']} reported a FAILURE" + (f": {b['msg']}" if b["msg"] else ""))
        else:
            EVENTS.add(lvl, "backup", f"{b['name']} is late, no ping for {round((now - (b['last'] or now)) / 3600000)}h")
        if self.store:
            self.store.kv_put(f"ops:hb:{b['name']}", json.dumps({k: b[k] for k in ("last", "fail", "msg", "lvl")}))

    def _beat_loop(self) -> None:
        while True:
            time.sleep(60)
            for b in list(self.beats.values()):
                try:
                    self._eval_beat(b)
                except Exception:  # noqa: BLE001
                    log.exception("heartbeat check failed")
            self._prune_windows()

    def heartbeats(self) -> list[dict]:
        return [dict(b) for b in self.beats.values()]
