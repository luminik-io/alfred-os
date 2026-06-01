"""``alfred serve`` server package.

A small, localhost-only read-only dashboard over ``$ALFRED_HOME/state``.
The CLI driver lives in ``bin/alfred-serve.py``; the bulk of the logic is
split into:

- :mod:`lib.server.reader`     state reader (Protocol + filesystem impl)
- :mod:`lib.server.app`        FastAPI factory, takes a reader
- :mod:`lib.server.views`      route handlers (fleet, firings, detail)
"""

from __future__ import annotations

from .reader import AgentSummary, FilesystemReader, FiringRecord, FleetReader, PlanDraft

__all__ = [
    "AgentSummary",
    "FilesystemReader",
    "FiringRecord",
    "FleetReader",
    "PlanDraft",
    "create_app",
]


def __getattr__(name: str):
    """Load FastAPI-backed server pieces only when ``alfred serve`` asks."""

    if name == "create_app":
        from .app import create_app

        globals()["create_app"] = create_app
        return create_app
    raise AttributeError(name)
