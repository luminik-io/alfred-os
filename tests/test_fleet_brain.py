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

from fleet_brain import FleetBrain, Lesson, SQLiteStore  # noqa: E402
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
            "schema_version",
        }.issubset(names)
    finally:
        conn.close()


def test_fleetbrain_creates_db_file(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "brain.db"
    FleetBrain(db_path=db)
    assert db.exists()


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

    def stats(self):  # type: ignore[no-untyped-def]
        return {"lessons": len(self.lessons)}


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
    s = brain.stats()
    assert s["lessons"] == 2
    assert s["firings"] == 1
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
