"""Concrete :class:`MemoryProvider` implementations.

Three providers ship in-tree:

* :class:`FleetBrainProvider` -- wraps the in-tree :mod:`fleet_brain`
  so the rest of Alfred only depends on the Protocol.
* :class:`ChainedMemoryProvider` -- consults a list of providers in
  order; the first non-empty ``recall`` wins. ``reflect`` writes to
  the first provider that does not raise :class:`NotImplementedError`.
* :class:`NullMemoryProvider` -- no-op fallback. Returned by
  :func:`alfred.memory.config.load_provider` when no provider is
  configured. Lets a runner depend on a non-optional
  :class:`MemoryProvider` field without branching on ``None``.

New providers go in their own module (e.g. ``gbrain_stub.py``) and
register themselves in :mod:`alfred.memory.config` via the provider
registry, never by editing this file -- Open-Closed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from fleet_brain import FleetBrain, Lesson, Severity

if TYPE_CHECKING:
    from . import MemoryProvider

_LOG = logging.getLogger(__name__)


@dataclass
class FleetBrainProvider:
    """Adapter that exposes :class:`FleetBrain` as a
    :class:`MemoryProvider`.

    Owns the underlying brain instance. Constructed by
    :func:`alfred.memory.config.load_provider` with the operator's
    configured SQLite path; tests inject an in-memory brain via
    ``brain=``.
    """

    brain: FleetBrain = field(default_factory=FleetBrain)
    name: str = "fleet"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FleetBrainProvider:
        """Build the local operational ledger from the same env map as config."""
        return cls(brain=FleetBrain.from_env(env))

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        return self.brain.recall(
            codename=codename,
            repo=repo,
            query=query,
            limit=limit,
        )

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
        return self.brain.reflect(
            codename=codename,
            repo=repo,
            body=body,
            tags=tags,
            severity=severity,
            firing_id=firing_id,
            created_at=created_at,
        )


@dataclass
class NullMemoryProvider:
    """No-op provider. ``recall`` returns ``[]``; ``reflect`` raises.

    Used when the operator explicitly disables memory. Keeps the runner
    code branch-free.
    """

    name: str = "null"

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        return []

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
        raise NotImplementedError(
            "NullMemoryProvider is read-only; configure a writable "
            "provider (e.g. fleet) to record lessons."
        )


@dataclass
class ChainedMemoryProvider:
    """Consults a list of providers in order.

    ``recall`` merges results from every readable provider in order. This keeps
    the default ``redis,fleet`` chain honest: Redis provides semantic recall,
    while freshly reviewed FleetBrain lessons still appear in prompts before a
    separate Redis sync has run.

    ``reflect`` writes to the first provider that does not raise
    :class:`NotImplementedError`. Read-only providers later in the
    chain are skipped silently.

    Construction is explicit: callers pass the ordered list. The
    config layer is responsible for parsing env into that list.
    """

    providers: list[MemoryProvider]
    name: str = "chained"

    def __post_init__(self) -> None:
        if not self.providers:
            raise ValueError(
                "ChainedMemoryProvider needs at least one provider; "
                "use NullMemoryProvider for a no-op default."
            )

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        out: list[Lesson] = []
        seen: set[str] = set()
        for provider in self.providers:
            try:
                lessons = provider.recall(
                    query=query,
                    codename=codename,
                    repo=repo,
                    limit=limit,
                )
            except Exception:
                # One flaky backend must not break the chain. Log and
                # try the next provider; the firing still gets context.
                _LOG.exception(
                    "memory.chained: provider %r recall raised; falling through",
                    provider.name,
                )
                continue
            for lesson in lessons:
                key = lesson.id or f"{lesson.codename}:{lesson.repo}:{lesson.body}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(lesson)
            if lessons:
                _LOG.debug(
                    "memory.chained: %r returned %d lesson(s)",
                    provider.name,
                    len(lessons),
                )
        return out[: max(1, int(limit))]

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
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return provider.reflect(
                    codename=codename,
                    repo=repo,
                    body=body,
                    tags=tags,
                    severity=severity,
                    firing_id=firing_id,
                    created_at=created_at,
                )
            except NotImplementedError as exc:
                last_error = exc
                _LOG.debug(
                    "memory.chained: provider %r is read-only; trying next",
                    provider.name,
                )
                continue
        raise NotImplementedError(
            "ChainedMemoryProvider: no writable provider in chain"
        ) from last_error
