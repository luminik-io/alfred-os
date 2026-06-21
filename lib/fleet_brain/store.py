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

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol

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
MemoryCandidateStatus = Literal["candidate", "validated", "rejected", "retired"]
GitHubItemKind = Literal["issue", "pr"]
GitHubItemState = Literal["open", "closed", "merged", "unknown"]
WorkerStatus = Literal["running", "ok", "failed", "stale", "cancelled"]


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
class MemoryCandidate:
    """A proposed memory awaiting operator or policy review."""

    id: str
    codename: str
    repo: str
    body: str
    tags: list[str]
    severity: Severity
    source: str
    source_firing_id: str | None
    evidence: str
    confidence: float
    status: MemoryCandidateStatus
    created_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    promoted_lesson_id: str | None = None


@dataclass(frozen=True)
class FailureEvent:
    """One normalized non-success outcome from a firing or integration."""

    id: str
    codename: str
    subtype: str
    summary: str
    severity: Severity
    created_at: datetime
    repo: str | None = None
    firing_id: str | None = None
    engine: str | None = None


@dataclass(frozen=True)
class GitHubItem:
    """Cached GitHub issue/PR state collected by the poller."""

    id: str
    repo: str
    number: int
    kind: GitHubItemKind
    state: GitHubItemState
    title: str
    url: str
    labels: list[str]
    updated_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    head_ref: str | None = None
    base_ref: str | None = None
    bundle_slug: str | None = None
    additions: int | None = 0
    deletions: int | None = 0
    line_metrics_seen_at: datetime | None = None


@dataclass(frozen=True)
class BundleItem:
    """One issue/PR member of an ``agent:bundle:<slug>`` bundle."""

    id: str
    bundle_slug: str
    repo: str
    item_kind: GitHubItemKind
    number: int
    state: GitHubItemState
    title: str
    url: str
    labels: list[str]
    updated_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class WorkerHeartbeat:
    """Last-seen liveness row for a worker firing."""

    id: str
    codename: str
    firing_id: str
    status: WorkerStatus
    started_at: datetime
    heartbeat_at: datetime
    repo: str | None = None
    pid: int | None = None
    detail: str = ""


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

    def count_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
    ) -> int: ...

    def insert_memory_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate: ...

    def get_memory_candidate(self, candidate_id: str) -> MemoryCandidate | None: ...

    def update_memory_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate: ...

    def list_memory_candidates(
        self,
        status: MemoryCandidateStatus | None = None,
        repo: str | None = None,
        codename: str | None = None,
        limit: int = 50,
    ) -> list[MemoryCandidate]: ...

    def insert_failure_event(self, event: FailureEvent) -> FailureEvent: ...

    def list_failure_events(
        self,
        repo: str | None = None,
        codename: str | None = None,
        subtype: str | None = None,
        limit: int = 50,
    ) -> list[FailureEvent]: ...

    def upsert_github_item(self, item: GitHubItem) -> GitHubItem: ...

    def list_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        limit: int = 50,
    ) -> list[GitHubItem]: ...

    def count_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
    ) -> int: ...

    def sum_github_changed_lines(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
    ) -> int: ...

    def upsert_bundle_item(self, item: BundleItem) -> BundleItem: ...

    def list_bundle_items(
        self,
        bundle_slug: str | None = None,
        state: GitHubItemState | None = None,
        limit: int = 50,
    ) -> list[BundleItem]: ...

    def upsert_worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat: ...

    def list_worker_heartbeats(
        self,
        codename: str | None = None,
        status: WorkerStatus | None = None,
        limit: int = 50,
    ) -> list[WorkerHeartbeat]: ...

    def stats(self) -> dict[str, int]: ...


# ---------------------------------------------------------------------------
# SQLite implementation.
# ---------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Agent-authorship signals for github_items. The poller (bin/fleet-github-poll)
# stores EVERY PR it sees from `gh pr list`, including operator- and bot-opened
# PRs, so a plain `kind="pr"` count would over-report. An item is treated as
# agent-authored when it carries the framework's provenance label
# ``agent:authored`` (set on PR open, see lib/labels.AUTHORED) OR its head branch
# uses one of the agent branch-name prefixes the fleet pushes from. Either signal
# alone is sufficient; both are written by the poller into columns that already
# exist (labels_json, head_ref), so no schema or poller change is needed and the
# count stays an exact COUNT(*) (never bounded by the list 500-row cap).
# Older rows that predate the agent:authored label are still matched by the
# branch-prefix signal; a row with neither is counted as NOT agent-authored
# (conservative: we never claim a PR Alfred did not open).
AGENT_AUTHORED_LABEL: Final[str] = "agent:authored"

