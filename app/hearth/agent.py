"""HEARTH agent: collects this machine's telemetry and pushes it to a hub on the LAN."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from websockets.asyncio.client import connect

from . import control
from .collect import INTERVAL, NODE_ID, Collector

log = logging.getLogger("hearth")

HUB = os.getenv("HEARTH_HUB", "").strip().rstrip("/")
TOKEN = os.getenv("HEARTH_TOKEN", "").strip()

state = {"connected": False, "since": None, "error": None}


def hub_url() -> str:
    url = HUB
    if url.startswith("http://"):
        url = "ws://" + url[7:]
    elif url.startswith("https://"):
        url = "wss://" + url[8:]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    return url + "/agent"


async def handle_cmd(ws, m: dict) -> None:
    """Run a container action the hub forwarded. control.run refuses unless HEARTH_ALLOW_CONTROL=1 here."""
    try:
        res = await asyncio.to_thread(control.run, str(m.get("action")), str(m.get("cid")), int(m.get("lines") or 200))
    except control.ControlError as e:
        res = {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": f"{e.__class__.__name__}: {e}"[:300]}
    if m.get("action") != "logs":
        log.info("hub requested %s on %s: %s", m.get("action"), m.get("cid"), "ok" if res.get("ok") else res.get("error"))
    try:
        await ws.send(json.dumps({"type": "cmd_result", "id": m.get("id"), **res}))
    except Exception:  # noqa: BLE001
        pass


async def recv_loop(ws) -> None:
    tasks: set[asyncio.Task] = set()
    async for raw in ws:
        try:
            m = json.loads(raw)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("type") == "cmd":
            t = asyncio.create_task(handle_cmd(ws, m))
            tasks.add(t)
            t.add_done_callback(tasks.discard)


async def run_agent() -> None:
    if not HUB or not TOKEN:
        log.error("agent mode needs HEARTH_HUB and HEARTH_TOKEN")
        state["error"] = "HEARTH_HUB / HEARTH_TOKEN not set"
        return
    collector = Collector()
    collector.start()
    url = hub_url()
    delay = 2.0
    while True:
        try:
            async with connect(url, open_timeout=10, max_size=8 * 1024 * 1024) as ws:
                await ws.send(json.dumps({"type": "auth", "token": TOKEN, "node": NODE_ID}))
                welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                log.info("linked to hub %s as '%s'", HUB, welcome.get("node"))
                state.update(connected=True, since=time.time(), error=None)
                delay = 2.0
                recv = asyncio.create_task(recv_loop(ws))
                try:
                    while True:
                        if recv.done():
                            recv.result()  # re-raises the close reason
                            raise ConnectionError("hub closed the link")
                        t0 = time.monotonic()
                        snap = await asyncio.to_thread(collector.collect)
                        await ws.send(json.dumps({"type": "tick", "data": snap}, separators=(",", ":")))
                        await asyncio.sleep(max(0.05, INTERVAL - (time.monotonic() - t0)))
                finally:
                    recv.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            state.update(connected=False, error=f"{e.__class__.__name__}: {e}"[:200])
            log.warning("hub link down (%s) — retrying in %.0fs", state["error"], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(run_agent())
    yield
    task.cancel()


app = FastAPI(title="HEARTH agent", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "mode": "agent", "node": NODE_ID, **state}
