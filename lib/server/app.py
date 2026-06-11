"""FastAPI app factory for ``alfred serve``.

The factory takes a :class:`FleetReader` so tests can swap the source of
truth. The default driver in ``bin/alfred-serve.py`` constructs a
:class:`FilesystemReader` and passes it in.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import views
from .formatting import friendly_time, short_firing_id, timestamp_title
from .reader import FleetReader

logger = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def create_app(reader: FleetReader) -> FastAPI:
    """Build the FastAPI application bound to ``reader``.

    The app is intentionally tiny: three GET routes, one static mount, no
    middleware beyond what FastAPI provides out of the box. It is meant to
    be served on ``127.0.0.1`` only; binding to any other interface is a
    deliberate choice the operator must make at the CLI level.
    """
    app = FastAPI(
        title="alfred serve",
        description="Localhost-only read-only dashboard over $ALFRED_HOME/state.",
        version="0.4.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["friendly_time"] = friendly_time
    templates.env.filters["timestamp_title"] = timestamp_title
    templates.env.filters["short_firing_id"] = short_firing_id

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Attach reader + templates to app.state so view functions can pull
    # them without needing a global. Keeps create_app the only place
    # wiring happens.
    app.state.reader = reader
    app.state.templates = templates

    # Mint a fresh per-launch token and persist it (0600) under the state root.
    # State-mutating POSTs require it via the X-Alfred-Token header, so a
    # drive-by same-origin localhost page (which cannot read the token file)
    # can never arm work or mutate fleet/trust/plan state.
    state_root = getattr(reader, "state_root", None)
    if isinstance(state_root, Path):
        try:
            views.ensure_server_token(state_root)
        except OSError as exc:
            # A serve start must not be blocked by a token-write failure; the
            # gate then fails closed (mutating POSTs return 403) rather than
            # silently downgrading to same-origin-only.
            logger.warning("could not write server token under %s: %s", state_root, exc)

    views.register_routes(app)
    return app