# Branch-name prefixes the fleet's agents push PR head refs from. Kept in sync
# with lib/shipped_board._DEFAULT_AGENT_BRANCH_PREFIXES.
AGENT_BRANCH_PREFIXES: Final[tuple[str, ...]] = (
    "alfred/",
    "alfred-nightly/",
    "automerge/",
    "bane/",
    "batman/",
    "damian/",
    "lucius/",
    "nightwing/",
    "rasalghul/",
    "robin/",
)


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

    def count_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
    ) -> int:
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
        sql = f"SELECT COUNT(*) FROM file_touches {where_clause}"
        with self._connect() as conn:
            (total,) = conn.execute(sql, params).fetchone()
            return int(total)

    # ----- memory candidates -------------------------------------------

    def insert_memory_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO memory_candidates "
                "(id, codename, repo, body, tags_json, severity, source, "
                " source_firing_id, evidence, confidence, status, created_at, "
                " reviewed_at, reviewed_by, review_note, promoted_lesson_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.id,
                    candidate.codename,
                    candidate.repo,
                    candidate.body,
                    _tags_to_json(candidate.tags),
                    candidate.severity,
                    candidate.source,
                    candidate.source_firing_id,
                    candidate.evidence,
                    float(candidate.confidence),
                    candidate.status,
                    _to_iso(candidate.created_at),
                    _to_iso(candidate.reviewed_at) if candidate.reviewed_at else None,
                    candidate.reviewed_by,
                    candidate.review_note,
                    candidate.promoted_lesson_id,
                ),
            )
        return candidate

    def get_memory_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, codename, repo, body, tags_json, severity, source, "
                "source_firing_id, evidence, confidence, status, created_at, "
                "reviewed_at, reviewed_by, review_note, promoted_lesson_id "
                "FROM memory_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            return None if row is None else _row_to_memory_candidate(row)

    def update_memory_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self._connect() as conn, conn:
            conn.execute(
                "UPDATE memory_candidates SET "
                "codename = ?, repo = ?, body = ?, tags_json = ?, severity = ?, "
                "source = ?, source_firing_id = ?, evidence = ?, confidence = ?, "
                "status = ?, created_at = ?, reviewed_at = ?, reviewed_by = ?, "
                "review_note = ?, promoted_lesson_id = ? "
                "WHERE id = ?",
                (
                    candidate.codename,
                    candidate.repo,
                    candidate.body,
                    _tags_to_json(candidate.tags),
                    candidate.severity,
                    candidate.source,
                    candidate.source_firing_id,
                    candidate.evidence,
                    float(candidate.confidence),
                    candidate.status,
                    _to_iso(candidate.created_at),
                    _to_iso(candidate.reviewed_at) if candidate.reviewed_at else None,
                    candidate.reviewed_by,
                    candidate.review_note,
                    candidate.promoted_lesson_id,
                    candidate.id,
                ),
            )
        return candidate

    def list_memory_candidates(
        self,
        status: MemoryCandidateStatus | None = None,
        repo: str | None = None,
        codename: str | None = None,
        limit: int = 50,
    ) -> list[MemoryCandidate]:
        wheres: list[str] = []
        params: list[object] = []
        if status:
            wheres.append("status = ?")
            params.append(status)
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if codename:
            wheres.append("codename = ?")
            params.append(codename)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            "SELECT id, codename, repo, body, tags_json, severity, source, "
            "source_firing_id, evidence, confidence, status, created_at, "
            "reviewed_at, reviewed_by, review_note, promoted_lesson_id "
            f"FROM memory_candidates {where_clause} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_memory_candidate(r) for r in rows]

    # ----- failure events ----------------------------------------------

    def insert_failure_event(self, event: FailureEvent) -> FailureEvent:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO failure_events "
                "(id, codename, repo, firing_id, subtype, summary, engine, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.codename,
                    event.repo,
                    event.firing_id,
                    event.subtype,
                    event.summary,
                    event.engine,
                    event.severity,
                    _to_iso(event.created_at),
                ),
            )
        return event

    def list_failure_events(
        self,
        repo: str | None = None,
        codename: str | None = None,
        subtype: str | None = None,
        limit: int = 50,
    ) -> list[FailureEvent]:
        wheres: list[str] = []
        params: list[object] = []
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if codename:
            wheres.append("codename = ?")
            params.append(codename)
        if subtype:
            wheres.append("subtype = ?")
            params.append(subtype)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            "SELECT id, codename, repo, firing_id, subtype, summary, engine, severity, created_at "
            f"FROM failure_events {where_clause} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_failure_event(r) for r in rows]

    # ----- GitHub state -------------------------------------------------

    def upsert_github_item(self, item: GitHubItem) -> GitHubItem:
        line_metrics_seen_at = (
            item.last_seen_at if item.additions is not None or item.deletions is not None else None
        )
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO github_items "
                "(id, repo, number, kind, state, title, url, labels_json, updated_at, "
                " last_seen_at, closed_at, merged_at, head_ref, base_ref, bundle_slug, "
                " additions, deletions, line_metrics_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  state = excluded.state, "
                "  title = excluded.title, "
                "  url = excluded.url, "
                "  labels_json = excluded.labels_json, "
                "  updated_at = excluded.updated_at, "
                "  last_seen_at = excluded.last_seen_at, "
                "  closed_at = excluded.closed_at, "
                "  merged_at = excluded.merged_at, "
                "  head_ref = excluded.head_ref, "
                "  base_ref = excluded.base_ref, "
                "  bundle_slug = excluded.bundle_slug, "
                "  additions = CASE "
                "    WHEN ? IS NULL THEN github_items.additions "
                "    ELSE excluded.additions "
                "  END, "
                "  deletions = CASE "
                "    WHEN ? IS NULL THEN github_items.deletions "
                "    ELSE excluded.deletions "
                "  END, "
                "  line_metrics_seen_at = CASE "
                "    WHEN ? IS NULL AND ? IS NULL THEN github_items.line_metrics_seen_at "
                "    ELSE excluded.line_metrics_seen_at "
                "  END",
                (
                    item.id,
                    item.repo,
                    int(item.number),
                    item.kind,
                    item.state,
                    item.title,
                    item.url,
                    _tags_to_json(item.labels),
                    _to_iso(item.updated_at),
                    _to_iso(item.last_seen_at),
                    _to_iso(item.closed_at) if item.closed_at else None,
                    _to_iso(item.merged_at) if item.merged_at else None,
                    item.head_ref,
                    item.base_ref,
                    item.bundle_slug,
                    max(0, int(item.additions)) if item.additions is not None else 0,
                    max(0, int(item.deletions)) if item.deletions is not None else 0,
                    _to_iso(line_metrics_seen_at) if line_metrics_seen_at else None,
                    item.additions,
                    item.deletions,
                    item.additions,
                    item.deletions,
                ),
            )
        return item

    def list_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        limit: int = 50,
    ) -> list[GitHubItem]:
        wheres: list[str] = []
        params: list[object] = []
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if kind:
            wheres.append("kind = ?")
            params.append(kind)
        if state:
            wheres.append("state = ?")
            params.append(state)
        if bundle_slug:
            wheres.append("bundle_slug = ?")
            params.append(bundle_slug)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            "SELECT id, repo, number, kind, state, title, url, labels_json, updated_at, "
            "last_seen_at, closed_at, merged_at, head_ref, base_ref, bundle_slug, "
            "additions, deletions, line_metrics_seen_at "
            f"FROM github_items {where_clause} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_github_item(r) for r in rows]

    def count_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
    ) -> int:
        wheres: list[str] = []
        params: list[object] = []
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if kind:
            wheres.append("kind = ?")
            params.append(kind)
        if state:
            wheres.append("state = ?")
            params.append(state)
        if bundle_slug:
            wheres.append("bundle_slug = ?")
            params.append(bundle_slug)
        if authored_only:
            authored_sql, authored_params = _authored_predicate()
            wheres.append(authored_sql)
            params.extend(authored_params)
        if agent_labeled_only:
            agent_sql, agent_params = _agent_labeled_predicate()
            wheres.append(agent_sql)
            params.extend(agent_params)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = f"SELECT COUNT(*) FROM github_items {where_clause}"
        with self._connect() as conn:
            (total,) = conn.execute(sql, params).fetchone()
            return int(total)

    def sum_github_changed_lines(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
    ) -> int:
        wheres: list[str] = []
        params: list[object] = []
        if repo:
            wheres.append("repo = ?")
            params.append(repo)
        if kind:
            wheres.append("kind = ?")
            params.append(kind)
        if state:
            wheres.append("state = ?")
            params.append(state)
        if bundle_slug:
            wheres.append("bundle_slug = ?")
            params.append(bundle_slug)
        if authored_only:
            authored_sql, authored_params = _authored_predicate()
            wheres.append(authored_sql)
            params.extend(authored_params)
        if agent_labeled_only:
            agent_sql, agent_params = _agent_labeled_predicate()
            wheres.append(agent_sql)
            params.extend(agent_params)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            "SELECT COALESCE(SUM(CASE "
            "WHEN line_metrics_seen_at IS NULL THEN 0 "
            "ELSE additions + deletions END), 0) "
            f"FROM github_items {where_clause}"
        )
        with self._connect() as conn:
            (total,) = conn.execute(sql, params).fetchone()
            return max(0, int(total or 0))

    # ----- bundle items -------------------------------------------------

    def upsert_bundle_item(self, item: BundleItem) -> BundleItem:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO bundle_items "
                "(id, bundle_slug, repo, item_kind, number, state, title, url, "
                " labels_json, updated_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  state = excluded.state, "
                "  title = excluded.title, "
                "  url = excluded.url, "
                "  labels_json = excluded.labels_json, "
                "  updated_at = excluded.updated_at, "
                "  last_seen_at = excluded.last_seen_at",
                (
                    item.id,
                    item.bundle_slug,
                    item.repo,
                    item.item_kind,
                    int(item.number),
                    item.state,
                    item.title,
                    item.url,
                    _tags_to_json(item.labels),
                    _to_iso(item.updated_at),
                    _to_iso(item.last_seen_at),
                ),
            )
        return item

    def list_bundle_items(
        self,
        bundle_slug: str | None = None,
        state: GitHubItemState | None = None,
        limit: int = 50,
    ) -> list[BundleItem]:
        wheres: list[str] = []
        params: list[object] = []
        if bundle_slug:
            wheres.append("bundle_slug = ?")
            params.append(bundle_slug)
        if state:
            wheres.append("state = ?")
            params.append(state)
        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        sql = (
            "SELECT id, bundle_slug, repo, item_kind, number, state, title, url, "
            "labels_json, updated_at, last_seen_at "
            f"FROM bundle_items {where_clause} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_bundle_item(r) for r in rows]

    # ----- worker heartbeats -------------------------------------------

    def upsert_worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat:
        with self._connect() as conn, conn:
            conn.execute(
                "INSERT INTO worker_heartbeats "
                "(id, codename, firing_id, status, started_at, heartbeat_at, repo, pid, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET "
                "  status = excluded.status, "
                "  heartbeat_at = excluded.heartbeat_at, "
                "  repo = excluded.repo, "
                "  pid = excluded.pid, "
                "  detail = excluded.detail",
                (
                    heartbeat.id,
                    heartbeat.codename,
                    heartbeat.firing_id,
                    heartbeat.status,
                    _to_iso(heartbeat.started_at),
                    _to_iso(heartbeat.heartbeat_at),
                    heartbeat.repo,
                    heartbeat.pid,
                    heartbeat.detail,
                ),
            )
        return heartbeat

    def list_worker_heartbeats(
        self,
        codename: str | None = None,
        status: WorkerStatus | None = None,
        limit: int = 50,
    ) -> list[WorkerHeartbeat]:
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
            "SELECT id, codename, firing_id, status, started_at, heartbeat_at, repo, pid, detail "
            f"FROM worker_heartbeats {where_clause} "
            "ORDER BY heartbeat_at DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_worker_heartbeat(r) for r in rows]

    # ----- stats ---------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Cheap rollup for ``alfred brain status``."""
        with self._connect() as conn:
            (lessons,) = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()
            (firings,) = conn.execute("SELECT COUNT(*) FROM firing_logs").fetchone()
            (file_touches,) = conn.execute("SELECT COUNT(*) FROM file_touches").fetchone()
            (memory_candidates,) = conn.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()
            (open_candidates,) = conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE status = 'candidate'"
            ).fetchone()
            (failure_events,) = conn.execute("SELECT COUNT(*) FROM failure_events").fetchone()
            (github_items,) = conn.execute("SELECT COUNT(*) FROM github_items").fetchone()
            (bundle_items,) = conn.execute("SELECT COUNT(*) FROM bundle_items").fetchone()
            (worker_heartbeats,) = conn.execute("SELECT COUNT(*) FROM worker_heartbeats").fetchone()
            (running_workers,) = conn.execute(
                "SELECT COUNT(*) FROM worker_heartbeats WHERE status = 'running'"
            ).fetchone()
            (notes,) = conn.execute("SELECT COUNT(*) FROM repo_notes").fetchone()
            (tags,) = conn.execute("SELECT COUNT(DISTINCT tag) FROM lesson_tags").fetchone()
            (codenames,) = conn.execute("SELECT COUNT(DISTINCT codename) FROM lessons").fetchone()
            (repos,) = conn.execute("SELECT COUNT(DISTINCT repo) FROM lessons").fetchone()
        return {
            "lessons": int(lessons),
            "firings": int(firings),
            "file_touches": int(file_touches),
            "memory_candidates": int(memory_candidates),
            "memory_candidates_open": int(open_candidates),
            "failure_events": int(failure_events),
            "github_items": int(github_items),
            "bundle_items": int(bundle_items),
            "worker_heartbeats": int(worker_heartbeats),
            "workers_running": int(running_workers),
            "repo_notes": int(notes),
            "tags": int(tags),
            "codenames": int(codenames),
            "repos": int(repos),
        }


