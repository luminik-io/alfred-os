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

from .reader import FleetReader
from . import views

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
        version="0.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Attach reader + templates to app.state so view functions can pull
    # them without needing a global. Keeps create_app the only place
    # wiring happens.
    app.state.reader = reader
    app.state.templates = templates

    views.register_routes(app)
    return app
