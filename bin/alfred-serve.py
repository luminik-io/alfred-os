#!/usr/bin/env python3
"""``alfred-serve`` - localhost dashboard over the Alfred fleet state.

Runs a tiny FastAPI app under uvicorn. Binds to ``127.0.0.1`` by default,
reads state from ``$ALFRED_HOME/state`` (or ``~/.alfred/state``), and
ships fleet, firing, plan, and planning views.

Usage::

    python bin/alfred-serve.py
    python bin/alfred-serve.py --port 7010
    python bin/alfred-serve.py --host 0.0.0.0 --port 9000 --no-browser

Fleet views are read-only. The planning helper can save draft issue/spec
Markdown under ``$ALFRED_HOME/planning-drafts``. Binding to ``0.0.0.0`` is
allowed but discouraged, the dashboard exposes paths and event payloads
that may contain repo URLs or other operator context.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Resolve lib/ regardless of how the script was invoked. In the
# installed fleet layout the script lives in ``$ALFRED_HOME/bin/`` and
# the library in ``$ALFRED_HOME/lib/``; in a checkout the script lives
# in ``<repo>/bin/`` and the library in ``<repo>/lib/``. Prefer the
# script's own checkout so source-run servers cannot import stale
# deployed libraries just because ALFRED_HOME is set.
_HERE = Path(__file__).resolve().parent
for candidate in (
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
    # Processed last, so source checkout lib lands at sys.path[0].
    _HERE.parent / "lib",
):
    candidate_path = str(candidate)
    if candidate.is_dir():
        while candidate_path in sys.path:
            sys.path.remove(candidate_path)
        sys.path.insert(0, candidate_path)

logger = logging.getLogger("alfred-serve")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-serve",
        description="Localhost dashboard over $ALFRED_HOME/state.",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: 127.0.0.1; use 0.0.0.0 only on trusted LANs)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=7010,
        help="bind port (default: 7010)",
    )
    p.add_argument(
        "--no-browser",
        action="store_true",
        help="do not auto-open a browser tab",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="uvicorn log level (default: info)",
    )
    return p


def _open_browser_when_ready(url: str, *, delay: float = 0.6) -> None:
    """Open the dashboard in a browser after a small delay.

    The delay lets uvicorn finish binding so the user does not see a
    spurious connection-refused tab. Errors are swallowed; failing to
    open a browser must never block the server.
    """

    def _go() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:  # pragma: no cover  best-effort
            logger.debug("browser open failed: %s", exc)

    threading.Thread(target=_go, daemon=True).start()


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
            "alfred-serve: missing dependencies. Re-run install.sh, or install the "
            "dashboard deps into $ALFRED_HOME/venv:\n"
            '    uv pip install --python "$ALFRED_HOME/venv/bin/python" '
            "fastapi httpx uvicorn jinja2\n"
        )
        return 2

    # Local import so the missing-deps message above fires before any
    # ImportError from the server package itself.
    from server import FilesystemReader, create_app

    reader = FilesystemReader()
    if not reader.state_root.exists():
        logger.warning(
            "state root %s does not exist; the dashboard will render empty until an agent fires",
            reader.state_root,
        )

    app = create_app(reader)

    url = f"http://{args.host}:{args.port}/"
    logger.info("alfred-serve listening on %s (state=%s)", url, reader.state_root)
    if not args.no_browser and args.host in {"127.0.0.1", "localhost"}:
        _open_browser_when_ready(url)

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