def _authored_predicate() -> tuple[str, list[object]]:
    """Build a SQL fragment that matches agent-authored github_items.

    A row is agent-authored when EITHER its ``labels_json`` contains the
    ``agent:authored`` provenance label OR its ``head_ref`` starts with one of
    the fleet's agent branch prefixes. Both columns are already populated by the
    poller, so this is a pure read-side filter (an exact ``COUNT(*)`` predicate,
    not bounded by the list 500-row cap).

    ``labels_json`` is compact JSON of a sorted string list (see
    ``_tags_to_json``), e.g. ``["agent:authored","bug"]`` with no spaces, so a
    membership test on the quoted label is reliable. The branch match is a prefix
    test per known agent prefix.

    Both tests use ``GLOB`` rather than ``LIKE`` because SQLite's ``LIKE`` is
    case-INSENSITIVE for ASCII and -- crucially -- a ``COLLATE`` clause does NOT
    change that (``LIKE`` ignores collation for case folding; only ``GLOB`` or the
    connection-wide ``PRAGMA case_sensitive_like`` is case-sensitive). ``GLOB`` is
    natively case-SENSITIVE and locally scoped to this fragment. Without it an
    operator branch like ``Lucius/fix`` (capital L) would satisfy a ``lucius/``
    prefix and that PR would be miscounted as Alfred-authored, inflating the
    authored count. Case-sensitivity mirrors the list-fallback predicate
    ``proof_telemetry._row_is_agent_authored`` (exact ``label in labels`` and
    case-sensitive ``head_ref.startswith(prefix)``) so the SQL and Python paths
    agree.

    ``GLOB`` treats ``*``, ``?`` and ``[`` as metacharacters (it has no LIKE-style
    ``ESCAPE`` clause), so each literal segment is escaped via single-char
    ``[x]`` brackets before the trailing ``*`` prefix wildcard. Returns
    ``(sql_fragment, params)`` to splice into a WHERE clause; the fragment is
    fully parenthesized so it ANDs safely with other filters.
    """
    clauses: list[str] = ["labels_json GLOB ?"]
    params: list[object] = [f'*"{_glob_escape(AGENT_AUTHORED_LABEL)}"*']
    for prefix in AGENT_BRANCH_PREFIXES:
        clauses.append("head_ref GLOB ?")
        # Escape GLOB metacharacters in the prefix so it is matched literally,
        # then append a trailing wildcard for "starts with".
        params.append(f"{_glob_escape(prefix)}*")
    return "(" + " OR ".join(clauses) + ")", params


