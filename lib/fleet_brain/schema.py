"""SQLite schema for the fleet-brain memory layer.

Every statement is idempotent (``CREATE TABLE IF NOT EXISTS`` and
``CREATE INDEX IF NOT EXISTS``). The runtime calls
:func:`ensure_schema` on every connection, so a fresh checkout, a
re-deploy, and a third-party install all converge on the same schema
without ever running a destructive migration in code.

Open-Closed: adding a new entity is "append a new ``CREATE TABLE IF
NOT EXISTS`` here, then expose it through :mod:`fleet_brain.store`".
Never edit an existing column in-place; ship a new column via
``ALTER TABLE ... ADD COLUMN`` guarded by an introspection check
(see :func:`_add_column_if_missing` for the pattern).

The schema deliberately mirrors the entity model documented in
``docs/FLEET_BRAIN.md``:

* ``lessons`` — one row per recall-able fact a firing learned about
  a repo/codename.
* ``repo_notes`` — one row per repo: a free-text running summary
  that lessons roll up into. Upserted, not appended.
* ``firing_logs`` — one row per agent firing: status, summary,
  cost, sentinel for crash-debug. Audit trail.
* ``file_touches`` — one row per repo file an agent touched, optionally
  linked to a firing and PR.
* ``memory_candidates`` — reviewable lessons proposed by an agent or
  operator before they become prompt context.
* ``failure_events`` — normalized non-success outcomes, so repeated
  runner failures become searchable instead of Slack-only noise.
* ``github_items`` — cached GitHub issue/PR state from the poller.
* ``bundle_items`` — issue/PR membership keyed by ``agent:bundle:<slug>``.
* ``worker_heartbeats`` — last-seen worker liveness for stale-run detection.
* ``lesson_tags`` — many-to-many over ``lessons`` so a lesson can
  be filed under several taxonomy buckets without splitting rows.
* ``schema_version`` — single-row record of the applied schema
  version, for forward migrations.
"""

from __future__ import annotations

import sqlite3
from typing import Final

SCHEMA_VERSION: Final[int] = 7

