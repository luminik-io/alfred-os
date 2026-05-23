"""``alfred serve`` server package.

A small, localhost-only read-only dashboard over ``$ALFRED_HOME/state``.
The CLI driver lives in ``bin/alfred-serve.py``; the bulk of the logic is
split into:

- :mod:`lib.server.reader`     state reader (Protocol + filesystem impl)
- :mod:`lib.server.app`        FastAPI factory, takes a reader
- :mod:`lib.server.views`      route handlers (fleet, firings, detail)
"""

from __future__ import annotations

from .app import create_app
from .reader import FilesystemReader, FiringRecord, FleetReader, AgentSummary

__all__ = [
    "create_app",
    "FilesystemReader",
    "FiringRecord",
    "FleetReader",
    "AgentSummary",
]
