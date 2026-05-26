"""Read-only fleet-brain health checks."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, applied_version
from .store import default_db_path

_REQUIRED_TABLES = {
    "lessons",
    "lesson_tags",
    "repo_notes",
    "firing_logs",
    "file_touches",
    "memory_candidates",
    "failure_events",
    "schema_version",
}


def run_memory_doctor(db_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(db_path).expanduser() if db_path else default_db_path()
    checks: list[dict[str, str]] = []
    stats = {
        "lessons": 0,
        "firings": 0,
        "file_touches": 0,
        "memory_candidates": 0,
        "memory_candidates_open": 0,
        "failure_events": 0,
        "repo_notes": 0,
        "tags": 0,
        "codenames": 0,
        "repos": 0,
    }

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    if not path.exists():
        check("database", "warn", f"brain database does not exist: {path}")
        return _report("warn", path, stats, checks)

    try:
        conn = _connect_read_only(path)
    except sqlite3.Error as exc:
        check("database", "fail", f"cannot open brain database read-only: {exc}")
        return _report("fail", path, stats, checks)

    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity and integrity[0] == "ok":
            check("integrity", "ok", "sqlite integrity_check passed")
        else:
            check("integrity", "fail", f"sqlite integrity_check returned {integrity!r}")

        version = applied_version(conn)
        if version == SCHEMA_VERSION:
            check("schema", "ok", f"schema v{version}")
        else:
            check("schema", "warn", f"schema v{version}, expected v{SCHEMA_VERSION}")

        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = sorted(_REQUIRED_TABLES - tables)
        if missing:
            missing_set = set(missing)
            additive_v3 = {"memory_candidates", "failure_events"}
            if version is not None and version < SCHEMA_VERSION and missing_set <= additive_v3:
                check(
                    "tables",
                    "warn",
                    "additive schema tables missing; initialize the brain once to apply v3",
                )
            else:
                check("tables", "fail", f"missing tables: {', '.join(missing)}")
        else:
            check("tables", "ok", "required tables present")

        if not missing:
            stats = _stats(conn)
            open_candidates = stats["memory_candidates_open"]
            if open_candidates > 100:
                check("candidate_backlog", "fail", f"{open_candidates} candidates need review")
            elif open_candidates > 20:
                check("candidate_backlog", "warn", f"{open_candidates} candidates need review")
            else:
                check("candidate_backlog", "ok", f"{open_candidates} open candidates")

            failures = stats["failure_events"]
            if failures > 100:
                check("failure_events", "warn", f"{failures} failure events recorded")
            else:
                check("failure_events", "ok", f"{failures} failure events recorded")
    finally:
        conn.close()

    status = "ok"
    if any(c["status"] == "fail" for c in checks):
        status = "fail"
    elif any(c["status"] == "warn" for c in checks):
        status = "warn"
    return _report(status, path, stats, checks)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "lessons": _count(conn, "lessons"),
        "firings": _count(conn, "firing_logs"),
        "file_touches": _count(conn, "file_touches"),
        "memory_candidates": _count(conn, "memory_candidates"),
        "memory_candidates_open": _count_where(conn, "memory_candidates", "status = 'candidate'"),
        "failure_events": _count(conn, "failure_events"),
        "repo_notes": _count(conn, "repo_notes"),
        "tags": _count_distinct(conn, "lesson_tags", "tag"),
        "codenames": _count_distinct(conn, "lessons", "codename"),
        "repos": _count_distinct(conn, "lessons", "repo"),
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _count_where(conn: sqlite3.Connection, table: str, where: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _count_distinct(conn: sqlite3.Connection, table: str, column: str) -> int:
    return int(conn.execute(f"SELECT COUNT(DISTINCT {column}) FROM {table}").fetchone()[0])


def _report(
    status: str,
    path: Path,
    stats: dict[str, int],
    checks: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "status": status,
        "db": str(path),
        "stats": stats,
        "checks": checks,
    }
