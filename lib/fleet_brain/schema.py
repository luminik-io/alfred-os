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
* ``lesson_tags`` — many-to-many over ``lessons`` so a lesson can
  be filed under several taxonomy buckets without splitting rows.
* ``schema_version`` — single-row record of the applied schema
  version, for forward migrations.
"""

from __future__ import annotations

import sqlite3
from typing import Final

SCHEMA_VERSION: Final[int] = 2

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
    COLUMN`` only when the column is absent. Kept private because
    callers should add new tables instead of mutating old ones until
    the v2 PGLite/AGE upgrade lands a real migration framework.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
