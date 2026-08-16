"""Entry point: `python -m ryu_markitdown` starts the uvicorn server.

Host/port come from the environment so Core can pin them at spawn:
  RYU_MARKITDOWN_HOST (default 127.0.0.1) · RYU_MARKITDOWN_PORT

The port env is the manifest's `port_env`, which Core sets to the
**profile-shifted** port (the dev profile adds 1000 to every port), so the
DEFAULT_PORT constant is only reached on a bare standalone run. `MARKITDOWN_PORT`
is accepted as a plain-name fallback for running the package outside Core.
"""

from __future__ import annotations

import os

import uvicorn

from . import DEFAULT_PORT


def _port() -> int:
    for name in ("RYU_MARKITDOWN_PORT", "MARKITDOWN_PORT"):
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return DEFAULT_PORT


def main() -> None:
    # Loopback only. This process reads local files by path; it must never be
    # reachable off-box, and Core proxies it from the same machine.
    host = os.environ.get("RYU_MARKITDOWN_HOST", "127.0.0.1")
    # Single worker: the job table is in-process, so a second worker would answer
    # polls for jobs it has never heard of.
    uvicorn.run("ryu_markitdown.server:app", host=host, port=_port(), log_level="info")


if __name__ == "__main__":
    main()
