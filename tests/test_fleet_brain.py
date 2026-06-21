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
    _add_column_if_missing,
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
            "github_items",
            "bundle_items",
            "worker_heartbeats",
            "schema_version",
        }.issubset(names)
    finally:
        conn.close()


def test_fleetbrain_creates_db_file(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "brain.db"
    FleetBrain(db_path=db)
    assert db.exists()


def test_ensure_schema_adds_github_line_columns_to_existing_table(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE github_items (
                id TEXT NOT NULL PRIMARY KEY,
                repo TEXT NOT NULL,
                number INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                labels_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                closed_at TEXT,
                merged_at TEXT,
                head_ref TEXT,
                base_ref TEXT,
                bundle_slug TEXT
            )
            """
        )
        conn.commit()
        ensure_schema(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(github_items)")}
        assert {
            "additions",
            "deletions",
            "line_metrics_seen_at",
            "changed_files",
            "file_metrics_seen_at",
        }.issubset(cols)
    finally:
        conn.close()


def test_add_column_if_missing_tolerates_concurrent_duplicate_column() -> None:
    class RacingConnection:
        def __init__(self) -> None:
            self.alter_attempts = 0

        def execute(self, sql: str):
            if sql.startswith("PRAGMA table_info"):
                return [(0, "id")]
            if sql.startswith("ALTER TABLE"):
                self.alter_attempts += 1
                raise sqlite3.OperationalError("duplicate column name: additions")
            raise AssertionError(sql)

    conn = RacingConnection()

    _add_column_if_missing(conn, "github_items", "additions", "INTEGER NOT NULL DEFAULT 0")

    assert conn.alter_attempts == 1


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
# GitHub poller cache, bundles, worker heartbeats
# ---------------------------------------------------------------------------


def test_github_item_upsert_populates_bundle(brain: FleetBrain) -> None:
    item = brain.upsert_github_item(
        repo="org/api",
        number=42,
        kind="pr",
        state="open",
        title="feat: add endpoint",
        url="https://github.com/org/api/pull/42",
        labels=["agent:bundle:billing", "agent:authored"],
        head_ref="lucius/42",
        base_ref="main",
        changed_files=4,
        additions=12,
        deletions=3,
    )
    assert item.id == "org/api#42:pr"
    assert item.bundle_slug == "billing"

    items = brain.list_github_items(repo="org/api", kind="pr", state="open")
    assert len(items) == 1
    assert items[0].labels == ["agent:authored", "agent:bundle:billing"]
    assert items[0].changed_files == 4
    assert items[0].file_metrics_seen_at is not None
    assert items[0].additions == 12
    assert items[0].deletions == 3
    assert items[0].line_metrics_seen_at is not None

    bundle_items = brain.list_bundle_items(bundle_slug="billing")
    assert len(bundle_items) == 1
    assert bundle_items[0].repo == "org/api"
    assert bundle_items[0].item_kind == "pr"


def test_github_item_preserves_line_totals_when_updates_omit_them(
    brain: FleetBrain,
) -> None:
    brain.upsert_github_item(
        repo="org/api",
        number=43,
        kind="pr",
        state="open",
        changed_files=4,
        additions=12,
        deletions=3,
    )

    brain.upsert_github_item(
        repo="org/api",
        number=43,
        kind="pr",
        state="merged",
    )

    items = brain.list_github_items(repo="org/api", kind="pr", state="merged")
    assert len(items) == 1
    assert items[0].changed_files == 4
    assert items[0].additions == 12
    assert items[0].deletions == 3
    file_marker = items[0].file_metrics_seen_at
    marker = items[0].line_metrics_seen_at
    assert file_marker is not None
    assert marker is not None

    brain.upsert_github_item(
        repo="org/api",
        number=43,
        kind="pr",
        state="merged",
        changed_files=0,
        additions=0,
        deletions=0,
    )

    items = brain.list_github_items(repo="org/api", kind="pr", state="merged")
    assert items[0].changed_files == 0
    assert items[0].file_metrics_seen_at is not None
    assert items[0].file_metrics_seen_at >= file_marker
    assert items[0].additions == 0
    assert items[0].deletions == 0
    assert items[0].line_metrics_seen_at is not None
    assert items[0].line_metrics_seen_at >= marker


def test_sum_github_changed_lines_uses_authored_filter(brain: FleetBrain) -> None:
    brain.upsert_github_item(
        repo="org/api",
        number=1,
        kind="pr",
        state="merged",
        labels=["agent:authored"],
        changed_files=4,
        additions=10,
        deletions=2,
    )
    brain.upsert_github_item(
        repo="org/api",
        number=2,
        kind="pr",
        state="open",
        head_ref="lucius/2",
        changed_files=2,
        additions=5,
        deletions=1,
    )
    brain.upsert_github_item(
        repo="org/api",
        number=3,
        kind="pr",
        state="merged",
        head_ref="feature/human",
        changed_files=200,
        additions=1000,
        deletions=1000,
    )
    assert brain.sum_github_changed_lines(kind="pr") == 2018
    assert brain.sum_github_changed_lines(kind="pr", authored_only=True) == 18
    assert brain.sum_github_changed_lines(kind="pr", state="merged", authored_only=True) == 12
    assert brain.sum_github_changed_files(kind="pr") == 206
    assert brain.sum_github_changed_files(kind="pr", authored_only=True) == 6
    assert brain.sum_github_changed_files(kind="pr", state="merged", authored_only=True) == 4


def test_sum_github_changed_lines_skips_historical_unknown_line_metrics(
    brain: FleetBrain,
) -> None:
    brain.upsert_github_item(
        repo="org/api",
        number=4,
        kind="pr",
        state="open",
        labels=["agent:authored"],
    )
    brain.upsert_github_item(
        repo="org/api",
        number=5,
        kind="pr",
        state="open",
        labels=["agent:authored"],
        changed_files=6,
        additions=20,
        deletions=3,
    )

    assert brain.sum_github_changed_lines(kind="pr", authored_only=True) == 23
    assert brain.sum_github_changed_files(kind="pr", authored_only=True) == 6

    brain.upsert_github_item(
        repo="org/api",
        number=4,
        kind="pr",
        state="open",
        labels=["agent:authored"],
        changed_files=0,
        additions=0,
        deletions=0,
    )

    assert brain.sum_github_changed_lines(kind="pr", authored_only=True) == 23
    assert brain.sum_github_changed_files(kind="pr", authored_only=True) == 6


def test_count_github_items_counts_past_the_500_list_cap(brain: FleetBrain) -> None:
    # list_github_items clamps limit to 500, so len(list(...)) freezes at 500 on
    # a busy brain. count_github_items does an exact SQL COUNT(*) and must report
    # the true total. Regression guard for the proof-telemetry under-count
    # (finding #4): seed >500 PRs and assert the count exceeds the list cap.
    total = 612
    merged = 400
    for n in range(1, total + 1):
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged" if n <= merged else "open",
            title=f"pr {n}",
            url=f"https://github.com/org/api/pull/{n}",
        )
    # The list method tops out at its 500-row clamp ...
    assert len(brain.list_github_items(kind="pr", limit=10_000)) == 500
    # ... but the exact count sees them all.
    assert brain.count_github_items(kind="pr") == total
    assert brain.count_github_items(kind="pr", state="merged") == merged
    assert brain.count_github_items(kind="pr", state="open") == total - merged
    assert brain.count_github_items(kind="pr", state="closed") == 0


def test_count_github_items_authored_only(brain: FleetBrain) -> None:
    # The poller caches EVERY PR from `gh pr list`, including operator- and
    # bot-opened ones. count_github_items(authored_only=True) must count only
    # agent-authored rows: those carrying the agent:authored label OR pushed from
    # an agent branch prefix. Regression guard for Codex finding #2 (the proof
    # counter must not claim PRs Alfred did not open).
    # 3 authored by label, 2 authored by branch prefix, 4 operator PRs.
    for n in range(1, 4):
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged",
            labels=["agent:authored"],
            url=f"u/{n}",
        )
    for n in range(4, 6):
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged",
            head_ref="lucius/feature",
            url=f"u/{n}",
        )
    for n in range(6, 10):
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged",
            labels=["bug"],
            head_ref="feature/by-human",
            url=f"u/{n}",
        )
    assert brain.count_github_items(kind="pr") == 9, "all cached PRs"
    assert brain.count_github_items(kind="pr", authored_only=True) == 5, (
        "only the 3 label-authored + 2 branch-authored PRs"
    )
    assert brain.count_github_items(kind="pr", state="merged", authored_only=True) == 5, (
        "state and authorship filters compose"
    )
    assert brain.count_github_items(kind="pr", state="open", authored_only=True) == 0


def test_count_github_items_authored_branch_prefix_is_case_sensitive(
    brain: FleetBrain,
) -> None:
    # The branch-prefix authorship match must be case-SENSITIVE so an operator PR
    # on a differently-cased branch (e.g. "Lucius/fix" with a capital L) is NOT
    # miscounted as Alfred-authored. SQLite's default LIKE is case-insensitive for
    # ASCII (and COLLATE does not change that), so the SQL predicate uses GLOB to
    # match the case-sensitive Python fallback head_ref.startswith(prefix).
    brain.upsert_github_item(
        repo="org/api",
        number=1,
        kind="pr",
        state="merged",
        head_ref="lucius/fix",
        url="u/1",
    )
    brain.upsert_github_item(
        repo="org/api",
        number=2,
        kind="pr",
        state="merged",
        head_ref="Lucius/fix",
        url="u/2",
    )
    assert brain.count_github_items(kind="pr") == 2, "both PRs cached"
    assert brain.count_github_items(kind="pr", authored_only=True) == 1, (
        "lucius/fix IS authored; Lucius/fix (capital L) is NOT"
    )


def test_count_github_items_authored_only_past_500_cap(brain: FleetBrain) -> None:
    # The authored filter is a SQL predicate, so it stays an exact COUNT(*) past
    # the 500-row list cap: a busy install with thousands of authored PRs is
    # counted honestly, and interleaved operator PRs are excluded.
    authored = 520
    operator = 300
    n = 0
    for _ in range(authored):
        n += 1
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged",
            labels=["agent:authored"],
            url=f"u/{n}",
        )
    for _ in range(operator):
        n += 1
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="pr",
            state="merged",
            head_ref="feature/human",
            url=f"u/{n}",
        )
    assert brain.count_github_items(kind="pr") == authored + operator
    assert brain.count_github_items(kind="pr", authored_only=True) == authored, (
        "authored count exceeds the 500 list cap and excludes operator PRs"
    )


def test_count_github_items_agent_labeled_only_past_500_cap(brain: FleetBrain) -> None:
    agent_issues = 525
    unlabeled_issues = 125
    n = 0
    for _ in range(agent_issues):
        n += 1
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="issue",
            state="open",
            labels=["agent:implement"],
            url=f"u/{n}",
        )
    for _ in range(unlabeled_issues):
        n += 1
        brain.upsert_github_item(
            repo="org/api",
            number=n,
            kind="issue",
            state="open",
            labels=["bug"],
            url=f"u/{n}",
        )

    assert brain.count_github_items(kind="issue") == agent_issues + unlabeled_issues
    assert brain.count_github_items(kind="issue", agent_labeled_only=True) == agent_issues


def test_count_file_touches_counts_past_the_500_list_cap(brain: FleetBrain) -> None:
    total = 555
    for n in range(total):
        brain.record_file_touch(
            repo="org/api",
            path=f"src/file_{n}.py",
            codename="lucius",
            firing_id=f"fid-{n}",
            change_type="modified",
        )
    assert len(brain.list_file_touches(limit=10_000)) == 500
    assert brain.count_file_touches() == total
    assert brain.count_file_touches(repo="org/api") == total
    assert brain.count_file_touches(repo="missing") == 0


def test_worker_heartbeat_and_stale_detection(brain: FleetBrain) -> None:
    now = datetime.now(UTC)
    fresh = brain.upsert_worker_heartbeat(
        codename="lucius",
        firing_id="fresh",
        repo="org/api",
        pid=123,
        heartbeat_at=now,
    )
    stale = brain.upsert_worker_heartbeat(
        codename="bane",
        firing_id="stale",
        heartbeat_at=now - timedelta(minutes=90),
    )

    assert fresh.id == "lucius:fresh"
    assert stale.status == "running"
    assert [w.firing_id for w in brain.list_stale_workers(max_age_minutes=60)] == ["stale"]


def test_memory_promotion_suggestions(brain: FleetBrain) -> None:
    low = brain.propose_memory(
        codename="lucius",
        repo="org/api",
        body="Speculative, no evidence.",
        confidence=0.4,
    )
    high = brain.propose_memory(
        codename="lucius",
        repo="org/api",
        body="Use fixtures for API tests.",
        tags=["tests"],
        evidence="Seen in three recent PRs.",
        confidence=0.8,
    )
    suggestions = brain.suggest_memory_promotions()
    assert [s["candidate_id"] for s in suggestions] == [high.id]
    assert low.id not in {s["candidate_id"] for s in suggestions}


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
    brain.upsert_github_item(
        repo="org/api",
        number=1,
        kind="issue",
        state="open",
        labels=["agent:bundle:billing"],
    )
    brain.upsert_worker_heartbeat(codename="lucius", firing_id="fid")
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
    assert len(data["github_items"]) == 1
    assert len(data["bundle_items"]) == 1
    assert len(data["worker_heartbeats"]) == 1


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


def test_fleetbrain_from_env_respects_public_paths(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-from-env.db"
    brain = FleetBrain.from_env(
        {
            "ALFRED_FLEET_BRAIN_DB": str(explicit),
            "ALFRED_HOME": str(tmp_path / "ignored-home"),
        }
    )
    brain.reflect(codename="batman", repo="org/api", body="remember the explicit path")
    assert explicit.exists()

    home = tmp_path / "alfred-home"
    home_brain = FleetBrain.from_env({"ALFRED_HOME": str(home)})
    home_brain.reflect(codename="lucius", repo="org/web", body="remember the home path")
    assert (home / "fleet-brain.db").exists()


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
    brain.upsert_github_item(
        repo="org/api",
        number=1,
        kind="issue",
        state="open",
        labels=["agent:bundle:billing"],
    )
    brain.upsert_worker_heartbeat(codename="lucius", firing_id="fid")
    s = brain.stats()
    assert s["lessons"] == 2
    assert s["firings"] == 1
    assert s["file_touches"] == 1
    assert s["memory_candidates"] == 1
    assert s["memory_candidates_open"] == 1
    assert s["failure_events"] == 1
    assert s["github_items"] == 1
    assert s["bundle_items"] == 1
    assert s["worker_heartbeats"] == 1
    assert s["workers_running"] == 1
    assert s["repo_notes"] == 1
    assert s["tags"] == 2
    assert s["codenames"] == 2
    assert s["repos"] == 2


def test_failure_patterns_classify_repeated_setup_failures(brain: FleetBrain) -> None:
    now = datetime.now(UTC)
    for idx in range(2):
        brain.record_failure(
            codename="huntress",
            repo="org/web",
            firing_id=f"fid-{idx}",
            subtype="error_timeout",
            summary=("browserType.launch: Executable doesn't exist at chromium_headless_shell"),
            engine="claude",
            created_at=now - timedelta(minutes=idx),
        )

    patterns = brain.list_failure_patterns(window_days=7, min_count=2)

    assert len(patterns) == 1
    assert patterns[0]["classification"] == "local_setup"
    assert patterns[0]["suggested_action"] == "file_setup_issue"
    assert patterns[0]["severity"] == "blocker"


def test_failure_patterns_ignore_classifier_tokens_in_codename(brain: FleetBrain) -> None:
    for idx in range(2):
        brain.record_failure(
            codename="playwright-runner",
            repo="org/web",
            firing_id=f"fid-{idx}",
            subtype="rate_limit",
            summary="provider quota exhausted",
            engine="claude",
        )

    patterns = brain.list_failure_patterns(window_days=7, min_count=2)

    assert len(patterns) == 1
    assert patterns[0]["classification"] == "provider_limit"
    assert patterns[0]["suggested_action"] == "retry_later"


def test_failure_patterns_ignore_informational_terminal_events(brain: FleetBrain) -> None:
    for idx in range(3):
        brain.record_failure(
            codename="rasalghul",
            repo="org/app",
            firing_id=f"review-{idx}",
            subtype="review-posted",
            summary="review-posted",
        )
    for idx in range(2):
        brain.record_failure(
            codename="lucius",
            repo="org/app",
            firing_id=f"test-{idx}",
            subtype="test_ok",
            summary="test_ok",
        )

    assert brain.list_failure_patterns(window_days=7, min_count=2) == []


def test_reliability_report_surfaces_actions(brain: FleetBrain) -> None:
    stale_at = datetime.now(UTC) - timedelta(hours=2)
    brain.upsert_worker_heartbeat(
        codename="lucius",
        firing_id="stale",
        status="running",
        heartbeat_at=stale_at,
    )
    brain.propose_memory(
        codename="lucius",
        repo="org/api",
        body="Use request fixtures.",
        evidence="Observed in PR 42.",
        confidence=0.9,
    )

    report = brain.reliability_report()

    assert report["status"] == "warn"
    assert {item["kind"] for item in report["actions"]} == {
        "stale_worker",
        "memory_promotion",
    }
    memory_action = next(item for item in report["actions"] if item["kind"] == "memory_promotion")
    assert memory_action["target"] is None


def test_reliability_report_actions_cover_each_visible_signal(brain: FleetBrain) -> None:
    for pattern_idx in range(6):
        for event_idx in range(2):
            brain.record_failure(
                codename=f"agent-{pattern_idx}",
                subtype="error_timeout",
                summary="implementation timed out",
                event_id=f"failure-{pattern_idx}-{event_idx}",
            )
    brain.upsert_worker_heartbeat(
        codename="stale-agent",
        firing_id="stale",
        status="running",
        heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
    )

    report = brain.reliability_report(limit=6)

    assert len(report["failure_patterns"]) == 6
    assert len(report["stale_workers"]) == 1
    assert any(action["kind"] == "stale_worker" for action in report["actions"])


def test_doctor_flags_repeated_blocker_patterns(brain: FleetBrain) -> None:
    for idx in range(3):
        brain.record_failure(
            codename="lucius",
            subtype="error_timeout",
            summary="implementation timed out",
            event_id=f"failure-{idx}",
        )

    report = brain.doctor()

    assert report["status"] == "fail"
    assert any(
        check["name"] == "reliability_governor" and check["status"] == "fail"
        for check in report["checks"]
    )


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
