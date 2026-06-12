#!/usr/bin/env python3
"""``alfred-serve`` - headless localhost JSON API over the Alfred fleet state.

Runs a tiny FastAPI app under uvicorn. Binds to ``127.0.0.1`` by default and
reads state from ``$ALFRED_HOME/state`` (or ``~/.alfred/state``). The native
client (and its browser dev mode) is the only UI; this process serves JSON
and SSE only.

Usage::

    python bin/alfred-serve.py
    python bin/alfred-serve.py --port 7010

Fleet views are read-only; mutating POSTs require the per-launch
``X-Alfred-Token``. Binding to ``0.0.0.0`` is allowed but discouraged, the
API exposes paths and event payloads that may contain repo URLs or other
operator context.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Resolve lib/ regardless of how the script was invoked. In the
# installed fleet layout the script lives in ``$ALFRED_HOME/bin/`` and
# the library in ``$ALFRED_HOME/lib/``; in a checkout the script lives
# in ``<repo>/bin/`` and the library in ``<repo>/lib/``. Probe both.
_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "lib", Path(os.environ.get("ALFRED_HOME", "")) / "lib"):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

logger = logging.getLogger("alfred-serve")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-serve",
        description="Headless localhost JSON/SSE API over $ALFRED_HOME/state.",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; use 0.0.0.0 only on trusted LANs)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=7000,
        help="bind port (default: 7000)",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="uvicorn log level (default: info)",
    )
    # Accepted for backward compatibility: serve is headless now and never
    # opens a browser, so this is a no-op. The `bin/alfred serve --no-browser`
    # wrapper still forwards it, so the flag must stay registered or that
    # command exits with argparse error 2 (unrecognized arguments).
    p.add_argument(
        "--no-browser",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write(
            "alfred-serve: missing dependencies. Install the serve extras:\n"
            "    pip install 'alfred-os[serve]'\n"
        )
        return 2

    # Local import so the missing-deps message above fires before any
    # ImportError from the server package itself.
    from server import FilesystemReader, create_app

    reader = FilesystemReader()
    if not reader.state_root.exists():
        logger.warning(
            "state root %s does not exist; the API will serve empty state until an agent fires",
            reader.state_root,
        )

    app = create_app(reader)

    url = f"http://{args.host}:{args.port}/"
    logger.info("alfred-serve listening on %s (state=%s)", url, reader.state_root)

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