def _agent_labeled_predicate() -> tuple[str, list[object]]:
    """Build a SQL fragment that matches rows with any ``agent:*`` label."""

    return "labels_json GLOB ?", ['*"agent:[^"]*"*']


def _glob_escape(text: str) -> str:
    """Escape GLOB metacharacters (``*``, ``?``, ``[``) for a literal match.

    GLOB has no ``ESCAPE`` clause, so a metacharacter is matched literally by
    wrapping it in a single-character class ``[x]`` (``[[]`` for ``[``). The known
    agent prefixes and label contain none of these today; this keeps the predicate
    correct if one is ever added.
    """
    out: list[str] = []
    for ch in text:
        if ch in "*?[":
            out.append(f"[{ch}]")
        else:
            out.append(ch)
    return "".join(out)


def _tags_to_json(tags: list[str]) -> str:
    return json.dumps(sorted({t.strip() for t in tags if t.strip()}), separators=(",", ":"))


def _tags_from_json(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return sorted({str(tag).strip() for tag in value if str(tag).strip()})


def _row_to_memory_candidate(row: tuple) -> MemoryCandidate:
    (
        candidate_id,
        codename,
        repo,
        body,
        tags_json,
        severity,
        source,
        source_firing_id,
        evidence,
        confidence,
        status,
        created_at,
        reviewed_at,
        reviewed_by,
        review_note,
        promoted_lesson_id,
    ) = row
    return MemoryCandidate(
        id=candidate_id,
        codename=codename,
        repo=repo,
        body=body,
        tags=_tags_from_json(tags_json),
        severity=severity,
        source=source,
        source_firing_id=source_firing_id,
        evidence=evidence,
        confidence=float(confidence),
        status=status,
        created_at=_from_iso(created_at),
        reviewed_at=_from_iso(reviewed_at) if reviewed_at else None,
        reviewed_by=reviewed_by,
        review_note=review_note,
        promoted_lesson_id=promoted_lesson_id,
    )


def _row_to_failure_event(row: tuple) -> FailureEvent:
    event_id, codename, repo, firing_id, subtype, summary, engine, severity, created_at = row
    return FailureEvent(
        id=event_id,
        codename=codename,
        repo=repo,
        firing_id=firing_id,
        subtype=subtype,
        summary=summary,
        engine=engine,
        severity=severity,
        created_at=_from_iso(created_at),
    )


def _row_to_github_item(row: tuple) -> GitHubItem:
    (
        item_id,
        repo,
        number,
        kind,
        state,
        title,
        url,
        labels_json,
        updated_at,
        last_seen_at,
        closed_at,
        merged_at,
        head_ref,
        base_ref,
        bundle_slug,
        additions,
        deletions,
        line_metrics_seen_at,
    ) = row
    return GitHubItem(
        id=item_id,
        repo=repo,
        number=int(number),
        kind=kind,
        state=state,
        title=title,
        url=url,
        labels=_tags_from_json(labels_json),
        updated_at=_from_iso(updated_at),
        last_seen_at=_from_iso(last_seen_at),
        closed_at=_from_iso(closed_at) if closed_at else None,
        merged_at=_from_iso(merged_at) if merged_at else None,
        head_ref=head_ref,
        base_ref=base_ref,
        bundle_slug=bundle_slug,
        additions=max(0, int(additions or 0)),
        deletions=max(0, int(deletions or 0)),
        line_metrics_seen_at=_from_iso(line_metrics_seen_at) if line_metrics_seen_at else None,
    )


def _row_to_bundle_item(row: tuple) -> BundleItem:
    (
        item_id,
        bundle_slug,
        repo,
        item_kind,
        number,
        state,
        title,
        url,
        labels_json,
        updated_at,
        last_seen_at,
    ) = row
    return BundleItem(
        id=item_id,
        bundle_slug=bundle_slug,
        repo=repo,
        item_kind=item_kind,
        number=int(number),
        state=state,
        title=title,
        url=url,
        labels=_tags_from_json(labels_json),
        updated_at=_from_iso(updated_at),
        last_seen_at=_from_iso(last_seen_at),
    )


def _row_to_worker_heartbeat(row: tuple) -> WorkerHeartbeat:
    item_id, codename, firing_id, status, started_at, heartbeat_at, repo, pid, detail = row
    return WorkerHeartbeat(
        id=item_id,
        codename=codename,
        firing_id=firing_id,
        status=status,
        started_at=_from_iso(started_at),
        heartbeat_at=_from_iso(heartbeat_at),
        repo=repo,
        pid=int(pid) if pid is not None else None,
        detail=detail or "",
    )
