"""Repository pattern over the fleet-brain SQLite file.

:class:`Store` is the Protocol that :class:`fleet_brain.FleetBrain`
depends on. :class:`SQLiteStore` is the only implementation today.
A future PGLite/AGE-backed store (v2) implements the same Protocol
and drops in via dependency injection — see
``docs/FLEET_BRAIN.md`` for the upgrade path.

Connections are intentionally short-lived (per-call open + close).
SQLite handles this trivially and it sidesteps thread-local state
when launchd / systemd agents share the same brain file.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from . import schema as schema_mod

# ---------------------------------------------------------------------------
# Entity dataclasses. Kept here (not in a separate ``models.py``) so the
# Store contract and the entity types stay in one file — the entities
# are an implementation concern of the store, not a separate domain
# layer. ``FleetBrain`` re-exports them as part of its public surface.
# ---------------------------------------------------------------------------

Severity = Literal["info", "warning", "blocker"]
FiringStatus = Literal["ok", "blocked", "partial", "silent"]
FileChangeType = Literal["added", "modified", "deleted", "renamed", "unknown"]


@dataclass(frozen=True)
class Lesson:
    """One recall-able fact a firing learned about a codename/repo."""

    id: str
    codename: str
    repo: str
    body: str
    tags: list[str]
    created_at: datetime
    firing_id: str | None
    severity: Severity = "info"


@dataclass(frozen=True)
class FiringLog:
    """One firing's audit row."""

    firing_id: str
    codename: str
    repo: str | None
    status: FiringStatus
    summary: str
    started_at: datetime
    finished_at: datetime
    cost_cents: int = 0
    pr_url: str | None = None
    sentinel: str | None = None


@dataclass(frozen=True)
class FileTouch:
    """One repo file an agent touched during a firing or PR."""

    id: str
    repo: str
    path: str
    codename: str
    touched_at: datetime
    firing_id: str | None = None
    pr_url: str | None = None
    change_type: FileChangeType = "modified"


@dataclass(frozen=True)
class RepoNote:
    """Free-text rollup for one repository."""

    repo: str
    body: str
    updated_at: datetime


# ---------------------------------------------------------------------------
# ID generation. ULID-like: 48 bits of millisecond timestamp + 80 bits of
# entropy, base32-Crockford encoded. Sortable by creation time,
# url-safe, stdlib-only.
# ---------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """Return a fresh 26-character ULID-style identifier."""
    ms = time.time_ns() // 1_000_000
    rand = secrets.randbits(80)
    value = (ms << 80) | rand
    out: list[str] = []
    for _ in range(26):
        out.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


# ---------------------------------------------------------------------------
# Path resolution. 12-factor: env-driven, with a single ``$ALFRED_HOME``
# fallback so an operator who set nothing still gets a sensible default.
# ---------------------------------------------------------------------------


def default_db_path() -> Path:
    """Resolve the SQLite path from environment.

    Order of precedence:

    1. ``ALFRED_FLEET_BRAIN_DB`` — explicit override.
    2. ``$ALFRED_HOME/fleet-brain.db``.
    3. ``~/.alfred/fleet-brain.db``.
    """
    explicit = os.environ.get("ALFRED_FLEET_BRAIN_DB")
    if explicit:
        return Path(explicit).expanduser()
    alfred_home = os.environ.get("ALFRED_HOME")
    if alfred_home:
        return Path(alfred_home).expanduser() / "fleet-brain.db"
    return Path.home() / ".alfred" / "fleet-brain.db"


# ---------------------------------------------------------------------------
# Store Protocol. The public FleetBrain API depends on this, not the
# concrete SQLite impl. v2 (PGLite + AGE) replaces SQLiteStore wholesale.
# ---------------------------------------------------------------------------


