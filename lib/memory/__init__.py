"""Alfred memory-provider layer.

A thin Protocol that lets Alfred chain memory backends. The default shipping
chain is Redis Agent Memory for semantic recalled lessons, followed by the
in-tree :mod:`fleet_brain` operational ledger. Operators can chain additional
read-only backends (a personal knowledge base, a team wiki shim, anything that
can answer ``recall``) by setting ``ALFRED_MEMORY_PROVIDERS`` and any
provider-specific env vars.

Design rules:

* The Protocol surface is intentionally tiny: ``recall`` plus an
  optional ``reflect``. New providers implement the Protocol and
  register themselves in :mod:`alfred.memory.config`.
* Read-only providers are first-class. ``reflect`` may raise
  :class:`NotImplementedError`; the chained provider handles the
  exception by falling through to the next writer.
* OSS installs get Redis Agent Memory plus FleetBrain by default. Personal
  knowledge-base paths are not wired up unless the operator explicitly opts in
  via env.
* Zero new third-party dependencies; everything is stdlib.

Public surface:

* :class:`MemoryProvider` -- the Protocol every backend implements.
* :class:`Lesson` -- re-exported from :mod:`fleet_brain` so callers
  do not need to import the concrete brain to type their code.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

from fleet_brain import Lesson, Severity

__all__ = [
    "Lesson",
    "MemoryProvider",
    "Severity",
]


@runtime_checkable
class MemoryProvider(Protocol):
    """Contract every memory backend implements.

    Implementations should be safe to call from multiple agents on
    the same host. Read providers can choose to raise
    :class:`NotImplementedError` from :meth:`reflect`; chained
    providers will catch it and try the next writer.

    The surface is deliberately tiny:

    * :meth:`recall` -- pull lessons the next firing should read.
    * :meth:`reflect` -- file a lesson for next time (optional).

    Concrete backends may expose richer APIs of their own; chained
    runners only depend on this Protocol.
    """

    name: str
    """Stable identifier (e.g. ``"fleet"``, ``"gbrain"``). Used in
    logs and the config registry."""

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        """Return up to ``limit`` lessons matching the filters.

        Returning an empty list is the normal "I have nothing" answer;
        chained providers use that signal to fall through.
        """
        ...

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        firing_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        """Persist a new lesson. May raise :class:`NotImplementedError`
        on read-only backends."""
        ...