# Each CREATE statement is a string in this tuple. We execute them
# one at a time so a syntax error in one statement does not silently
# truncate the rest.
_CREATE_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version    INTEGER NOT NULL PRIMARY KEY,
        applied_at TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lessons (
        id          TEXT    NOT NULL PRIMARY KEY,
        codename    TEXT    NOT NULL,
        repo        TEXT    NOT NULL,
        body        TEXT    NOT NULL,
        severity    TEXT    NOT NULL DEFAULT 'info',
        firing_id   TEXT,
        created_at  TEXT    NOT NULL,
        CHECK (severity IN ('info', 'warning', 'blocker'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS lesson_tags (
        lesson_id  TEXT NOT NULL,
        tag        TEXT NOT NULL,
        PRIMARY KEY (lesson_id, tag),
        FOREIGN KEY (lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS repo_notes (
        repo       TEXT NOT NULL PRIMARY KEY,
        body       TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS firing_logs (
        firing_id    TEXT    NOT NULL PRIMARY KEY,
        codename     TEXT    NOT NULL,
        repo         TEXT,
        status       TEXT    NOT NULL,
        summary      TEXT    NOT NULL DEFAULT '',
        started_at   TEXT    NOT NULL,
        finished_at  TEXT    NOT NULL,
        cost_cents   INTEGER NOT NULL DEFAULT 0,
        pr_url       TEXT,
        sentinel     TEXT,
        CHECK (status IN ('ok', 'blocked', 'partial', 'silent'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_touches (
        id          TEXT    NOT NULL PRIMARY KEY,
        repo        TEXT    NOT NULL,
        path        TEXT    NOT NULL,
        codename    TEXT    NOT NULL,
        firing_id   TEXT,
        pr_url      TEXT,
        change_type TEXT    NOT NULL DEFAULT 'modified',
        touched_at  TEXT    NOT NULL,
        CHECK (change_type IN ('added', 'modified', 'deleted', 'renamed', 'unknown'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_candidates (
        id                 TEXT    NOT NULL PRIMARY KEY,
        codename           TEXT    NOT NULL,
        repo               TEXT    NOT NULL,
        body               TEXT    NOT NULL,
        tags_json          TEXT    NOT NULL DEFAULT '[]',
        severity           TEXT    NOT NULL DEFAULT 'info',
        source             TEXT    NOT NULL DEFAULT 'manual',
        source_firing_id   TEXT,
        evidence           TEXT    NOT NULL DEFAULT '',
        confidence         REAL    NOT NULL DEFAULT 0.5,
        status             TEXT    NOT NULL DEFAULT 'candidate',
        created_at         TEXT    NOT NULL,
        reviewed_at        TEXT,
        reviewed_by        TEXT,
        review_note        TEXT,
        promoted_lesson_id TEXT,
        CHECK (severity IN ('info', 'warning', 'blocker')),
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
        CHECK (status IN ('candidate', 'validated', 'rejected', 'retired'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS failure_events (
        id         TEXT    NOT NULL PRIMARY KEY,
        codename   TEXT    NOT NULL,
        repo       TEXT,
        firing_id  TEXT,
        subtype    TEXT    NOT NULL,
        summary    TEXT    NOT NULL DEFAULT '',
        engine     TEXT,
        severity   TEXT    NOT NULL DEFAULT 'warning',
        created_at TEXT    NOT NULL,
        CHECK (severity IN ('info', 'warning', 'blocker'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS github_items (
        id           TEXT    NOT NULL PRIMARY KEY,
        repo         TEXT    NOT NULL,
        number       INTEGER NOT NULL,
        kind         TEXT    NOT NULL,
        state        TEXT    NOT NULL,
        title        TEXT    NOT NULL DEFAULT '',
        url          TEXT    NOT NULL DEFAULT '',
        labels_json  TEXT    NOT NULL DEFAULT '[]',
        created_at   TEXT,
        updated_at   TEXT    NOT NULL,
        last_seen_at TEXT    NOT NULL,
        closed_at    TEXT,
        merged_at    TEXT,
        head_ref     TEXT,
        base_ref     TEXT,
        bundle_slug  TEXT,
        changed_files INTEGER NOT NULL DEFAULT 0,
        file_metrics_seen_at TEXT,
        additions    INTEGER NOT NULL DEFAULT 0,
        deletions    INTEGER NOT NULL DEFAULT 0,
        line_metrics_seen_at TEXT,
        CHECK (kind IN ('issue', 'pr')),
        CHECK (state IN ('open', 'closed', 'merged', 'unknown'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bundle_items (
        id           TEXT    NOT NULL PRIMARY KEY,
        bundle_slug  TEXT    NOT NULL,
        repo         TEXT    NOT NULL,
        item_kind    TEXT    NOT NULL,
        number       INTEGER NOT NULL,
        state        TEXT    NOT NULL,
        title        TEXT    NOT NULL DEFAULT '',
        url          TEXT    NOT NULL DEFAULT '',
        labels_json  TEXT    NOT NULL DEFAULT '[]',
        updated_at   TEXT    NOT NULL,
        last_seen_at TEXT    NOT NULL,
        CHECK (item_kind IN ('issue', 'pr')),
        CHECK (state IN ('open', 'closed', 'merged', 'unknown'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_heartbeats (
        id           TEXT    NOT NULL PRIMARY KEY,
        codename     TEXT    NOT NULL,
        firing_id    TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'running',
        started_at   TEXT    NOT NULL,
        heartbeat_at TEXT    NOT NULL,
        repo         TEXT,
        pid          INTEGER,
        detail       TEXT    NOT NULL DEFAULT '',
        CHECK (status IN ('running', 'ok', 'failed', 'stale', 'cancelled'))
    )
    """,
    # Indexes — recall is read-heavy on (codename, repo) and recent-first,
    # so we cover that path explicitly.
    """
    CREATE INDEX IF NOT EXISTS lessons_codename_repo_created_idx
        ON lessons (codename, repo, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS lessons_repo_created_idx
        ON lessons (repo, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS lesson_tags_tag_idx
        ON lesson_tags (tag)
    """,
    """
    CREATE INDEX IF NOT EXISTS firing_logs_codename_started_idx
        ON firing_logs (codename, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS firing_logs_status_idx
        ON firing_logs (status, started_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_touches_repo_path_touched_idx
        ON file_touches (repo, path, touched_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_touches_codename_touched_idx
        ON file_touches (codename, touched_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS file_touches_firing_idx
        ON file_touches (firing_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS memory_candidates_status_created_idx
        ON memory_candidates (status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS memory_candidates_repo_status_idx
        ON memory_candidates (repo, status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS memory_candidates_codename_status_idx
        ON memory_candidates (codename, status, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS failure_events_codename_created_idx
        ON failure_events (codename, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS failure_events_repo_created_idx
        ON failure_events (repo, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS failure_events_subtype_created_idx
        ON failure_events (subtype, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS failure_events_firing_idx
        ON failure_events (firing_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS github_items_repo_kind_updated_idx
        ON github_items (repo, kind, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS github_items_state_updated_idx
        ON github_items (state, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS github_items_bundle_idx
        ON github_items (bundle_slug, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS bundle_items_slug_updated_idx
        ON bundle_items (bundle_slug, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS bundle_items_repo_state_idx
        ON bundle_items (repo, state, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS worker_heartbeats_status_idx
        ON worker_heartbeats (status, heartbeat_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS worker_heartbeats_codename_idx
        ON worker_heartbeats (codename, heartbeat_at DESC)
    """,
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply every ``CREATE ... IF NOT EXISTS`` statement.

    Safe to call on every connection, every run. Wraps the whole set
    in a single transaction so partial application can't leave the DB
    in an inconsistent state.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    with conn:
        for stmt in _CREATE_STATEMENTS:
            conn.execute(stmt)
        _add_column_if_missing(conn, "github_items", "additions", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "github_items", "deletions", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "github_items", "line_metrics_seen_at", "TEXT")
        _add_column_if_missing(conn, "github_items", "created_at", "TEXT")
        _add_column_if_missing(conn, "github_items", "changed_files", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "github_items", "file_metrics_seen_at", "TEXT")
        # Record the schema version, idempotently. A row with the
        # current version means "we have run ensure_schema at least
        # once at this code revision".
        conn.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) "
            "VALUES (?, datetime('now'))",
            (SCHEMA_VERSION,),
        )


def applied_version(conn: sqlite3.Connection) -> int | None:
    """Return the highest applied schema version, or ``None`` if the
    table is missing (i.e. the brain has never been initialized)."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return None
    return None if row is None or row[0] is None else int(row[0])


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Future migration helper. Reserved for v1.x additive changes.

    Inspects ``PRAGMA table_info`` and runs ``ALTER TABLE ... ADD
    COLUMN`` only when the column is absent. A concurrent Alfred process
    may add the same column between the PRAGMA read and ALTER; SQLite
    reports that as ``duplicate column name``, which is safe to ignore.
    Kept private because callers should prefer new tables over mutating
    old rows unless an additive migration is unavoidable.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "duplicate column name" in message and column.lower() in message:
            return
        raise