class Store(Protocol):
    """The persistence contract the public :class:`FleetBrain` depends on.

    Implementations must be re-entrant — every method takes care of
    its own connection management. Methods raise no custom exceptions
    today; SQLite errors surface unmodified so callers can attach
    their own retry policy.
    """

    def ensure_schema(self) -> None: ...

    def insert_lesson(self, lesson: Lesson) -> Lesson: ...

    def recall_lessons(
        self,
        codename: str | None,
        repo: str | None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[Lesson]: ...

    def list_lessons(self, limit: int | None = None) -> list[Lesson]: ...

    def get_lesson(self, lesson_id: str) -> Lesson | None: ...

    def delete_lesson(self, lesson_id: str) -> bool: ...

    def delete_lessons_before(self, cutoff: datetime) -> int: ...

    def upsert_repo_note(self, note: RepoNote) -> RepoNote: ...

    def get_repo_note(self, repo: str) -> RepoNote | None: ...

    def insert_firing_log(self, log: FiringLog) -> FiringLog: ...

    def list_firing_logs(
        self,
        codename: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 50,
    ) -> list[FiringLog]: ...

    def insert_file_touch(self, touch: FileTouch) -> FileTouch: ...

    def list_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[FileTouch]: ...

    def stats(self) -> dict[str, int]: ...


# ---------------------------------------------------------------------------
# SQLite implementation.
# ---------------------------------------------------------------------------


def _to_iso(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 with timezone, always UTC.

    SQLite has no native timezone-aware datetime type, so we store
    everything as ISO-8601 strings. Naive datetimes are assumed to be
    UTC — the brain is local-only single-process so this is safe.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _from_iso(s: str) -> datetime:
    """Parse the string back. Handles trailing ``Z`` for sloppy inputs."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass
class SQLiteStore:
    """SQLite-backed :class:`Store` implementation.

    Pass ``db_path=":memory:"`` for an in-process throwaway brain
    (tests). For shared in-memory across connections we'd need a
    URI like ``file::memory:?cache=shared``; the default per-call
    connection pattern intentionally doesn't support that — tests
    pin a single :class:`SQLiteStore` instance, not the path.
    """

    db_path: Path = field(default_factory=default_db_path)
    # When ``db_path`` is ``:memory:`` we cache the single connection
    # so the table contents survive between method calls. For on-disk
    # paths this stays ``None`` and every call opens a fresh handle.
    _memory_conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path) if not isinstance(self.db_path, Path) else self.db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection. In-memory stores reuse a single handle
        so successive calls see the same data; on-disk stores open a
        fresh connection per call (12-factor short-lived)."""
        if str(self.db_path) == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:")
                self._memory_conn.execute("PRAGMA foreign_keys = ON")
                # WAL is a no-op for ``:memory:`` but the synchronous setting
                # is harmless; keep the call shape symmetric with the disk
                # path so a future refactor doesn't drift the two branches.
                self._memory_conn.execute("PRAGMA journal_mode = WAL")
                self._memory_conn.execute("PRAGMA synchronous = NORMAL")
            yield self._memory_conn
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets concurrent readers proceed while a writer holds the
        # database, and lets two short-lived writers from sibling firings
        # (lucius + drake calling reflect() inside the same second) not
        # serialise. ``synchronous = NORMAL`` keeps WAL durable across
        # process crashes while halving the per-commit fsync cost.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            yield conn
        finally:
            conn.close()

    # ----- schema --------------------------------------------------------

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            schema_mod.ensure_schema(conn)

    # ----- lessons -------------------------------------------------------

    def insert_lesson(self, lesson: Lesson) -> Lesson:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO lessons "
                "(id, codename, repo, body, severity, firing_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    lesson.id,
                    lesson.codename,
                    lesson.repo,
                    lesson.body,
                    lesson.severity,
                    lesson.firing_id,
                    _to_iso(lesson.created_at),
                ),
            )
            if lesson.tags:
                conn.executemany(
                    "INSERT OR IGNORE INTO lesson_tags (lesson_id, tag) VALUES (?, ?)",
                    [(lesson.id, t) for t in lesson.tags],
                )
        return lesson

    def recall_lessons(
        self,
        codename: str | None,
        repo: str | None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[Lesson]:
        """Return the most relevant lessons, most-recent first.

        v1 relevance is a literal substring match on ``body`` when
        ``query`` is given. Vector / semantic recall is deferred to
        v2 — see ``docs/FLEET_BRAIN.md``.

        Either ``codename`` or ``repo`` may be ``None`` to widen the
        scope.
        """
        wheres: list[str] = []
        params: list[object] = []
        if codename:
            wheres.append("codename = ?")
            params.append(codename)
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if query:
            wheres.append("body LIKE ?")
            params.append(f"%{query}%")
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            f"SELECT id, codename, repo, body, severity, firing_id, created_at "
            f"FROM lessons {where_clause} "
            f"ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_lesson(conn, r) for r in rows]

    def list_lessons(self, limit: int | None = None) -> list[Lesson]:
        sql = (
            "SELECT id, codename, repo, body, severity, firing_id, created_at "
            "FROM lessons ORDER BY created_at DESC"
        )
        params: list[object] = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_lesson(conn, r) for r in rows]

    def get_lesson(self, lesson_id: str) -> Lesson | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, codename, repo, body, severity, firing_id, created_at "
                "FROM lessons WHERE id = ?",
                (lesson_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_lesson(conn, row)

    def delete_lesson(self, lesson_id: str) -> bool:
        with self._connect() as conn, conn:
            cur = conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))
            return cur.rowcount > 0

    def delete_lessons_before(self, cutoff: datetime) -> int:
        """GC helper. Returns the number of rows deleted."""
        with self._connect() as conn, conn:
            cur = conn.execute(
                "DELETE FROM lessons WHERE created_at < ?",
                (_to_iso(cutoff),),
            )
            return cur.rowcount

    @staticmethod
    def _row_to_lesson(conn: sqlite3.Connection, row: tuple) -> Lesson:
        lesson_id, codename, repo, body, severity, firing_id, created_at = row
        tags = [
            t[0]
            for t in conn.execute(
                "SELECT tag FROM lesson_tags WHERE lesson_id = ? ORDER BY tag",
                (lesson_id,),
            )
        ]
        return Lesson(
            id=lesson_id,
            codename=codename,
            repo=repo,
            body=body,
            tags=tags,
            created_at=_from_iso(created_at),
            firing_id=firing_id,
            severity=severity,
        )

    # ----- repo notes ----------------------------------------------------

    def upsert_repo_note(self, note: RepoNote) -> RepoNote:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO repo_notes (repo, body, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (repo) DO UPDATE SET "
                "  body = excluded.body, "
                "  updated_at = excluded.updated_at",
                (note.repo, note.body, _to_iso(note.updated_at)),
            )
        return note

    def get_repo_note(self, repo: str) -> RepoNote | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT repo, body, updated_at FROM repo_notes WHERE repo = ?",
                (repo,),
            ).fetchone()
            if not row:
                return None
            return RepoNote(repo=row[0], body=row[1], updated_at=_from_iso(row[2]))

    # ----- firing logs ---------------------------------------------------

    def insert_firing_log(self, log: FiringLog) -> FiringLog:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO firing_logs "
                "(firing_id, codename, repo, status, summary, "
                " started_at, finished_at, cost_cents, pr_url, sentinel) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (firing_id) DO UPDATE SET "
                "  status = excluded.status, "
                "  summary = excluded.summary, "
                "  finished_at = excluded.finished_at, "
                "  cost_cents = excluded.cost_cents, "
                "  pr_url = excluded.pr_url, "
                "  sentinel = excluded.sentinel",
                (
                    log.firing_id,
                    log.codename,
                    log.repo,
                    log.status,
                    log.summary,
                    _to_iso(log.started_at),
                    _to_iso(log.finished_at),
                    int(log.cost_cents),
                    log.pr_url,
                    log.sentinel,
                ),
            )
        return log

    def list_firing_logs(
        self,
        codename: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 50,
    ) -> list[FiringLog]:
        wheres: list[str] = []
        params: list[object] = []
        if codename:
            wheres.append("codename = ?")
            params.append(codename)
        if status:
            wheres.append("status = ?")
            params.append(status)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            f"SELECT firing_id, codename, repo, status, summary, "
            f"started_at, finished_at, cost_cents, pr_url, sentinel "
            f"FROM firing_logs {where_clause} "
            f"ORDER BY started_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                FiringLog(
                    firing_id=r[0],
                    codename=r[1],
                    repo=r[2],
                    status=r[3],
                    summary=r[4],
                    started_at=_from_iso(r[5]),
                    finished_at=_from_iso(r[6]),
                    cost_cents=int(r[7]),
                    pr_url=r[8],
                    sentinel=r[9],
                )
                for r in rows
            ]

    # ----- file touches -------------------------------------------------

    def insert_file_touch(self, touch: FileTouch) -> FileTouch:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO file_touches "
                "(id, repo, path, codename, firing_id, pr_url, change_type, touched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    touch.id,
                    touch.repo,
                    touch.path,
                    touch.codename,
                    touch.firing_id,
                    touch.pr_url,
                    touch.change_type,
                    _to_iso(touch.touched_at),
                ),
            )
        return touch

    def list_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[FileTouch]:
        wheres: list[str] = []
        params: list[object] = []
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if codename:
            wheres.append("codename = ?")
            params.append(codename)
        if path:
            wheres.append("path = ?")
            params.append(path)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            f"SELECT id, repo, path, codename, firing_id, pr_url, change_type, touched_at "
            f"FROM file_touches {where_clause} "
            f"ORDER BY touched_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                FileTouch(
                    id=r[0],
                    repo=r[1],
                    path=r[2],
                    codename=r[3],
                    firing_id=r[4],
                    pr_url=r[5],
                    change_type=r[6],
                    touched_at=_from_iso(r[7]),
                )
                for r in rows
            ]

    # ----- stats ---------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Cheap rollup for ``alfred brain status``."""
        with self._connect() as conn:
            (lessons,) = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()
            (firings,) = conn.execute("SELECT COUNT(*) FROM firing_logs").fetchone()
            (file_touches,) = conn.execute("SELECT COUNT(*) FROM file_touches").fetchone()
            (notes,) = conn.execute("SELECT COUNT(*) FROM repo_notes").fetchone()
            (tags,) = conn.execute("SELECT COUNT(DISTINCT tag) FROM lesson_tags").fetchone()
            (codenames,) = conn.execute("SELECT COUNT(DISTINCT codename) FROM lessons").fetchone()
            (repos,) = conn.execute("SELECT COUNT(DISTINCT repo) FROM lessons").fetchone()
        return {
            "lessons": int(lessons),
            "firings": int(firings),
            "file_touches": int(file_touches),
            "repo_notes": int(notes),
            "tags": int(tags),
            "codenames": int(codenames),
            "repos": int(repos),
        }
