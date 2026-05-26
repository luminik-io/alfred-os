"""Unit tests for the fleet-brain memory layer.

Covers the public API in :mod:`fleet_brain`:

* ``ensure_schema`` is idempotent (running it twice does not raise).
* ``reflect`` + ``recall`` round-trip preserves body, tags, severity,
  firing_id.
* ``recall`` returns most-recent-first.
* ``firing_log`` insert + ``list_firings`` query.
* ``RepoNote`` upsert.
* ``export`` JSON snapshot.
* ``forget`` by id, ``forget_before`` GC.
* CLI smoke (status, lessons, reflect) through ``alfred-brain.py``.

All tests use the ``tmp_path`` fixture so nothing touches the
operator's real ``$ALFRED_HOME``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

# Make ``lib/`` importable from the repo root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "lib"))

from fleet_brain import FileTouch, FleetBrain, Lesson, SQLiteStore  # noqa: E402
from fleet_brain.schema import (  # noqa: E402
    SCHEMA_VERSION,
    applied_version,
    ensure_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "brain.db"


@pytest.fixture()
def brain(db_path: Path) -> FleetBrain:
    return FleetBrain(db_path=db_path)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_ensure_schema_is_idempotent(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        ensure_schema(conn)
        ensure_schema(conn)
        assert applied_version(conn) == SCHEMA_VERSION
        # And every expected table is present.
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {
            "lessons",
            "lesson_tags",
            "repo_notes",
            "firing_logs",
            "file_touches",
            "memory_candidates",
            "failure_events",
            "schema_version",
        }.issubset(names)
    finally:
        conn.close()


def test_fleetbrain_creates_db_file(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "brain.db"
    FleetBrain(db_path=db)
    assert db.exists()


def test_memory_doctor_warns_for_v2_database_missing_additive_tables(tmp_path: Path) -> None:
    from fleet_brain.doctor import run_memory_doctor

    db = tmp_path / "v2.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT)")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (2, 'now')")
        for table in ("lessons", "lesson_tags", "repo_notes", "firing_logs", "file_touches"):
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        conn.commit()
    finally:
        conn.close()

    report = run_memory_doctor(db)
    assert report["status"] == "warn"
    table_check = next(c for c in report["checks"] if c["name"] == "tables")
    assert table_check["status"] == "warn"
    assert "additive schema tables missing" in table_check["detail"]


# ---------------------------------------------------------------------------
# Reflect + recall
# ---------------------------------------------------------------------------


def test_reflect_recall_round_trip(brain: FleetBrain) -> None:
    lesson = brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="GraphQL schema lives in src/schema.graphql",
        tags=["graphql", "layout"],
        firing_id="firing-1",
        severity="warning",
    )
    assert lesson.id  # ULID-shaped, 26 chars
    assert len(lesson.id) == 26

    out = brain.recall(codename="lucius", repo="your-org/api")
    assert len(out) == 1
    L = out[0]
    assert L.id == lesson.id
    assert L.body == "GraphQL schema lives in src/schema.graphql"
    assert L.tags == ["graphql", "layout"]
    assert L.severity == "warning"
    assert L.firing_id == "firing-1"
    assert L.created_at.tzinfo is not None


def test_recall_returns_most_recent_first(brain: FleetBrain) -> None:
    now = datetime.now(UTC)
    oldest = brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="oldest",
        created_at=now - timedelta(hours=2),
    )
    middle = brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="middle",
        created_at=now - timedelta(hours=1),
    )
    newest = brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="newest",
        created_at=now,
    )
    out = brain.recall(codename="lucius", repo="your-org/api")
    assert [L.id for L in out] == [newest.id, middle.id, oldest.id]


def test_recall_widens_with_none(brain: FleetBrain) -> None:
    brain.reflect(codename="lucius", repo="org/api", body="alpha")
    brain.reflect(codename="drake", repo="org/api", body="beta")
    brain.reflect(codename="lucius", repo="org/web", body="gamma")

    all_for_repo = brain.recall(repo="org/api")
    bodies = {L.body for L in all_for_repo}
    assert bodies == {"alpha", "beta"}

    all_for_codename = brain.recall(codename="lucius")
    bodies = {L.body for L in all_for_codename}
    assert bodies == {"alpha", "gamma"}


def test_recall_query_substring(brain: FleetBrain) -> None:
    brain.reflect(codename="lucius", repo="org/api", body="GraphQL auth header")
    brain.reflect(codename="lucius", repo="org/api", body="REST pagination quirk")
    out = brain.recall(codename="lucius", repo="org/api", query="GraphQL")
    assert len(out) == 1
    assert "GraphQL" in out[0].body


def test_recall_limit_is_clamped(brain: FleetBrain) -> None:
    for i in range(5):
        brain.reflect(codename="lucius", repo="org/api", body=f"body-{i}")
    assert len(brain.recall(codename="lucius", repo="org/api", limit=2)) == 2
    # Negative and zero get clamped to >=1.
    assert len(brain.recall(codename="lucius", repo="org/api", limit=0)) >= 1


def test_reflect_validates_inputs(brain: FleetBrain) -> None:
    with pytest.raises(ValueError):
        brain.reflect(codename="", repo="org/api", body="x")
    with pytest.raises(ValueError):
        brain.reflect(codename="lucius", repo="", body="x")
    with pytest.raises(ValueError):
        brain.reflect(codename="lucius", repo="org/api", body="")
    with pytest.raises(ValueError):
        brain.reflect(
            codename="lucius",
            repo="org/api",
            body="x",
            severity="critical",  # type: ignore[arg-type]
        )


def test_reflect_deduplicates_tags(brain: FleetBrain) -> None:
    L = brain.reflect(
        codename="lucius",
        repo="org/api",
        body="x",
        tags=["graphql", "graphql", " graphql ", "auth"],
    )
    assert L.tags == ["auth", "graphql"]


# ---------------------------------------------------------------------------
# Firing log
# ---------------------------------------------------------------------------


def test_firing_log_insert_and_query(brain: FleetBrain) -> None:
    started = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    finished = started + timedelta(minutes=3)
    F = brain.firing_log(
        firing_id="01HZA",
        codename="lucius",
        status="ok",
        summary="opened PR #42",
        repo="org/api",
        started_at=started,
        finished_at=finished,
        cost_cents=17,
        pr_url="https://github.com/org/api/pull/42",
    )
    assert F.firing_id == "01HZA"

    out = brain.list_firings(codename="lucius")
    assert len(out) == 1
    assert out[0].pr_url.endswith("/42")
    assert out[0].cost_cents == 17

    # Status filter.
    assert brain.list_firings(status="ok")
    assert brain.list_firings(status="blocked") == []


def test_firing_log_upserts_on_firing_id(brain: FleetBrain) -> None:
    brain.firing_log(firing_id="fid", codename="lucius", status="partial", summary="started")
    brain.firing_log(firing_id="fid", codename="lucius", status="ok", summary="done")
    out = brain.list_firings()
    assert len(out) == 1
    assert out[0].status == "ok"
    assert out[0].summary == "done"


# ---------------------------------------------------------------------------
# File touches
# ---------------------------------------------------------------------------


def test_file_touch_record_and_query(brain: FleetBrain) -> None:
    touched = brain.record_file_touch(
        repo="org/api",
        path="src/api.py",
        codename="lucius",
        firing_id="fid",
        pr_url="https://github.com/org/api/pull/42",
        change_type="modified",
    )
    assert isinstance(touched, FileTouch)
    assert len(touched.id) == 26

    out = brain.list_file_touches(repo="org/api", codename="lucius")
    assert len(out) == 1
    assert out[0].path == "src/api.py"
    assert out[0].firing_id == "fid"
    assert out[0].pr_url.endswith("/42")

    assert brain.list_file_touches(repo="org/api", path="src/api.py")[0].id == touched.id
    assert brain.list_file_touches(repo="org/api", path="missing.py") == []


def test_file_touch_validates_inputs(brain: FleetBrain) -> None:
    with pytest.raises(ValueError):
        brain.record_file_touch(repo="", path="src/api.py", codename="lucius")
    with pytest.raises(ValueError):
        brain.record_file_touch(repo="org/api", path="", codename="lucius")
    with pytest.raises(ValueError):
        brain.record_file_touch(
            repo="org/api",
            path="src/api.py",
            codename="lucius",
            change_type="bad",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Memory candidates
# ---------------------------------------------------------------------------


def test_memory_candidate_promote_and_reject(brain: FleetBrain) -> None:
    candidate = brain.propose_memory(
        codename="lucius",
        repo="org/api",
        body="Use the API fixture factory.",
        tags=["tests", "tests"],
        severity="warning",
        source="engine-reflection",
        source_firing_id="fid",
        confidence=0.7,
    )
    assert len(candidate.id) == 26
    assert candidate.tags == ["tests"]
    assert candidate.status == "candidate"

    assert brain.recall(codename="lucius", repo="org/api") == []
    lesson = brain.promote_memory_candidate(candidate.id, reviewer="alice", review_note="true")
    assert lesson.body == "Use the API fixture factory."
    assert lesson.severity == "warning"

    promoted = brain.list_memory_candidates(status="validated")[0]
    assert promoted.promoted_lesson_id == lesson.id
    assert promoted.reviewed_by == "alice"

    rejected = brain.propose_memory(codename="lucius", repo="org/api", body="speculative")
    out = brain.reject_memory_candidate(rejected.id, reviewer="bob", review_note="too vague")
    assert out.status == "rejected"
    assert out.review_note == "too vague"


def test_memory_candidate_validates_inputs(brain: FleetBrain) -> None:
    with pytest.raises(ValueError):
        brain.propose_memory(codename="", repo="org/api", body="x")
    with pytest.raises(ValueError):
        brain.propose_memory(codename="lucius", repo="org/api", body="x", confidence=2)
    with pytest.raises(ValueError):
        brain.promote_memory_candidate("missing")


# ---------------------------------------------------------------------------
# Failure events
# ---------------------------------------------------------------------------


def test_failure_event_record_and_query(brain: FleetBrain) -> None:
    event = brain.record_failure(
        codename="huntress",
        repo="org/web",
        firing_id="fid",
        subtype="error_timeout",
        summary="browser install missing",
        engine="claude",
    )
    assert len(event.id) == 26
    out = brain.list_failures(repo="org/web", codename="huntress")
    assert len(out) == 1
    assert out[0].subtype == "error_timeout"
    assert out[0].engine == "claude"

    assert brain.list_failures(subtype="different") == []


# ---------------------------------------------------------------------------
# Repo note
# ---------------------------------------------------------------------------


def test_repo_note_upsert(brain: FleetBrain) -> None:
    brain.note_repo(repo="org/api", body="first")
    brain.note_repo(repo="org/api", body="second")
    note = brain.get_repo_note("org/api")
    assert note is not None
    assert note.body == "second"
    assert brain.get_repo_note("org/missing") is None


# ---------------------------------------------------------------------------
# Export / forget
# ---------------------------------------------------------------------------


def test_export_round_trip_shape(brain: FleetBrain) -> None:
    brain.reflect(codename="lucius", repo="org/api", body="alpha", tags=["x"])
    brain.note_repo(repo="org/api", body="rollup")
    brain.firing_log(firing_id="fid", codename="lucius", status="ok", summary="done")
    brain.record_file_touch(repo="org/api", path="src/api.py", codename="lucius")
    brain.propose_memory(codename="lucius", repo="org/api", body="candidate")
    brain.record_failure(codename="lucius", subtype="error_timeout", summary="timeout")
    payload = brain.export()
    # Must be JSON-roundtrippable.
    s = json.dumps(payload, default=str)
    data = json.loads(s)
    assert data["schema_version"] == SCHEMA_VERSION
    assert len(data["lessons"]) == 1
    assert data["lessons"][0]["body"] == "alpha"
    assert len(data["repo_notes"]) == 1
    assert data["repo_notes"][0]["body"] == "rollup"
    assert len(data["firings"]) == 1
    assert len(data["file_touches"]) == 1
    assert data["file_touches"][0]["path"] == "src/api.py"
    assert len(data["memory_candidates"]) == 1
    assert len(data["failure_events"]) == 1


def test_forget_by_id(brain: FleetBrain) -> None:
    L = brain.reflect(codename="lucius", repo="org/api", body="alpha")
    assert brain.forget(L.id) is True
    assert brain.recall(codename="lucius", repo="org/api") == []
    # Idempotent: second call returns False (already gone).
    assert brain.forget(L.id) is False


def test_forget_cascades_tags(brain: FleetBrain, db_path: Path) -> None:
    L = brain.reflect(codename="lucius", repo="org/api", body="alpha", tags=["graphql"])
    brain.forget(L.id)
    conn = sqlite3.connect(db_path)
    try:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM lesson_tags WHERE lesson_id = ?", (L.id,)
        ).fetchone()
    finally:
        conn.close()
    assert count == 0


def test_forget_before(brain: FleetBrain) -> None:
    now = datetime.now(UTC)
    brain.reflect(
        codename="lucius",
        repo="org/api",
        body="ancient",
        created_at=now - timedelta(days=60),
    )
    brain.reflect(
        codename="lucius",
        repo="org/api",
        body="recent",
        created_at=now,
    )
    deleted = brain.forget_before(days=30)
    assert deleted == 1
    remaining = brain.recall(codename="lucius", repo="org/api")
    assert [L.body for L in remaining] == ["recent"]


def test_forget_before_validates_args(brain: FleetBrain) -> None:
    with pytest.raises(ValueError):
        brain.forget_before()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        brain.forget_before(days=1, before=datetime.now(UTC))


# ---------------------------------------------------------------------------
# Store injection
# ---------------------------------------------------------------------------


class FakeStore:
    """Minimal in-memory store to prove dependency inversion."""

    def __init__(self) -> None:
        self.lessons: list[Lesson] = []
        self.schema_calls = 0

    def ensure_schema(self) -> None:
        self.schema_calls += 1

    def insert_lesson(self, lesson):  # type: ignore[no-untyped-def]
        self.lessons.append(lesson)
        return lesson

    def recall_lessons(self, codename, repo, query=None, limit=20):  # type: ignore[no-untyped-def]
        out = [
            L
            for L in self.lessons
            if (codename is None or L.codename == codename) and (repo is None or L.repo == repo)
        ]
        return list(reversed(out))[:limit]

    def list_lessons(self, limit=None):  # type: ignore[no-untyped-def]
        return list(reversed(self.lessons))[: (limit or len(self.lessons))]

    def get_lesson(self, lesson_id):  # type: ignore[no-untyped-def]
        return next((L for L in self.lessons if L.id == lesson_id), None)

    def delete_lesson(self, lesson_id):  # type: ignore[no-untyped-def]
        before = len(self.lessons)
        self.lessons = [L for L in self.lessons if L.id != lesson_id]
        return len(self.lessons) != before

    def delete_lessons_before(self, cutoff):  # type: ignore[no-untyped-def]
        before = len(self.lessons)
        self.lessons = [L for L in self.lessons if L.created_at >= cutoff]
        return before - len(self.lessons)

    def upsert_repo_note(self, note):  # type: ignore[no-untyped-def]
        return note

    def get_repo_note(self, repo):  # type: ignore[no-untyped-def]
        return None

    def insert_firing_log(self, log):  # type: ignore[no-untyped-def]
        return log

    def list_firing_logs(self, codename=None, status=None, limit=50):  # type: ignore[no-untyped-def]
        return []

    def insert_file_touch(self, touch):  # type: ignore[no-untyped-def]
        return touch

    def list_file_touches(self, repo=None, codename=None, path=None, limit=50):  # type: ignore[no-untyped-def]
        return []

    def insert_memory_candidate(self, candidate):  # type: ignore[no-untyped-def]
        return candidate

    def get_memory_candidate(self, candidate_id):  # type: ignore[no-untyped-def]
        return None

    def update_memory_candidate(self, candidate):  # type: ignore[no-untyped-def]
        return candidate

    def list_memory_candidates(self, status=None, repo=None, codename=None, limit=50):  # type: ignore[no-untyped-def]
        return []

    def insert_failure_event(self, event):  # type: ignore[no-untyped-def]
        return event

    def list_failure_events(self, repo=None, codename=None, subtype=None, limit=50):  # type: ignore[no-untyped-def]
        return []

    def stats(self):  # type: ignore[no-untyped-def]
        return {
            "lessons": len(self.lessons),
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


def test_store_protocol_injection() -> None:
    fake = FakeStore()
    brain = FleetBrain(store=fake)
    assert fake.schema_calls == 1
    brain.reflect(codename="lucius", repo="org/api", body="x")
    assert len(fake.lessons) == 1


# ---------------------------------------------------------------------------
# Default db path resolution
# ---------------------------------------------------------------------------


def test_default_db_path_respects_alfred_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.delenv("ALFRED_FLEET_BRAIN_DB", raising=False)
    from fleet_brain.store import default_db_path

    assert default_db_path() == tmp_path / "alfred" / "fleet-brain.db"


def test_default_db_path_respects_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "explicit.db"
    monkeypatch.setenv("ALFRED_FLEET_BRAIN_DB", str(target))
    from fleet_brain.store import default_db_path

    assert default_db_path() == target


# ---------------------------------------------------------------------------
# ULID-ish identifier sortability
# ---------------------------------------------------------------------------


def test_new_id_is_time_sortable() -> None:
    from fleet_brain.store import new_id

    a = new_id()
    time.sleep(0.002)
    b = new_id()
    assert a < b
    assert len(a) == 26


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_reports_counts(brain: FleetBrain) -> None:
    brain.reflect(codename="lucius", repo="org/api", body="x", tags=["a"])
    brain.reflect(codename="drake", repo="org/web", body="y", tags=["b"])
    brain.firing_log(firing_id="fid", codename="lucius", status="ok")
    brain.note_repo(repo="org/api", body="rollup")
    brain.record_file_touch(repo="org/api", path="src/api.py", codename="lucius")
    brain.propose_memory(codename="lucius", repo="org/api", body="candidate")
    brain.record_failure(codename="lucius", subtype="error_timeout", summary="timeout")
    s = brain.stats()
    assert s["lessons"] == 2
    assert s["firings"] == 1
    assert s["file_touches"] == 1
    assert s["memory_candidates"] == 1
    assert s["memory_candidates_open"] == 1
    assert s["failure_events"] == 1
    assert s["repo_notes"] == 1
    assert s["tags"] == 2
    assert s["codenames"] == 2
    assert s["repos"] == 2


# ---------------------------------------------------------------------------
# SQLiteStore direct
# ---------------------------------------------------------------------------


def test_sqlite_store_in_memory_survives_calls() -> None:
    """The :memory: convenience path should preserve data across calls."""
    store = SQLiteStore(db_path=Path(":memory:"))
    store.ensure_schema()
    from fleet_brain.store import Lesson, new_id

    store.insert_lesson(
        Lesson(
            id=new_id(),
            codename="lucius",
            repo="org/api",
            body="alpha",
            tags=[],
            created_at=datetime.now(UTC),
            firing_id=None,
        )
    )
    out = store.recall_lessons(codename="lucius", repo="org/api")
    assert len(out) == 1


def test_disk_store_enables_wal_journal_mode(tmp_path: Path) -> None:
    """The fleet_brain SQLite store must enable WAL on disk-backed dbs so
    two short-lived writers from sibling firings (lucius + drake calling
    ``reflect()`` within the same second) don't serialise on a writer
    lock. Default journal mode is ``delete`` which exclusively locks the
    database for the writer; WAL lets readers proceed concurrently and
    halves writer contention."""
    store = SQLiteStore(db_path=tmp_path / "brain.sqlite")
    store.ensure_schema()
    # Reading the PRAGMA reopens through ``_connect`` which applies the
    # journal mode we want to assert.
    conn = sqlite3.connect(tmp_path / "brain.sqlite")
    try:
        # ``journal_mode`` is sticky on disk for WAL — once set it persists
        # across opens. Verify the on-disk mode is wal (not delete/memory).
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal", f"expected wal, got {mode!r}"
    finally:
        conn.close()
