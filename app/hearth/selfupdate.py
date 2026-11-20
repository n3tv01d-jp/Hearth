"""HEARTH's self-update helper. ARSENAL starts it in a short-lived container (from the image HEARTH
runs now, with only the Docker socket) because HEARTH can't replace the container it runs in:

    python -m hearth.selfupdate <container id> <image ref> <running version>

It waits a moment so HEARTH can answer the request, swaps HEARTH's container for a copy on the
freshly pulled image, and waits for the new one to be healthy. If anything fails, the old
container is put back. The last line it prints (`OK …` / `FAIL …`) is read and reported by
whichever HEARTH is running afterwards, which then removes this helper.
"""
from __future__ import annotations

import sys
import time

from .arsenal import SETTLE_SECONDS, recreate
from .collect import DOCKER_HOST, docker


def main(argv: list[str]) -> int:
    if len(argv) != 4 or docker is None:
        print("FAIL usage: python -m hearth.selfupdate <container> <image> <version>", flush=True)
        return 2
    cid, ref, old = argv[1:]
    time.sleep(3)
    api = docker.APIClient(base_url=DOCKER_HOST, timeout=600, version="auto")
    try:
        c = api.inspect_container(cid)
        print(f"replacing {c['Name'].lstrip('/')} ({old}) with a copy on {ref}", flush=True)
        recreate(api, c, ref, verify=SETTLE_SECONDS)
    except Exception as e:  # noqa: BLE001
        msg = str(getattr(e, "explanation", None) or e) or e.__class__.__name__
        print(f"FAIL {msg[:300]} (the old container was kept)", flush=True)
        return 1
    finally:
        api.close()
    print(f"OK {old}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
