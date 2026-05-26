"""Alfred's fleet-brain: a local procedural-learning memory layer.

``fleet_brain`` records what each agent firing learned about a repo
or codename, then surfaces those lessons back to the next firing as
prepended prompt context. Storage is a single SQLite file under
``$ALFRED_HOME``; nothing ever leaves the host.

Quick start::

    from fleet_brain import FleetBrain

    brain = FleetBrain()
    brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="GraphQL schema lives in src/schema.graphql; tests live next to it.",
        tags=["graphql", "layout"],
    )
    lessons = brain.recall(codename="lucius", repo="your-org/api")
    for L in lessons:
        print(L.body)

Public surface:

* :class:`FleetBrain` — the main API: ``recall``, ``reflect``,
  ``firing_log``, ``record_file_touch``, ``note_repo``, ``forget``,
  ``export``.
* :class:`fleet_brain.store.Lesson`, :class:`FiringLog`,
  :class:`FileTouch`, :class:`RepoNote` — entity dataclasses,
  re-exported here.
* :class:`fleet_brain.store.Store` — the Protocol the public API
  depends on. The default impl is :class:`SQLiteStore`; a
  PGLite/AGE-backed impl drops in for v2.

PGLite + Apache AGE graph storage is the v2 target; see
``docs/FLEET_BRAIN.md`` for the upgrade path.

Privacy: the brain is a SQLite file in your ``$ALFRED_HOME``. It
never leaves your machine. The only outbound surface is the prompt
context Alfred prepends to a firing, which goes to Claude Code or
Codex on your existing CLI auth. No telemetry, no phone-home, no
cloud sync.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import (
    FileChangeType,
    FileTouch,
    FiringLog,
    FiringStatus,
    Lesson,
    RepoNote,
    Severity,
    SQLiteStore,
    Store,
    default_db_path,
    new_id,
)

__all__ = [
    "FileChangeType",
    "FileTouch",
    "FiringLog",
    "FiringStatus",
    "FleetBrain",
    "Lesson",
    "RepoNote",
    "SQLiteStore",
    "Severity",
    "Store",
    "default_db_path",
    "new_id",
]


_LOG = logging.getLogger(__name__)

# Cap recall output so a runaway codename can't blow up a prompt.
_RECALL_DEFAULT = 8
_RECALL_MAX = 50


class FleetBrain:
    """Local procedural-memory layer for the Alfred fleet.

    Operates on a SQLite file by default; inject a custom
    :class:`Store` (Dependency Inversion) for tests or for a future
    Postgres-backed implementation.

    Method names map to the operator-facing verbs:

    * :meth:`reflect` — file a lesson the firing learned.
    * :meth:`recall`  — pull lessons relevant to the next firing.
    * :meth:`firing_log` — record one firing's audit row.
    * :meth:`record_file_touch` — record a file changed by an agent.
    * :meth:`note_repo` — upsert a free-text repo summary.
    * :meth:`forget`  — remove a lesson by id.
    * :meth:`export`  — JSON-serializable snapshot for backup or
      cross-host export (the operator must do the transfer; the
      brain never phones home).
    """

    def __init__(self, store: Store | None = None, *, db_path: Path | str | None = None) -> None:
        if store is not None:
            self.store: Store = store
        else:
            resolved = Path(db_path) if db_path is not None else default_db_path()
            self.store = SQLiteStore(db_path=resolved)
        self.store.ensure_schema()

    # ----- write paths --------------------------------------------------

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        firing_id: str | None = None,
        severity: Severity = "info",
        lesson_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        """File a lesson the firing learned. Returns the persisted row.

        ``severity`` follows the same taxonomy as the fleet's Slack
        severity routing: ``info`` (recall-only context), ``warning``
        (worth bubbling into a future prompt), ``blocker`` (the next
        firing must read this before doing anything).
        """
        if not codename or not repo or not body:
            raise ValueError("reflect: codename, repo, and body are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"reflect: unknown severity {severity!r}")
        lesson = Lesson(
            id=lesson_id or new_id(),
            codename=codename,
            repo=repo,
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            created_at=created_at or datetime.now(UTC),
            firing_id=firing_id,
            severity=severity,
        )
        _LOG.debug("reflect: codename=%s repo=%s tags=%s", codename, repo, lesson.tags)
        return self.store.insert_lesson(lesson)

    def firing_log(
        self,
        *,
        firing_id: str,
        codename: str,
        status: FiringStatus,
        summary: str = "",
        repo: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        cost_cents: int = 0,
        pr_url: str | None = None,
        sentinel: str | None = None,
    ) -> FiringLog:
        """Persist one firing's audit row. Upserts on ``firing_id``."""
        if not firing_id or not codename:
            raise ValueError("firing_log: firing_id and codename are required")
        if status not in ("ok", "blocked", "partial", "silent"):
            raise ValueError(f"firing_log: unknown status {status!r}")
        now = datetime.now(UTC)
        log = FiringLog(
            firing_id=firing_id,
            codename=codename,
            repo=repo,
            status=status,
            summary=summary or "",
            started_at=started_at or now,
            finished_at=finished_at or now,
            cost_cents=int(cost_cents),
            pr_url=pr_url,
            sentinel=sentinel,
        )
        return self.store.insert_firing_log(log)

    def note_repo(self, *, repo: str, body: str, updated_at: datetime | None = None) -> RepoNote:
        """Upsert the free-text rollup for ``repo``."""
        if not repo or not body:
            raise ValueError("note_repo: repo and body are required")
        note = RepoNote(
            repo=repo,
            body=body.strip(),
            updated_at=updated_at or datetime.now(UTC),
        )
        return self.store.upsert_repo_note(note)

    def record_file_touch(
        self,
        *,
        repo: str,
        path: str,
        codename: str,
        firing_id: str | None = None,
        pr_url: str | None = None,
        change_type: FileChangeType = "modified",
        touch_id: str | None = None,
        touched_at: datetime | None = None,
    ) -> FileTouch:
        """Persist one repo file touched by an agent firing or PR."""
        if not repo or not path or not codename:
            raise ValueError("record_file_touch: repo, path, and codename are required")
        if change_type not in ("added", "modified", "deleted", "renamed", "unknown"):
            raise ValueError(f"record_file_touch: unknown change_type {change_type!r}")
        touch = FileTouch(
            id=touch_id or new_id(),
            repo=repo.strip(),
            path=path.strip(),
            codename=codename.strip(),
            firing_id=firing_id,
            pr_url=pr_url,
            change_type=change_type,
            touched_at=touched_at or datetime.now(UTC),
        )
        return self.store.insert_file_touch(touch)

    # ----- read paths ---------------------------------------------------

    def recall(
        self,
        codename: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        *,
        limit: int = _RECALL_DEFAULT,
    ) -> list[Lesson]:
        """Return the most-recent-first lessons matching the filters.

        Calling shape mirrors the prompt-prepend pattern: the runner
        does ``brain.recall(codename, repo)`` and dumps the bodies
        into the firing's system prompt.
        """
        clamped = max(1, min(int(limit), _RECALL_MAX))
        return self.store.recall_lessons(
            codename=codename,
            repo=repo,
            query=query,
            limit=clamped,
        )

    def get_repo_note(self, repo: str) -> RepoNote | None:
        return self.store.get_repo_note(repo)

    def list_lessons(self, limit: int | None = None) -> list[Lesson]:
        return self.store.list_lessons(limit=limit)

    def list_firings(
        self,
        codename: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 50,
    ) -> list[FiringLog]:
        return self.store.list_firing_logs(codename=codename, status=status, limit=limit)

    def list_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[FileTouch]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_file_touches(
            repo=repo,
            codename=codename,
            path=path,
            limit=clamped,
        )

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    # ----- delete paths -------------------------------------------------

    def forget(self, lesson_id: str) -> bool:
        """Delete a single lesson by id. Returns True if it existed."""
        return self.store.delete_lesson(lesson_id)

    def forget_before(self, *, days: int | None = None, before: datetime | None = None) -> int:
        """GC lessons older than ``days`` (or older than ``before``).

        Pass exactly one of ``days`` or ``before``.
        """
        if (days is None) == (before is None):
            raise ValueError("forget_before: pass exactly one of days= or before=")
        cutoff = before
        if cutoff is None:
            assert days is not None  # for mypy
            cutoff = datetime.now(UTC) - timedelta(days=int(days))
        return self.store.delete_lessons_before(cutoff)

    # ----- export -------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the entire brain.

        Format::

            {
              "schema_version": 1,
              "exported_at": "2026-05-23T...Z",
              "lessons": [{...}, ...],
              "repo_notes": [{...}, ...],
              "firings": [{...}, ...],
              "file_touches": [{...}, ...]
            }

        ``alfred brain export`` writes this to disk. Restoring is
        currently manual — re-run reflect/firing_log/note_repo on the
        target host. A round-trip ``import`` lives in the v2 roadmap.
        """
        from .schema import SCHEMA_VERSION

        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "lessons": [_serialize(asdict(L)) for L in self.list_lessons()],
            "repo_notes": [_serialize(asdict(n)) for n in self._all_repo_notes()],
            "firings": [_serialize(asdict(F)) for F in self.list_firings(limit=10_000)],
            "file_touches": [_serialize(asdict(T)) for T in self.list_file_touches(limit=10_000)],
        }

    def _all_repo_notes(self) -> list[RepoNote]:
        """Pull every repo note via a list_lessons-style sweep.

        The store doesn't expose a list method for notes today (the
        operator queries by repo); export needs everything, so we
        derive the repo set from existing lessons + any note we have.
        For now we use the lessons table as the source of repo keys.
        """
        seen: set[str] = set()
        out: list[RepoNote] = []
        for L in self.list_lessons():
            if L.repo in seen:
                continue
            seen.add(L.repo)
            note = self.store.get_repo_note(L.repo)
            if note is not None:
                out.append(note)
        return out


def _serialize(d: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON serialization: datetime -> ISO, everything else
    passes through. Used for export only."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(UTC).isoformat()
        else:
            out[k] = v
    return out
