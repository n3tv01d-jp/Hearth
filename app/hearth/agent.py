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
                while True:
                    t0 = time.monotonic()
                    snap = await asyncio.to_thread(collector.collect)
                    await ws.send(json.dumps({"type": "tick", "data": snap}, separators=(",", ":")))
                    await asyncio.sleep(max(0.05, INTERVAL - (time.monotonic() - t0)))
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
