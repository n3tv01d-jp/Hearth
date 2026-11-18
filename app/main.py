"""HEARTH // 炉心監視システム — entrypoint.

HEARTH_MODE=hub   (default) serve the dashboard, collect this machine, accept LAN agents
HEARTH_MODE=agent push this machine's telemetry to the hub at HEARTH_HUB
"""
import logging
import os

import uvicorn

logging.basicConfig(level=os.getenv("HEARTH_LOG", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

MODE = os.getenv("HEARTH_MODE", "hub").strip().lower()

if __name__ == "__main__":
    if MODE == "agent":
        from hearth.agent import app
        default_bind, default_port = "127.0.0.1", "8089"
    else:
        from hearth.hub import app
        default_bind, default_port = "0.0.0.0", "8088"
    uvicorn.run(app, host=os.getenv("HEARTH_BIND", default_bind), port=int(os.getenv("HEARTH_PORT", default_port)),
                log_level="warning", proxy_headers=True)
