"""Container actions: start / stop / restart / logs through the Docker socket.

Off unless HEARTH_ALLOW_CONTROL=1 is set on the machine that runs the container. The hub needs it
too, because it's the hub that accepts requests from browsers.
"""
from __future__ import annotations

import re

from .collect import ALLOW_CONTROL, DOCKER_HOST, SELF_CONTAINER, docker

ACTIONS = ("start", "stop", "restart", "logs")
ARSENAL_ACTIONS = ("check", "update", "prune", "stack")
CID = re.compile(r"^[0-9a-f]{12,64}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
MAX_LOG = 256 * 1024


class ControlError(Exception):
    pass


def run(action: str, cid: str, lines: int = 200, arg: str | None = None) -> dict:
    if action in ARSENAL_ACTIONS:
        from . import arsenal
        try:
            return arsenal.get().request(action, cid, arg)
        except (PermissionError, LookupError, ValueError, RuntimeError) as e:
            raise ControlError(str(e)[:300]) from e
    if not ALLOW_CONTROL:
        raise ControlError("container control is disabled on this machine (set HEARTH_ALLOW_CONTROL=1)")
    if action not in ACTIONS:
        raise ControlError(f"unknown action {action!r}")
    if not isinstance(cid, str) or not CID.match(cid):
        raise ControlError("bad container id")
    if docker is None:
        raise ControlError("docker SDK not installed")
    if action != "logs" and SELF_CONTAINER and cid[:12] == SELF_CONTAINER:
        raise ControlError("HEARTH won't stop or restart its own container")
    api = docker.APIClient(base_url=DOCKER_HOST, timeout=30, version="auto")
    try:
        if action == "logs":
            raw = api.logs(cid, tail=max(10, min(int(lines), 2000)), timestamps=True)
            text = ANSI.sub("", raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
            return {"ok": True, "logs": text[-MAX_LOG:]}
        if action == "start":
            api.start(cid)
        else:
            getattr(api, action)(cid, timeout=10)
        return {"ok": True}
    except docker.errors.APIError as e:
        msg = e.explanation or str(e)
        raise ControlError((msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg))[:300]) from e
    finally:
        api.close()
