"""Container healthcheck: hits /healthz on whichever port this mode listens on."""
import os
import sys
import urllib.request

agent = os.getenv("HEARTH_MODE", "hub").strip().lower() == "agent"
port = os.getenv("HEARTH_PORT", "8089" if agent else "8088")
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4)
except Exception:
    sys.exit(1)
