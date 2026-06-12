"""FastAPI app factory for ``alfred serve``.

The factory takes a :class:`FleetReader` so tests can swap the source of
truth. The default driver in ``bin/alfred-serve.py`` constructs a
:class:`FilesystemReader` and passes it in.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from . import views
from .reader import FleetReader

logger = logging.getLogger(__name__)


def create_app(reader: FleetReader) -> FastAPI:
    """Build the FastAPI application bound to ``reader``.

    The app is a headless JSON API over ``$ALFRED_HOME/state``: the native
    client (and its browser dev mode) is the only UI. It is meant to be
    served on ``127.0.0.1`` only; binding to any other interface is a
    deliberate choice the operator must make at the CLI level.
    """
    app = FastAPI(
        title="alfred serve",
        description="Localhost-only JSON API over $ALFRED_HOME/state.",
        version="0.4.1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Attach the reader to app.state so view functions can pull it without
    # needing a global. Keeps create_app the only place wiring happens.
    app.state.reader = reader

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
