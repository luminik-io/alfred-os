"""Focused tests for the local ``alfred serve`` planning routes."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import server.views as server_views  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fleet_brain import Lesson  # noqa: E402
from server import FilesystemReader, create_app  # noqa: E402
from spec_helper import IssueDraft  # noqa: E402


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _server_token(state: Path) -> str:
    """Read the per-launch token ``create_app`` wrote under the state root."""
    return (state / "server-token").read_text(encoding="utf-8").strip()


def _auth_headers(state: Path, **extra: str) -> dict[str, str]:
    """Headers a legitimate native-client POST sends: token + same-origin."""
    headers = {
        server_views.SERVER_TOKEN_HEADER: _server_token(state),
        "origin": "http://testserver",
    }
    headers.update(extra)
    return headers


def test_json_api_status_firings_and_plans(tmp_path: Path) -> None:
    state = tmp_path / "state"
    plans = tmp_path / "batman-plans"
    _write_jsonl(
        state / "batman" / "events" / "2026-05-27-1200-aa.jsonl",
        [
            {"ts": "2026-05-27T12:00:00Z", "event": "firing_started"},
            {"ts": "2026-05-27T12:04:00Z", "event": "firing_complete"},
        ],
    )
    plans.mkdir()
    (plans / "61-plan.md").write_text(
        "# Batman Plan for Issue #61\n\n"
        "**Status:** Draft (awaiting approval)\n\n"
        "**Issue URL:** https://github.com/example-org/alfred/issues/61\n\n"
        "**Affected Repos:** backend, frontend\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    status = client.get("/api/status")
    assert status.status_code == 200
    batman = status.json()["agents"][0]
    assert batman["codename"] == "batman"
    assert batman["display_name"] == "Batman"
    assert batman["role_title"] == "Architect"

    firings = client.get("/api/firings", params={"codename": "batman"})
    assert firings.status_code == 200
    assert firings.json()["rows"][0]["status"] == "ok"

    detail = client.get("/api/firings/2026-05-27-1200-aa")
    assert detail.status_code == 200
    assert detail.json()["raw_events"][1]["event"] == "firing_complete"

    plan = client.get("/api/plans/61-plan")
    assert plan.status_code == 200
    assert plan.json()["title"] == "Batman Plan for Issue #61"


def test_api_firings_surface_distilled_timeline(tmp_path: Path) -> None:
    """The /api/firings JSON must carry the honest, render-ready timeline so the
    desktop Activity view can show a one-line headline + expandable steps + an
    honestly-classified error without re-deriving anything client-side."""
    state = tmp_path / "state"
    # A clean PR-opened run.
    _write_jsonl(
        state / "bane" / "events" / "2026-05-27-1200-ok.jsonl",
        [
            {"ts": "2026-05-27T12:00:00Z", "event": "firing_started"},
            {"ts": "2026-05-27T12:01:00Z", "event": "repo_picked", "repo": "acme-org/api"},
            {
                "ts": "2026-05-27T12:05:00Z",
                "event": "llm_invoke_done",
                "engine": "claude",
                "turns": 12,
                "subtype": "success",
                "success": True,
            },
            {
                "ts": "2026-05-27T12:06:00Z",
                "event": "pr_opened",
                "url": "https://github.com/acme-org/api/pull/1048",
                "repo": "acme-org/api",
            },
            {"ts": "2026-05-27T12:06:10Z", "event": "firing_complete", "outcome": "pr-opened"},
        ],
    )
    # An honest auth failure whose provider text reads like a rate limit.
    _write_jsonl(
        state / "bane" / "events" / "2026-05-27-1300-err.jsonl",
        [
            {"ts": "2026-05-27T13:00:00Z", "event": "firing_started"},
            {
                "ts": "2026-05-27T13:01:00Z",
                "event": "llm_fallback",
                "from_engine": "claude",
                "to_engine": "codex",
                "reason": "API Error: 429 rate_limit_exceeded",
            },
            {
                "ts": "2026-05-27T13:02:00Z",
                "event": "llm_invoke_done",
                "engine": "codex",
                "turns": 2,
                "subtype": "error_authentication",
                "success": False,
            },
            {
                "ts": "2026-05-27T13:02:10Z",
                "event": "firing_complete",
                "outcome": "llm-error_authentication",
            },
        ],
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    rows = client.get("/api/firings", params={"codename": "bane"}).json()["rows"]
    by_id = {row["firing_id"]: row for row in rows}

    ok = by_id["2026-05-27-1200-ok"]["timeline"]
    assert ok["severity"] == "ok"
    assert ok["error"] is None
    assert ok["headline"] == "Opened PR #1048"
    assert [step["kind"] for step in ok["steps"]][-1] == "complete"

    err = by_id["2026-05-27-1300-err"]["timeline"]
    assert err["severity"] == "error"
    assert err["error"] == "authentication"
    assert "authentication" in err["headline"].lower()
    assert "rate" not in err["headline"].lower()


def test_api_status_reports_paused_state_from_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    # An agent with state but no pause marker is loaded + not paused.
    _write_jsonl(
        state / "lucius" / "events" / "2026-05-30-1000-aa.jsonl",
        [
            {"ts": "2026-05-30T10:00:00Z", "event": "firing_started"},
            {"ts": "2026-05-30T10:02:00Z", "event": "firing_complete"},
        ],
    )
    # A paused agent: marker present, body carries the pause timestamp.
    _write_jsonl(
        state / "bane" / "events" / "2026-05-30-0900-bb.jsonl",
        [
            {"ts": "2026-05-30T09:00:00Z", "event": "firing_started"},
            {"ts": "2026-05-30T09:01:00Z", "event": "firing_complete"},
        ],
    )
    pause_dir = state / "_paused"
    pause_dir.mkdir(parents=True)
    (pause_dir / "bane").write_text(
        "2026-05-30T09:00:00Z fail-streak self-pause\n", encoding="utf-8"
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    payload = client.get("/api/status").json()
    by_codename = {agent["codename"]: agent for agent in payload["agents"]}

    assert by_codename["lucius"]["paused"] is False
    assert by_codename["lucius"]["loaded"] is True
    assert by_codename["lucius"]["paused_since"] is None

    assert by_codename["bane"]["paused"] is True
    # A paused agent is unloaded from the scheduler by ``alfred pause``.
    assert by_codename["bane"]["loaded"] is False
    assert by_codename["bane"]["paused_since"] == "2026-05-30T09:00:00Z"


def test_api_status_orders_and_profiles_core_agents(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for codename in ("rasalghul", "lucius", "drake", "batman"):
        _write_jsonl(
            state / codename / "events" / f"2026-05-30-1000-{codename}.jsonl",
            [{"ts": "2026-05-30T10:00:00Z", "event": "firing_complete"}],
        )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    agents = client.get("/api/status").json()["agents"]
    assert [agent["codename"] for agent in agents[:4]] == [
        "batman",
        "lucius",
        "drake",
        "rasalghul",
    ]
    assert agents[0]["role_title"] == "Architect"
    assert agents[1]["role_title"] == "Senior Developer"
    assert agents[2]["role_title"] == "Planner"


def test_api_status_includes_scheduled_agents_before_first_firing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_jsonl(
        state / "cleanup" / "events" / "2026-06-01-0900-aa.jsonl",
        [{"ts": "2026-06-01T09:00:00Z", "event": "firing_complete"}],
    )
    _write_jsonl(
        state / "codenames" / "cleanup" / "events" / "2026-06-01-0910-aa.jsonl",
        [{"ts": "2026-06-01T09:10:00Z", "event": "firing_complete"}],
    )
    conf = tmp_path / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "# label\tscript\tschedule\tneeds_java\tlog_stem\trole\n"
        "alfred.lucius\tlucius.py\tinterval:1200\tno\talfred.lucius\tfeature dev\n"
        "alfred.batman\tbatman.py\tinterval:3600\tno\talfred.batman\tcross-repo architect\n"
        "alfred.agent-cleanup\tagent-cleanup.py\tcron:3:00\tno\t"
        "alfred.agent-cleanup\thygiene\n"
        "alfred.memory-auto-promote\tmemory-auto-promote.py\tcron:8:20\tno\t"
        "alfred.memory-auto-promote\tmemory auto-promote\n"
        "alfred.shipped-summary-daily\tshipped-summary-daily.sh\tcron:7:35\tno\t"
        "alfred.shipped-summary\tshipped summary daily\n"
        "#alfred.huntress\thuntress.py\tinterval:1800\tno\talfred.huntress\tstaging smoke runner\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    agents = client.get("/api/status").json()["agents"]
    by_codename = {agent["codename"]: agent for agent in agents}

    assert [agent["codename"] for agent in agents] == [
        "batman",
        "lucius",
        "agent-cleanup",
        "memory-auto-promote",
        "shipped-summary-daily",
    ]
    assert by_codename["batman"]["status"] == "idle"
    assert by_codename["batman"]["last_summary"] == "no firings yet"
    assert by_codename["batman"]["loaded"] is True
    assert by_codename["batman"]["display_name"] == "Batman"
    assert by_codename["agent-cleanup"]["display_name"] == "Agent Cleanup"
    assert "cleanup" not in by_codename
    assert by_codename["memory-auto-promote"]["role_title"] == "Memory Judge"
    assert by_codename["shipped-summary-daily"]["role_title"] == "Shipping Digest"
    assert "huntress" not in by_codename


def test_api_memory_candidates_promote_and_reject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"

    class FakeBrain:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def list_memory_candidates(
            self,
            *,
            status: str | None,
            limit: int,
            **_filters: object,
        ) -> list[dict[str, object]]:
            self.calls.append(("list", (status, limit)))
            return [
                {
                    "id": "01JYS9RY6W0M3T5J6QAF8D4P8B",
                    "source": "slack",
                    "agent": "lucius",
                    "repo": "example-org/alfred",
                    "topic": "planning",
                    "body": "Keep Slack memories reviewable.",
                    "evidence": [{"source": "slack"}],
                    "status": "candidate",
                    "created_at": datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
                }
            ]

        def promote_memory_candidate(
            self,
            candidate_id: str,
            *,
            reviewer: str,
            review_note: str = "",
        ) -> Lesson:
            self.calls.append(("promote", (candidate_id, reviewer, review_note)))
            return Lesson(
                id=f"lesson:memory_candidate:{candidate_id}",
                codename="lucius",
                repo="example-org/alfred",
                body="Keep Slack memories reviewable.",
                tags=[],
                created_at=datetime(2026, 5, 30, 12, 5, tzinfo=UTC),
                firing_id=None,
            )

        def reject_memory_candidate(
            self,
            candidate_id: str,
            *,
            reviewer: str,
            review_note: str = "",
        ) -> dict[str, object] | None:
            self.calls.append(("reject", (candidate_id, reviewer, review_note)))
            return {
                "id": candidate_id,
                "agent": "lucius",
                "repo": "example-org/alfred",
                "status": "rejected",
                "review_note": review_note,
            }

    brain = FakeBrain()
    monkeypatch.setattr(server_views, "_memory_brain", lambda *_a, **_kw: (brain, None))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    candidates = client.get("/api/memory/candidates")
    assert candidates.status_code == 200
    rows = candidates.json()["rows"]
    candidate_id = "01JYS9RY6W0M3T5J6QAF8D4P8B"
    assert rows[0]["id"] == candidate_id
    assert rows[0]["codename"] == "lucius"
    assert rows[0]["status"] == "candidate"
    assert rows[0]["tags"] == []
    assert rows[0]["severity"] == "info"
    assert rows[0]["confidence"] == 0.5
    assert rows[0]["source_firing_id"] is None
    assert json.loads(rows[0]["evidence"]) == [{"source": "slack"}]
    assert rows[0]["created_at"].endswith("+00:00")
    assert brain.calls[0] == ("list", ("candidate", 50))

    retired = client.get("/api/memory/candidates?status=retired")
    assert retired.status_code == 200
    assert brain.calls[1] == ("list", ("retired", 50))

    promoted = client.post(
        f"/api/memory/candidates/{candidate_id}/promote",
        json={"reviewer": "operator", "note": "useful"},
        headers=_auth_headers(state),
    )
    assert promoted.status_code == 200
    assert promoted.json()["lesson_id"] == f"lesson:memory_candidate:{candidate_id}"
    assert promoted.json()["status"] == "validated"
    assert promoted.json()["codename"] == "lucius"

    rejected = client.post(
        f"/api/memory/candidates/{candidate_id}/reject",
        json={"reviewer": "operator", "note": "too broad"},
        headers=_auth_headers(state),
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["review_note"] == "too broad"


def test_api_memory_lessons_lists_active_lessons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"

    class FakeBrain:
        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def list_lessons(self, *, limit: int) -> list[Lesson]:
            assert limit <= 200
            return [
                Lesson(
                    id="lesson:1",
                    codename="lucius",
                    repo="example-org/alfred",
                    body="GraphQL schema lives in src/schema.graphql.",
                    tags=["graphql"],
                    created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
                    firing_id=None,
                )
            ]

    brain = FakeBrain()
    monkeypatch.setattr(server_views, "_memory_brain", lambda *_a, **_kw: (brain, None))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.get("/api/memory/lessons")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["id"] == "lesson:1"
    assert rows[0]["codename"] == "lucius"
    assert rows[0]["body"].startswith("GraphQL")
    assert rows[0]["tags"] == ["graphql"]
    assert rows[0]["severity"] == "info"


def test_api_memory_candidate_value_error_is_bad_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ValueError from the brain is a validation rejection, so answer 400.

    The candidate was found, but the action could not be applied. Returning 404
    would let a client mistake this for "candidate disappeared" and silently
    retry or suppress the error. The body stays generic so no exception detail
    leaks (py/stack-trace-exposure).
    """
    state = tmp_path / "state"
    marker = _exc_sentinel("promote-value-error")

    class RejectingBrain:
        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def promote_memory_candidate(
            self, candidate_id: str, *, reviewer: str, review_note: str = ""
        ) -> dict[str, object] | None:
            raise ValueError(marker)

    monkeypatch.setattr(server_views, "_memory_brain", lambda *_a, **_kw: (RejectingBrain(), None))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/memory/candidates/101/promote",
        json={"reviewer": "operator", "note": "bad"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "internal error"
    _assert_no_exc_leak(body, marker)


def test_api_memory_candidate_colon_id_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"

    class Brain:
        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def promote_memory_candidate(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("invalid id must not reach FleetBrain")

    monkeypatch.setattr(server_views, "_memory_brain", lambda *_a, **_kw: (Brain(), None))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/memory/candidates/lesson:memory_candidate:abc/promote",
        json={"reviewer": "operator", "note": "bad"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "memory candidate id is invalid"


def test_api_memory_candidate_unknown_id_is_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown candidate id raises ValueError in the brain, but it must stay a
    clean 404 (not the generic 400) so a stale client gets the expected
    "not found" rather than an internal-error action failure. The brain message
    is inspected internally to pick the status but is never echoed.
    """
    state = tmp_path / "state"

    class MissingBrain:
        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def reject_memory_candidate(
            self, candidate_id: str, *, reviewer: str, review_note: str = ""
        ) -> dict[str, object] | None:
            raise ValueError(f"reject_memory_candidate: unknown candidate {candidate_id!r}")

    monkeypatch.setattr(server_views, "_memory_brain", lambda *_a, **_kw: (MissingBrain(), None))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/memory/candidates/999/reject",
        json={"reviewer": "operator", "note": "stale"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "memory candidate not found"
    # The brain's internal message (method name, id) must not leak.
    assert "reject_memory_candidate" not in str(body)
    assert "unknown candidate" not in str(body)


def test_api_memory_candidates_reject_cross_origin_posts(tmp_path: Path) -> None:
    client = TestClient(create_app(FilesystemReader(state_root=tmp_path / "state")))

    response = client.post(
        "/api/memory/candidates/101/promote",
        headers={"origin": "https://example.com"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_api_status_paused_marker_without_timestamp_uses_mtime(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_jsonl(
        state / "robin" / "events" / "2026-05-30-1000-cc.jsonl",
        [{"ts": "2026-05-30T10:00:00Z", "event": "firing_complete"}],
    )
    pause_dir = state / "_paused"
    pause_dir.mkdir(parents=True)
    marker = pause_dir / "robin"
    # An empty/legacy marker body has no parseable timestamp; the reader falls
    # back to the marker file mtime rather than reporting None.
    marker.write_text("", encoding="utf-8")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    payload = client.get("/api/status").json()
    robin = next(a for a in payload["agents"] if a["codename"] == "robin")

    assert robin["paused"] is True
    assert robin["loaded"] is False
    assert robin["paused_since"] is not None


def test_json_api_lists_slack_planning_drafts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    (drafts / "slack-20260529-0400-E1.json").write_text(
        json.dumps(
            {
                "source": "slack",
                "created_at": "2026-05-29T04:00:00Z",
                "draft": {
                    "title": "Add threaded plan revisions",
                    "problem": "Operators need to revise Alfred plans before implementation.",
                    "desired_behavior": "Replies update the saved plan draft.",
                    "repos": ["example-org/alfred"],
                },
                "spec_body": "# Spec\n\nThread replies update readiness.",
                "readiness": {"ok": True, "score": 92, "questions": []},
                "revision_count": 2,
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    plans = client.get("/api/plans")
    assert plans.status_code == 200
    assert plans.json()["rows"][0]["title"] == "Add threaded plan revisions"

    detail = client.get("/api/plans/slack-20260529-0400-E1")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["source"] == "slack"
    assert payload["revision_count"] == 2
    assert payload["readiness_score"] == 92


def _write_planning_draft(
    state: Path,
    draft_id: str,
    *,
    title: str,
    repos: list[str],
    created_at: str = "2026-06-01T04:00:00Z",
    revision_count: int = 0,
    bridge_issue_url: str = "",
) -> Path:
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / f"{draft_id}.json"
    payload: dict[str, object] = {
        "source": "compose",
        "created_at": created_at,
        "updated_at": created_at,
        "draft": {"title": title, "repos": repos},
        "readiness": {"ok": True, "score": 90},
        "revision_count": revision_count,
    }
    if bridge_issue_url:
        payload["bridge"] = {"converted": True, "issue_url": bridge_issue_url}
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_discard_plan_archives_draft_and_removes_it_from_listing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    source = drafts / "compose-junk-01.json"
    source.write_text(
        json.dumps(
            {
                "source": "compose",
                "created_at": "2026-05-29T04:00:00Z",
                "draft": {"title": "Hi", "repos": ["acme/api"]},
                "readiness": {"ok": False, "score": 12},
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))
    assert len(client.get("/api/plans").json()["rows"]) == 1

    response = client.post(
        "/api/plans/compose-junk-01/discard",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "discarded"
    assert not source.exists()
    archived = Path(body["archived_path"])
    assert archived.exists()
    assert archived.parent.name == "archive"
    assert client.get("/api/plans").json()["rows"] == []


def test_discard_plan_archives_matching_deduped_group(tmp_path: Path) -> None:
    state = tmp_path / "state"
    newest = _write_planning_draft(
        state,
        "compose-a-newest",
        title="Add a CSV export",
        repos=["acme/api", "acme/web"],
    )
    mid = _write_planning_draft(
        state,
        "compose-a-mid",
        title="add a CSV export",
        repos=["acme/web", "acme/api"],
    )
    oldest = _write_planning_draft(
        state,
        "compose-a-oldest",
        title="Add a CSV export",
        repos=["acme/api", "acme/web"],
        revision_count=2,
    )
    distinct = _write_planning_draft(
        state,
        "compose-b-distinct",
        title="Fix the login redirect",
        repos=["acme/api"],
    )
    for path, mtime in (
        (newest, 4000),
        (mid, 3000),
        (oldest, 1000),
        (distinct, 2000),
    ):
        os.utime(path, (mtime, mtime))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/compose-a-newest/discard",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "discarded"
    assert body["discarded_count"] == 3
    assert set(body["draft_ids"]) == {
        "compose-a-newest",
        "compose-a-mid",
        "compose-a-oldest",
    }
    assert len(body["archived_paths"]) == 3
    assert not newest.exists()
    assert not mid.exists()
    assert not oldest.exists()
    assert distinct.exists()
    assert {row["plan_id"] for row in client.get("/api/plans").json()["rows"]} == {
        "compose-b-distinct"
    }


def test_discard_plan_keeps_filed_sibling_out_of_deduped_group(tmp_path: Path) -> None:
    state = tmp_path / "state"
    unfiled = _write_planning_draft(
        state,
        "compose-export-unfiled",
        title="Add a CSV export",
        repos=["acme/api", "acme/web"],
    )
    filed = _write_planning_draft(
        state,
        "compose-export-filed",
        title="add a CSV export",
        repos=["acme/web", "acme/api"],
        bridge_issue_url="https://github.com/acme/api/issues/42",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/compose-export-unfiled/discard",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "discarded"
    assert body["discarded_count"] == 1
    assert body["draft_ids"] == ["compose-export-unfiled"]
    assert not unfiled.exists()
    assert filed.exists()
    rows = client.get("/api/plans").json()["rows"]
    assert [row["plan_id"] for row in rows] == ["compose-export-filed"]
    assert rows[0]["parent"] == "https://github.com/acme/api/issues/42"


def test_discard_plan_is_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    (drafts / "compose-dup-02.json").write_text(
        json.dumps({"draft": {"title": "Test", "repos": ["acme/api"]}}),
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    first = client.post(
        "/api/plans/compose-dup-02/discard",
        headers=_auth_headers(state),
    )
    second = client.post(
        "/api/plans/compose-dup-02/discard",
        headers=_auth_headers(state),
    )

    assert first.status_code == 200
    assert first.json()["status"] == "discarded"
    assert second.status_code == 200
    assert second.json()["status"] == "already_discarded"
    assert second.json()["ok"] is True


def test_discard_plan_returns_existing_timestamped_archive_path(tmp_path: Path) -> None:
    state = tmp_path / "state"
    archive = state / "planning-drafts" / "archive"
    archive.mkdir(parents=True)
    archived = archive / "compose-dup-03-20260618-120000.json"
    archived.write_text(json.dumps({"draft": {"title": "Archived"}}), encoding="utf-8")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/compose-dup-03/discard",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_discarded"
    assert Path(body["archived_path"]) == archived
    assert Path(body["archived_path"]).exists()


def test_discard_plan_treats_concurrent_archive_as_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    live = drafts / "compose-race.json"
    live.write_text(json.dumps({"draft": {"title": "Race"}}), encoding="utf-8")
    archive = drafts / "archive" / "compose-race.json"
    client = TestClient(create_app(FilesystemReader(state_root=state)))
    real_replace = Path.replace

    def racing_replace(self: Path, target: Path) -> Path:
        if self == live:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(live.read_text(encoding="utf-8"), encoding="utf-8")
            live.unlink()
            raise FileNotFoundError(str(live))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", racing_replace)

    response = client.post(
        "/api/plans/compose-race/discard",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "already_discarded"
    assert Path(body["archived_path"]) == archive
    assert archive.exists()


def test_discard_plan_requires_token_and_same_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    source = drafts / "compose-guard-03.json"
    source.write_text(json.dumps({"draft": {"title": "Guarded"}}), encoding="utf-8")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    no_token = client.post(
        "/api/plans/compose-guard-03/discard",
        headers={"origin": "http://testserver"},
    )
    cross_origin = client.post(
        "/api/plans/compose-guard-03/discard",
        headers=_auth_headers(state, origin="http://evil.example"),
    )

    assert no_token.status_code == 403
    assert cross_origin.status_code == 403
    assert source.exists()


def test_json_api_lists_slack_followups(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    (followups / "slack-C1-1716480000.000000.md").write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Captured: 2026-05-29T06:45:00Z\n"
        "- Thread: C1 / 1716480000.000000\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `change`: add a manual docs smoke test\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    plans = client.get("/api/plans")
    assert plans.status_code == 200
    assert plans.json()["rows"][0]["source"] == "followup"
    assert plans.json()["rows"][0]["status"] == "needs follow-up"

    detail = client.get("/api/plans/slack-C1-1716480000.000000")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["source"] == "followup"
    assert payload["title"] == "Follow-up for Improve planning loop"
    assert payload["parent"] == "https://github.com/example-org/alfred/issues/120"
    assert "manual docs smoke test" in payload["preview"]
    assert "manual docs smoke test" in payload["content"]


def test_followup_can_be_converted_to_planning_draft(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Captured: 2026-05-29T06:45:00Z\n"
        "- Thread: C1 / 1716480000.000000\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `change`: add a manual docs smoke test\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/plans/slack-C1-1716480000.000000/convert-followup",
        headers=_auth_headers(state),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/plans/followup-")
    drafts = list((state / "planning-drafts").glob("followup-*.json"))
    assert len(drafts) == 1
    payload = json.loads(drafts[0].read_text(encoding="utf-8"))
    assert payload["source"] == "planning"
    assert payload["converted_from"]["plan_id"] == "slack-C1-1716480000.000000"
    assert payload["draft"]["title"] == "Follow up: Improve planning loop"
    assert payload["draft"]["repos"] == ["example-org/alfred"]
    assert "Captured Follow-up Context" in payload["spec_body"]
    assert "manual docs smoke test" in payload["spec_body"]
    assert not source.exists()
    archived = list((followups / "handled").glob("slack-C1-1716480000.000000.md"))
    assert len(archived) == 1
    assert "Follow-up action: converted" in archived[0].read_text(encoding="utf-8")

    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "Plan next pass" not in detail.text
    assert "manual docs smoke test" in detail.text


def test_followup_conversion_derives_repos_from_created_links(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "20260529-bundle.md"
    source.write_text(
        "# Follow-up for rollout bundle\n\n"
        "- Bundle: `rollout-bundle`\n"
        "- Created: https://github.com/example-org/api/pull/42, "
        "https://github.com/example-org/web/issues/77\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `test`: add a smoke test to both shipped slices\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/20260529-bundle/convert-followup",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    draft_path = Path(response.json()["draft_path"])
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["draft"]["repos"] == [
        "example-org/api",
        "example-org/web",
    ]


def test_followup_actions_reject_cross_origin_posts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Cross-origin pages should not be able to mutate this inbox.\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    html_response = client.post(
        "/plans/slack-C1-1716480000.000000/convert-followup",
        headers={"origin": "https://example.invalid"},
        follow_redirects=False,
    )
    response = client.post(
        "/api/plans/slack-C1-1716480000.000000/mark-handled",
        headers={"origin": "https://example.invalid"},
    )

    assert html_response.status_code == 403
    assert response.status_code == 403
    assert source.exists()
    assert not (state / "planning-drafts").exists()
    assert not (followups / "handled").exists()


def test_followup_conversion_removes_draft_when_archive_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Archive failure should not leave a draft behind.\n",
        encoding="utf-8",
    )

    def fail_archive(*_args: object, **_kwargs: object) -> Path:
        raise OSError("simulated archive failure")

    monkeypatch.setattr(server_views, "_archive_followup", fail_archive)
    client = TestClient(
        create_app(FilesystemReader(state_root=state)),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/plans/slack-C1-1716480000.000000/convert-followup",
        headers=_auth_headers(state),
    )

    assert response.status_code == 500
    assert source.exists()
    assert list((state / "planning-drafts").glob("followup-*.json")) == []


def test_followup_can_be_marked_handled(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Already answered in the PR thread.\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/slack-C1-1716480000.000000/mark-handled",
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert not source.exists()
    archived_path = Path(response.json()["archived_path"])
    assert archived_path.exists()
    assert archived_path.parent.name == "handled"
    assert "Follow-up action: handled" in archived_path.read_text(encoding="utf-8")
    plans = client.get("/api/plans").json()["rows"]
    assert plans == []


def test_plan_detail_embeds_token_and_html_form_post_uses_it(tmp_path: Path) -> None:
    """The server-rendered plan page is the only client for the HTML form
    routes and cannot set a custom header. It must embed the per-launch token
    as a hidden ``_token`` field, and the POST handler must accept that field
    (a browser form never sends ``SERVER_TOKEN_HEADER``)."""
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    (followups / "slack-C1-1716480000.000000.md").write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Already answered in the PR thread.\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    detail = client.get("/plans/slack-C1-1716480000.000000")
    assert detail.status_code == 200
    token = _server_token(state)
    # The GET page must carry the token so the form can echo it back.
    assert f'name="_token" value="{token}"' in detail.text

    # A browser form POST: same-origin + token in the body, NO custom header.
    handled = client.post(
        "/plans/slack-C1-1716480000.000000/mark-handled",
        headers={"origin": "http://testserver"},
        data={"_token": token},
        follow_redirects=False,
    )
    assert handled.status_code == 303
    assert not (followups / "slack-C1-1716480000.000000.md").exists()


def test_html_form_post_without_token_is_forbidden(tmp_path: Path) -> None:
    """Same-origin alone is not enough: a form POST missing the ``_token`` body
    field (e.g. a header-less drive-by) is rejected and mutates nothing."""
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Header-less callers must not be able to mutate this inbox.\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/plans/slack-C1-1716480000.000000/mark-handled",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert source.exists()
    assert not (followups / "handled").exists()


def test_html_form_post_with_malformed_token_body_is_forbidden(tmp_path: Path) -> None:
    """Invalid form bytes fail closed rather than raising before auth."""
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n\n"
        "Malformed form bodies must not mutate this inbox.\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/plans/slack-C1-1716480000.000000/mark-handled",
        content=b"\xff",
        headers={
            "origin": "http://testserver",
            "content-type": "application/x-www-form-urlencoded",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert source.exists()
    assert not (followups / "handled").exists()


def test_json_plan_empty_body_does_not_return_raw_slack_event(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    (drafts / "slack-empty-body.json").write_text(
        json.dumps(
            {
                "source": "slack",
                "created_at": "2026-05-29T04:00:00Z",
                "draft": {
                    "title": "Clarify plan intake",
                    "problem": "Operators need a clean preview.",
                    "desired_behavior": "",
                    "repos": ["example-org/alfred"],
                },
                "issue_body": "",
                "spec_body": "",
                "event": {
                    "user": "USECRET",
                    "channel": "CSECRET",
                    "text": "raw slack text",
                },
            }
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    detail = client.get("/api/plans/slack-empty-body")

    assert detail.status_code == 200
    assert detail.json()["content"] == "Operators need a clean preview."
    assert "USECRET" not in detail.json()["content"]
    assert "raw slack text" not in detail.json()["content"]


def test_json_api_skips_invalid_newer_draft_to_fill_limit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    valid = drafts / "valid.json"
    valid.write_text(
        json.dumps(
            {
                "source": "slack",
                "created_at": "2026-05-29T04:00:00Z",
                "draft": {"title": "Valid older draft", "problem": "Keep reading."},
            }
        ),
        encoding="utf-8",
    )
    invalid = drafts / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    now = datetime.now(UTC).timestamp()
    os.utime(valid, (now - 10, now - 10))
    os.utime(invalid, (now, now))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/plans", params={"limit": 1})

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["title"] == "Valid older draft"


def test_api_actions_preserves_reliability_errors(tmp_path: Path) -> None:
    class Reader(FilesystemReader):
        def reliability_report(self) -> dict[str, object]:
            return {
                "status": "warn",
                "actions": [],
                "failure_patterns": [],
                "stale_workers": [],
                "promotion_suggestions": [],
                "errors": {"promotion_suggestions": "bridge unavailable"},
            }

    client = TestClient(create_app(Reader(state_root=tmp_path / "state")))

    response = client.get("/api/actions")

    assert response.status_code == 200
    assert response.json()["errors"] == {"promotion_suggestions": "bridge unavailable"}


def test_planning_save_spec_applies_pending_chat_message(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before "
                "implementation so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "example-org/alfred\nexample-org/web",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "chat_message": (
                "acceptance: saved specs include chat amendments\nremove repo: example-org/web"
            ),
            "action": "save_spec",
        },
    )

    assert response.status_code == 200
    specs = list((tmp_path / "spec-drafts").glob("*.md"))
    assert specs
    saved_spec = max(specs, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
    assert "saved specs include chat amendments" in saved_spec
    assert "example-org/web" not in saved_spec


def test_planning_memory_provider_ignores_runtime_env_for_temp_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("FLEET_BRAIN_HOST", "127.0.0.1")

    def fail_loader():
        raise AssertionError("temporary state must not load the real memory provider")

    monkeypatch.setattr(server_views, "_load_planning_memory_provider_from_env", fail_loader)
    app = create_app(FilesystemReader(state_root=tmp_path / "state"))
    request = SimpleNamespace(app=app)

    assert server_views._planning_memory_provider(request) is None


def test_planning_memory_provider_loads_for_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime_state = runtime / "state"
    runtime_state.mkdir(parents=True)
    sentinel = object()
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setattr(
        server_views,
        "_load_planning_memory_provider_from_env",
        lambda: sentinel,
    )
    app = create_app(FilesystemReader(state_root=runtime_state))
    request = SimpleNamespace(app=app)

    assert server_views._planning_memory_provider(request) is sentinel


def test_planning_page_surfaces_memory_and_queues_spec_candidate(
    tmp_path: Path,
) -> None:
    class Memory:
        name = "test"

        def __init__(self) -> None:
            self.candidates: list[dict[str, object]] = []

        def recall(self, *, repo=None, query=None, limit=3):
            return [
                {
                    "repo": repo,
                    "body": "Slack plans should show explicit revision commands.",
                    "tags": ["planning"],
                }
            ]

        def propose_memory(self, **kwargs):
            self.candidates.append(kwargs)
            return len(self.candidates)

    state = tmp_path / "state"
    state.mkdir()
    memory = Memory()
    app = create_app(FilesystemReader(state_root=state))
    app.state.planning_memory_provider = memory
    app.state.planning_memory_writer = memory
    client = TestClient(app)

    response = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before "
                "implementation so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "example-org/alfred",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "action": "save_spec",
        },
    )

    assert response.status_code == 200
    assert "Planning memory" in response.text
    assert "Slack plans should show explicit revision commands." in response.text
    assert "Memory review queued" in response.text
    assert len(memory.candidates) == 1
    assert memory.candidates[0]["source"] == "planning-ui"
    assert memory.candidates[0]["repo"] == "example-org/alfred"


def test_planning_memory_candidate_uses_writable_provider_inside_tuple_chain(
    tmp_path: Path,
) -> None:
    class ReadOnly:
        name = "readonly"

        def recall(self, *, repo=None, query=None, limit=3):
            return []

    class Writable:
        name = "writable"

        def __init__(self) -> None:
            self.candidates: list[dict[str, object]] = []

        def recall(self, *, repo=None, query=None, limit=3):
            return []

        def propose_memory(self, **kwargs):
            self.candidates.append(kwargs)
            return "candidate-1"

    class Chain:
        name = "chained"

        def __init__(self) -> None:
            self.writable = Writable()
            self.providers = (ReadOnly(), self.writable)

        def recall(self, *, repo=None, query=None, limit=3):
            return []

    state = tmp_path / "state"
    state.mkdir()
    chain = Chain()
    app = create_app(FilesystemReader(state_root=state))
    app.state.planning_memory_provider = chain
    client = TestClient(app)

    response = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before "
                "implementation so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "example-org/alfred",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "action": "save_spec",
        },
    )

    assert response.status_code == 200
    assert chain.writable.candidates
    assert chain.writable.candidates[0]["repo"] == "example-org/alfred"


def test_planning_memory_candidate_old_api_object_id_is_extracted(
    tmp_path: Path,
) -> None:
    class Candidate:
        id = "candidate-object-1"

    class LegacyWriter:
        def propose_memory(self, **kwargs):
            if "codename" in kwargs:
                raise TypeError("legacy api")
            return Candidate()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    draft = IssueDraft(
        title="Add Slack plan revision flow",
        problem="Operators need to discuss a plan before implementation.",
        desired_behavior="Alfred saves the refined plan.",
        repos=["example-org/alfred"],
        acceptance_criteria=["Saved spec queues a memory candidate."],
        test_plan="Unit test the fallback.",
    )

    ids = server_views._propose_planning_memory_candidate(
        request,
        draft,
        spec_path=tmp_path / "spec.md",
        spec_body="spec",
        memory_provider=LegacyWriter(),
    )

    assert ids == ("candidate-object-1",)


def test_compose_draft_returns_readiness_and_saves(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/draft",
        json={"text": "title: Add a desktop compose panel"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_id"].startswith("compose-")
    assert isinstance(payload["readiness"]["score"], int)
    # A sparse draft is not ready and must surface clarifying questions.
    assert payload["readiness"]["ok"] is False
    assert payload["questions"]
    assert any(finding["severity"] == "error" for finding in payload["findings"])
    assert payload["draft"]["title"] == "Add a desktop compose panel"

    saved = Path(payload["saved_path"])
    assert saved.exists()
    assert saved.parent == state / "planning-drafts"
    on_disk = json.loads(saved.read_text(encoding="utf-8"))
    assert on_disk["source"] == "compose"
    assert on_disk["revision_count"] == 1

    # The saved compose draft is listable via the shared plans API.
    plans = client.get("/api/plans").json()["rows"]
    assert any(row["plan_id"] == payload["draft_id"] for row in plans)


def test_compose_draft_plain_prose_builds_a_useful_starter_spec(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/draft",
        json={
            "text": (
                "Make the Plan work screen help a non-technical operator turn a "
                "messy product idea into a reviewable GitHub issue with clear "
                "acceptance criteria, the right Alfred agent labels, and a "
                "simple approval path."
            )
        },
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    draft = payload["draft"]
    assert draft["title"] == "Plan work drafts reviewable GitHub issues"
    assert "current flow" in draft["problem"].lower()
    assert "non-technical operator" in draft["user"].lower()
    assert "reviewable GitHub issue" in draft["desired_behavior"]
    assert any("acceptance criteria" in item for item in draft["acceptance_criteria"])
    assert any("agent labels" in item for item in draft["acceptance_criteria"])
    assert any("approval path" in item for item in draft["acceptance_criteria"])
    assert draft["test_plan"]
    assert payload["readiness"]["score"] > 0
    assert payload["readiness"]["ok"] is False
    assert "Which part of the workspace should Alfred change?" in payload["questions"]
    assert "Operator note:" not in payload["spec_body"]


def test_compose_draft_plain_prose_titles_review_queue_starters(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/draft",
        json={
            "text": (
                "The review queue is hard to scan at small window sizes. Make it "
                "usable without hiding important decisions."
            )
        },
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft"]["title"] == "Make review queue usable at small sizes"
    assert payload["readiness"]["score"] > 0


def test_compose_draft_iterates_on_same_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    first = client.post(
        "/api/plans/draft",
        json={
            "text": (
                "title: Ship the compose panel\n"
                "problem: Operators cannot author specs inside the desktop client "
                "today and must drop into Slack or the CLI."
            )
        },
        headers=_auth_headers(state),
    ).json()
    draft_id = first["draft_id"]
    assert first["readiness"]["ok"] is False

    second = client.post(
        "/api/plans/draft",
        json={
            "draft_id": draft_id,
            "text": (
                "desired: A Compose tab posts intent to the draft API and renders "
                "the readiness score plus clarifying questions.\n"
                "repo: example-org/alfred\n"
                "acceptance: Submitting intent renders the readiness score.\n"
                "test: Vitest covers the compose submit path with a mocked api."
            ),
        },
        headers=_auth_headers(state),
    ).json()

    # Same draft id, revisions accumulate, and the score improves.
    assert second["draft_id"] == draft_id
    assert second["revision_count"] == 2
    assert second["readiness"]["score"] > first["readiness"]["score"]
    assert "example-org/alfred" in second["draft"]["repos"]

    drafts = client.get("/api/plans/drafts").json()["rows"]
    matching = [row for row in drafts if row["draft_id"] == draft_id]
    assert len(matching) == 1
    assert matching[0]["revision_count"] == 2


def test_file_plan_issue_files_ready_draft_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.setup as setup_mod
    import slack_issue_bridge

    state = tmp_path / "state"
    state.mkdir()
    draft_id = "compose-20260619-120000-file-gh-issue"
    draft_path = state / "planning-drafts" / f"{draft_id}.json"
    draft_path.parent.mkdir(parents=True)
    draft_path.write_text(
        json.dumps(
            {
                "source": "compose",
                "draft_id": draft_id,
                "draft": {
                    "title": "File ready plans from native",
                    "repos": ["acme-org/api"],
                },
                "issue_body": "## Problem\n\nReady native plans need queue pickup.",
                "readiness": {"ok": True, "score": 95},
                "questions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_issue_creator(*, repo: str, title: str, body: str, labels: list[str]) -> str:
        calls.append({"repo": repo, "title": title, "body": body, "labels": labels})
        return "https://github.com/acme-org/api/issues/42"

    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "")
    monkeypatch.setenv("ALFRED_BRIDGE_LABEL", "agent:implement")
    monkeypatch.setattr(setup_mod, "selected_repos", lambda: ["acme-org/api"])
    monkeypatch.setattr(slack_issue_bridge, "default_issue_creator", fake_issue_creator)

    client = TestClient(create_app(FilesystemReader(state_root=state)))

    first = client.post(
        f"/api/plans/{draft_id}/file-issue",
        headers=_auth_headers(state),
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["status"] == "filed"
    assert payload["issue_url"] == "https://github.com/acme-org/api/issues/42"
    assert payload["issue_urls"] == ["https://github.com/acme-org/api/issues/42"]
    assert payload["issues_by_repo"] == {
        "acme-org/api": "https://github.com/acme-org/api/issues/42"
    }
    assert payload["repo"] == "acme-org/api"
    assert payload["repos"] == ["acme-org/api"]
    assert payload["label"] == "agent:implement"
    assert payload["labels"] == ["agent:implement"]
    assert len(calls) == 1
    assert calls[0]["repo"] == "acme-org/api"
    assert calls[0]["title"] == "File ready plans from native"
    assert calls[0]["labels"] == ["agent:implement"]
    assert "Ready native plans need queue pickup." in str(calls[0]["body"])
    assert "Alfred Desktop" in str(calls[0]["body"])
    assert "Slack issue bridge" not in str(calls[0]["body"])

    saved = json.loads(draft_path.read_text(encoding="utf-8"))
    assert saved["bridge"]["converted"] is True
    assert saved["bridge"]["source"] == "native-client"
    assert saved["bridge"]["issue_url"] == "https://github.com/acme-org/api/issues/42"
    assert saved["bridge"]["issues_by_repo"] == {
        "acme-org/api": "https://github.com/acme-org/api/issues/42"
    }

    second = client.post(
        f"/api/plans/{draft_id}/file-issue",
        headers=_auth_headers(state),
    )

    assert second.status_code == 200
    assert second.json()["status"] == "already_filed"
    assert second.json()["issue_url"] == "https://github.com/acme-org/api/issues/42"
    assert len(calls) == 1


def test_compose_draft_requires_intent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/draft",
        json={"text": "   "},
        headers=_auth_headers(state),
    )

    assert response.status_code == 400
    assert not (state / "planning-drafts").exists()


def test_compose_draft_rejects_cross_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/draft",
        json={"text": "title: Cross-origin attempt"},
        headers={"origin": "https://example.invalid"},
    )

    assert response.status_code == 403
    assert not (state / "planning-drafts").exists()


def test_conversation_control_handles_local_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_OPERATOR_SLACK_USER_ID", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    captured: dict[str, object] = {}

    class FakeControlHandler:
        def __init__(self, **kwargs: object) -> None:
            captured["operator_user_id"] = kwargs["operator_user_id"]
            captured["state_root"] = kwargs["state_root"]
            captured["plan_reader"] = kwargs["plan_reader"]

        def handle(self, text: str, *, trusted: bool, actor_user_id: str) -> object:
            captured["text"] = text
            captured["trusted"] = trusted
            captured["actor_user_id"] = actor_user_id
            return SimpleNamespace(
                handled=True,
                action="run",
                text="*Triggered one run* `batman`.",
                detail="",
            )

    monkeypatch.setattr(server_views, "SlackControlHandler", FakeControlHandler)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/conversation/control",
        json={"text": "run batman"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["handled"] is True
    assert payload["action"] == "run"
    assert payload["actor_user_id"] == "ULOCALCLIENT"
    assert captured["operator_user_id"] == "ULOCALCLIENT"
    assert captured["trusted"] is True
    assert captured["state_root"] == state


def test_conversation_control_allows_planning_fallthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_OPERATOR_SLACK_USER_ID", raising=False)
    state = tmp_path / "state"
    state.mkdir()

    class FakeControlHandler:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def handle(self, text: str, *, trusted: bool, actor_user_id: str) -> object:
            assert text == "run tests for the onboarding flow"
            assert trusted is True
            assert actor_user_id == "ULOCALCLIENT"
            return SimpleNamespace(
                handled=False,
                action="not_a_command",
                text="",
                detail="unknown run codename tests",
            )

    monkeypatch.setattr(server_views, "SlackControlHandler", FakeControlHandler)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/conversation/control",
        json={"text": "run tests for the onboarding flow"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert response.json()["detail"] == "unknown run codename tests"


def test_conversation_control_help_prose_falls_through(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/conversation/control",
        json={"text": "help me add onboarding tests"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["handled"] is False
    assert payload["action"] == "not_a_command"


def test_conversation_control_requires_token_and_same_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    missing_token = client.post(
        "/api/conversation/control",
        json={"text": "status"},
        headers={"origin": "http://testserver"},
    )
    assert missing_token.status_code == 403

    foreign_origin = client.post(
        "/api/conversation/control",
        json={"text": "status"},
        headers=_auth_headers(state, origin="https://example.invalid"),
    )
    assert foreign_origin.status_code == 403

    empty = client.post(
        "/api/conversation/control",
        json={"text": "  "},
        headers=_auth_headers(state),
    )
    assert empty.status_code == 400


def test_compose_draft_rejects_foreign_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    # A non-compose id must not let a caller overwrite a Slack/followup draft;
    # it is ignored and a fresh compose draft id is generated instead.
    response = client.post(
        "/api/plans/draft",
        json={"draft_id": "slack-20260529-0400-E1", "text": "title: Sneaky"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["draft_id"].startswith("compose-")


def test_compose_draft_ignores_unknown_compose_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))
    unknown = "compose-20260531-9999-never-seen"

    response = client.post(
        "/api/plans/draft",
        json={"draft_id": unknown, "text": "title: Safe new draft"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_id"].startswith("compose-")
    assert payload["draft_id"] != unknown
    assert not (state / "planning-drafts" / f"{unknown}.json").exists()


# --------------------------------------------------------------------------- #
# Conversational, repo-grounded spec-builder (POST /api/compose/converse)
# --------------------------------------------------------------------------- #


# The spec-interrogator system prompt lives at the repo root; point the runtime
# at this worktree's copy so the endpoint loads it regardless of the operator's
# live workspace path.
_INTERROGATOR_PROMPT = REPO_ROOT / "prompts" / "spec-interrogator.md"


def _use_interrogator_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_SPEC_INTERROGATOR_PROMPT", str(_INTERROGATOR_PROMPT))


def test_compose_interrogator_prompt_prefers_runtime_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_SPEC_INTERROGATOR_PROMPT", raising=False)
    home = tmp_path / "alfred"
    prompt = home / "prompts" / "spec-interrogator.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("runtime prompt\n", encoding="utf-8")
    monkeypatch.setenv("ALFRED_HOME", str(home))

    assert server_views._compose_interrogator_prompt_path() == prompt


def test_compose_interrogator_prompt_falls_back_to_source_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_SPEC_INTERROGATOR_PROMPT", raising=False)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "missing-runtime"))

    assert server_views._compose_interrogator_prompt_path() == _INTERROGATOR_PROMPT


def _stub_converse_turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str,
    draft_overrides: dict | None = None,
    score: int = 30,
    ready: bool = False,
    missing: list[str] | None = None,
    done: bool = False,
    intent: str = "build",
    capture: dict | None = None,
):
    """Patch the LLM dispatch so the endpoint runs without a live model call."""
    import compose_converse as cc

    def fake_run_turn(*, base_draft, messages, repo_grounding, code_map, **_kw):
        if capture is not None:
            capture["base_draft"] = base_draft
            capture["messages"] = list(messages)
            capture["repo_grounding"] = repo_grounding
            capture["code_map"] = code_map
        draft = base_draft
        if draft_overrides:
            from dataclasses import replace

            draft = replace(base_draft, **draft_overrides)
        return cc.ConverseTurn(
            reply=reply,
            draft=draft,
            readiness=cc.ConverseReadiness(score=score, ready=ready, missing=tuple(missing or [])),
            done=done,
            intent=intent,
        )

    monkeypatch.setattr(cc, "run_turn", fake_run_turn)


def test_compose_converse_runs_a_turn_and_persists_a_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    _use_interrogator_prompt(monkeypatch)
    capture: dict = {}
    _stub_converse_turn(
        monkeypatch,
        reply="Which columns should the export include?",
        draft_overrides={
            "title": "Add CSV export",
            "desired_behavior": "A download button exports the visible rows as CSV.",
            "repos": ["acme/frontend"],
        },
        score=45,
        missing=["test plan"],
        capture=capture,
    )

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={
            "repos": ["acme/frontend"],
            "messages": [{"role": "user", "content": "Add a CSV export to attendees"}],
        },
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_id"].startswith("compose-")
    assert payload["reply"] == "Which columns should the export include?"
    assert payload["readiness"] == {
        "score": 45,
        "ready": False,
        "missing": ["test plan"],
    }
    assert payload["done"] is False
    assert payload["intent"] == "build"
    assert payload["draft"]["title"] == "Add CSV export"
    assert payload["draft"]["repos"] == ["acme/frontend"]

    # The user message reached the dispatch as a parsed ConverseMessage.
    assert capture["messages"][0].content == "Add a CSV export to attendees"

    # The conversation + spec is persisted as a compose planning draft, listable
    # via the shared plans API (threads into Plans / the RequestThread).
    saved = Path(payload["saved_path"])
    assert saved.exists()
    assert saved.parent == state / "planning-drafts"
    on_disk = json.loads(saved.read_text(encoding="utf-8"))
    assert on_disk["source"] == "compose"
    assert on_disk["mode"] == "converse"
    # The assistant reply is appended to the stored transcript.
    assert on_disk["conversation"][-1]["role"] == "assistant"
    assert on_disk["conversation"][-1]["content"].startswith("Which columns")

    plans = client.get("/api/plans").json()["rows"]
    assert any(row["plan_id"] == payload["draft_id"] for row in plans)


def test_compose_converse_conversation_intent_flows_to_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversational turn ("who are you?") returns intent=conversation.

    The client uses this to render a plain chat reply instead of a plan card,
    so a question never gets a forced "Needs detail" planning form.
    """
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    _use_interrogator_prompt(monkeypatch)
    _stub_converse_turn(
        monkeypatch,
        reply="I'm Alfred. I turn an outcome into a planned, reviewed change.",
        intent="conversation",
        score=0,
    )

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Who are you?"}]},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "conversation"
    assert payload["reply"].startswith("I'm Alfred")


def test_compose_converse_defaults_single_setup_repo_as_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.setup as setup_mod

    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    _use_interrogator_prompt(monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    setup_mod.persist_selected_repos(["acme/frontend"])
    capture: dict = {}
    _stub_converse_turn(
        monkeypatch,
        reply="I saved the scope from your setup.",
        draft_overrides={"title": "Fix login copy"},
        capture=capture,
    )

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Fix the login copy"}]},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    payload = response.json()
    assert capture["base_draft"].repos == ["acme/frontend"]
    assert payload["draft"]["repos"] == ["acme/frontend"]


def test_compose_converse_iterates_on_same_draft_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    _use_interrogator_prompt(monkeypatch)
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    _stub_converse_turn(
        monkeypatch,
        reply="Which repo is this in?",
        draft_overrides={"title": "Export attendees"},
        score=20,
    )
    first = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Export attendees to CSV"}]},
        headers=_auth_headers(state),
    ).json()
    draft_id = first["draft_id"]

    capture: dict = {}
    _stub_converse_turn(
        monkeypatch,
        reply="Got it.",
        draft_overrides={"repos": ["acme/frontend"]},
        score=70,
        capture=capture,
    )
    second = client.post(
        "/api/compose/converse",
        json={
            "draft_id": draft_id,
            "messages": [
                {"role": "user", "content": "Export attendees to CSV"},
                {"role": "assistant", "content": "Which repo is this in?"},
                {"role": "user", "content": "It is the frontend"},
            ],
        },
        headers=_auth_headers(state),
    ).json()

    # Same draft id is refined in place, not duplicated.
    assert second["draft_id"] == draft_id
    # The prior turn's title was carried forward into this turn's base draft.
    assert capture["base_draft"].title == "Export attendees"
    drafts = client.get("/api/plans/drafts").json()["rows"]
    assert sum(1 for row in drafts if row["draft_id"] == draft_id) == 1


def _capture_intake_guidance(monkeypatch: pytest.MonkeyPatch, capture: dict) -> None:
    """Patch run_turn to record the intake_guidance the endpoint passed in.

    The guidance text is the observable signal that the plain/technical persona
    was selected for this turn (compose_converse.intake_guidance_for renders a
    different one-liner per profile).
    """
    import compose_converse as cc

    def fake_run_turn(*, base_draft, intake_guidance, **_kw):
        capture["intake_guidance"] = intake_guidance
        return cc.ConverseTurn(
            reply="ok",
            draft=base_draft,
            readiness=cc.ConverseReadiness(score=10, ready=False, missing=()),
            done=False,
        )

    monkeypatch.setattr(cc, "run_turn", fake_run_turn)


def test_compose_converse_plain_param_forces_plain_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Server env default is technical, but the per-request plain flag must win.
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    monkeypatch.delenv("ALFRED_INTAKE_PROFILE", raising=False)
    _use_interrogator_prompt(monkeypatch)
    capture: dict = {}
    _capture_intake_guidance(monkeypatch, capture)

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={
            "plain": True,
            "messages": [{"role": "user", "content": "Add a download button"}],
        },
        headers=_auth_headers(state),
    )
    assert response.status_code == 200
    assert "Plain mode is on" in capture["intake_guidance"]


def test_compose_converse_plain_false_forces_technical_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with the server env set to plain, plain:false in the body wins.
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    monkeypatch.setenv("ALFRED_INTAKE_PROFILE", "plain")
    _use_interrogator_prompt(monkeypatch)
    capture: dict = {}
    _capture_intake_guidance(monkeypatch, capture)

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={
            "plain": False,
            "messages": [{"role": "user", "content": "Add a download button"}],
        },
        headers=_auth_headers(state),
    )
    assert response.status_code == 200
    assert "Technical mode" in capture["intake_guidance"]


def test_compose_converse_absent_plain_falls_back_to_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No plain flag in the body: the ALFRED_INTAKE_PROFILE env is the default.
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    monkeypatch.setenv("ALFRED_INTAKE_PROFILE", "plain")
    _use_interrogator_prompt(monkeypatch)
    capture: dict = {}
    _capture_intake_guidance(monkeypatch, capture)

    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Add a download button"}]},
        headers=_auth_headers(state),
    )
    assert response.status_code == 200
    assert "Plain mode is on" in capture["intake_guidance"]


def test_compose_converse_degrades_when_no_engine_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No engine env: the live session is unavailable, so the endpoint returns a
    # clear 503 (the client falls back to the one-shot form) instead of faking.
    monkeypatch.delenv("ALFRED_COMPOSE_CONVERSE_ENGINE", raising=False)
    monkeypatch.delenv("ALFRED_PLANNING_ASSISTANT_ENGINE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Build something"}]},
        headers=_auth_headers(state),
    )
    assert response.status_code == 503
    assert response.json()["error"] == "live_session_unavailable"


def test_compose_converse_degrades_when_engine_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    _use_interrogator_prompt(monkeypatch)
    import compose_converse as cc

    monkeypatch.setattr(cc, "run_turn", lambda **_kw: None)
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "Build something"}]},
        headers=_auth_headers(state),
    )
    assert response.status_code == 503
    assert response.json()["error"] == "live_session_unavailable"


def test_compose_converse_requires_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALFRED_COMPOSE_CONVERSE_ENGINE", "claude")
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": []},
        headers=_auth_headers(state),
    )
    assert response.status_code == 400


def test_compose_converse_rejects_cross_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={
            server_views.SERVER_TOKEN_HEADER: _server_token(state),
            "origin": "http://evil.example",
        },
    )
    assert response.status_code == 403


def test_compose_converse_requires_the_server_token(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/compose/converse",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"origin": "http://testserver"},  # same-origin but no token
    )
    assert response.status_code == 403


def test_api_queue_arm_requires_the_server_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-origin POST without the per-launch token cannot arm pickup."""
    import issue_queue as iq

    state = tmp_path / "state"
    state.mkdir()
    calls: list[tuple[str, int, bool]] = []

    def fake_set_issue_pickup(repo: str, number: int, *, hold: bool):
        calls.append((repo, number, hold))
        return True, "unexpected mutation"

    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    # Same-origin but no X-Alfred-Token header: the CSRF gate rejects it before
    # any label mutation can happen, so a drive-by localhost page cannot arm.
    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "queue"},
        headers={"origin": "http://testserver"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert calls == []

    # A wrong token is rejected the same way.
    bad_token = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "queue"},
        headers={
            "origin": "http://testserver",
            server_views.SERVER_TOKEN_HEADER: "nope",
        },
    )
    assert bad_token.status_code == 403
    assert calls == []


def test_api_queue_arm_allowed_with_the_server_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the operator's per-launch token, arming pickup is allowed."""
    import issue_queue as iq

    state = tmp_path / "state"
    state.mkdir()
    calls: list[tuple[str, int, bool]] = []

    def fake_set_issue_pickup(repo: str, number: int, *, hold: bool):
        calls.append((repo, number, hold))
        return True, f"{repo}#{number} queued"

    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "queue"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["action"] == "queue"
    assert calls == [("org/repo", 7, False)]


def test_api_queue_allows_hold_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import issue_queue as iq

    state = tmp_path / "state"
    state.mkdir()
    calls: list[tuple[str, int, bool]] = []

    def fake_set_issue_pickup(repo: str, number: int, *, hold: bool):
        calls.append((repo, number, hold))
        return True, f"{repo}#{number} held"

    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "hold"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["action"] == "hold"
    assert response.json()["detail"] == "org/repo#7 held"
    assert calls == [("org/repo", 7, True)]


def test_api_queue_allows_assign_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import issue_assignment

    state = tmp_path / "state"
    state.mkdir()
    calls: list[tuple[str, int, str]] = []

    def fake_assign_issue(repo: str, number: int, *, target_agent: str = ""):
        calls.append((repo, number, target_agent))
        return SimpleNamespace(
            ok=True,
            decision=SimpleNamespace(agent="lucius"),
            detail=f"{repo}#{number} assigned to Lucius",
            error="",
        )

    monkeypatch.setattr(issue_assignment, "assign_issue", fake_assign_issue)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "assign"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["action"] == "assign"
    assert response.json()["target_agent"] == "lucius"
    assert response.json()["detail"] == "org/repo#7 assigned to Lucius"
    assert calls == [("org/repo", 7, "")]


def test_api_queue_assign_accepts_target_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import issue_assignment

    state = tmp_path / "state"
    state.mkdir()
    calls: list[tuple[str, int, str]] = []

    def fake_assign_issue(repo: str, number: int, *, target_agent: str = ""):
        calls.append((repo, number, target_agent))
        return SimpleNamespace(
            ok=True,
            decision=SimpleNamespace(agent="batman"),
            detail=f"{repo}#{number} assigned to Batman",
            error="",
        )

    monkeypatch.setattr(issue_assignment, "assign_issue", fake_assign_issue)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={
            "repo": "org/repo",
            "number": 7,
            "action": "assign",
            "target_agent": "batman",
        },
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["target_agent"] == "batman"
    assert response.json()["detail"] == "org/repo#7 assigned to Batman"
    assert calls == [("org/repo", 7, "batman")]


def test_api_queue_allows_done_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Done action closes the issue via close_issue (native closed state)."""
    import issue_queue as iq

    state = tmp_path / "state"
    state.mkdir()
    closed: list[tuple[str, int]] = []
    pickup_calls: list[tuple[str, int, bool]] = []

    def fake_close_issue(repo: str, number: int):
        closed.append((repo, number))
        return True, f"{repo}#{number} closed (marked done)"

    def fake_set_issue_pickup(repo: str, number: int, *, hold: bool):
        pickup_calls.append((repo, number, hold))
        return True, "unexpected pickup mutation"

    monkeypatch.setattr(iq, "close_issue", fake_close_issue)
    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "done"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 200
    assert response.json()["action"] == "done"
    assert response.json()["detail"] == "org/repo#7 closed (marked done)"
    assert closed == [("org/repo", 7)]
    # Done must not touch the pickup labels.
    assert pickup_calls == []


def test_api_queue_done_requires_the_server_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Done closes a real issue, so it requires the per-launch token too."""
    import issue_queue as iq

    state = tmp_path / "state"
    state.mkdir()
    closed: list[tuple[str, int]] = []

    def fake_close_issue(repo: str, number: int):
        closed.append((repo, number))
        return True, "unexpected close"

    monkeypatch.setattr(iq, "close_issue", fake_close_issue)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "done"},
        headers={"origin": "http://testserver"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert closed == []


def test_api_queue_rejects_unknown_action(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/queue",
        json={"repo": "org/repo", "number": 7, "action": "delete"},
        headers=_auth_headers(state),
    )

    assert response.status_code == 400
    assert "queue" in response.json()["error"]


def test_create_app_writes_per_launch_token_with_owner_only_perms(
    tmp_path: Path,
) -> None:
    import stat

    state = tmp_path / "state"
    create_app(FilesystemReader(state_root=state))

    token_path = state / "server-token"
    assert token_path.is_file()
    assert token_path.read_text(encoding="utf-8").strip()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_mutating_post_requires_token_and_is_allowed_with_it(
    tmp_path: Path,
) -> None:
    """The canonical CSRF gate: a mutating POST is 403 without the token,
    allowed with it, and a fresh per-launch token rotates on each app start."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "planning-drafts").mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    body = {"text": "title: Token-gated compose draft"}

    # Same-origin but no token -> 403.
    missing = client.post(
        "/api/plans/draft",
        json=body,
        headers={"origin": "http://testserver"},
    )
    assert missing.status_code == 403
    assert missing.json()["error"] == "forbidden"

    # Correct token -> allowed.
    allowed = client.post("/api/plans/draft", json=body, headers=_auth_headers(state))
    assert allowed.status_code == 200

    # The token is also rejected when the request is cross-origin: the
    # same-origin layer stays in force alongside the token gate.
    cross_origin = client.post(
        "/api/plans/draft",
        json=body,
        headers={
            "origin": "https://example.invalid",
            server_views.SERVER_TOKEN_HEADER: _server_token(state),
        },
    )
    assert cross_origin.status_code == 403


def test_authorized_mutation_fails_closed_without_token_file(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    state = tmp_path / "state"
    state.mkdir()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(reader=SimpleNamespace(state_root=state))),
        headers={server_views.SERVER_TOKEN_HEADER: "anything"},
    )

    # No server-token on disk: the gate must deny rather than downgrade to
    # same-origin-only.
    assert server_views._authorized_mutation(request) is False


def test_api_slack_trusted_users_adds_and_removes_local_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UENV1")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    listed = client.get("/api/slack/trusted-users")

    assert listed.status_code == 200
    assert listed.json()["operator_user_id"] == "UOPERATOR"
    assert any(user["user_id"] == "UENV1" for user in listed.json()["users"])

    added = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "<@ULOCAL1>"},
        headers=_auth_headers(state),
    )

    assert added.status_code == 200
    assert added.json()["added"] is True
    local = [user for user in added.json()["users"] if user["user_id"] == "ULOCAL1"]
    assert local
    assert local[0]["can_remove"] is True

    removed = client.post(
        "/api/slack/trusted-users/ULOCAL1/remove",
        headers=_auth_headers(state),
    )

    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert all(user["user_id"] != "ULOCAL1" for user in removed.json()["users"])


def test_api_slack_trusted_users_rejects_cross_origin_and_bad_ids(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    bad_origin = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "ULOCAL1"},
        headers={"origin": "https://example.invalid"},
    )

    assert bad_origin.status_code == 403

    bad_id = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "../../nope"},
        headers=_auth_headers(state),
    )

    assert bad_id.status_code == 400
    assert not (state / "slack-trust").exists()


def _write_spend(state: Path, codename: str, day: str, **fields: object) -> None:
    agent_dir = state / codename
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / f"spend-{day}.json").write_text(json.dumps(fields), encoding="utf-8")


def test_api_status_rolls_up_todays_cost(tmp_path: Path) -> None:
    state = tmp_path / "state"
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Two agents fired today; one yesterday's ledger must be excluded.
    _write_spend(
        state,
        "lucius",
        today,
        firings_today=3,
        successes_today=2,
        failures_today=1,
        cost_usd_today=1.25,
    )
    _write_spend(
        state,
        "bane",
        today,
        firings_today=1,
        successes_today=1,
        failures_today=0,
        cost_usd_today=0.50,
    )
    _write_spend(
        state,
        "lucius",
        "2020-01-01",
        firings_today=99,
        cost_usd_today=99.0,
    )

    client = TestClient(create_app(FilesystemReader(state_root=state)))
    metrics = client.get("/api/status").json()["metrics"]

    assert metrics["spend_usd"] == 1.75
    assert metrics["firings"] == 4
    assert metrics["successes"] == 3
    assert metrics["failures"] == 1
    assert metrics["agents_with_spend"] == 2


def test_api_status_cost_rollup_is_null_when_no_spend(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))
    metrics = client.get("/api/status").json()["metrics"]

    # No ledgers today: spend is null (not 0) so the client can show an honest
    # "not surfaced" instead of fabricating a zero-dollar day.
    assert metrics["spend_usd"] is None
    assert metrics["firings"] == 0
    assert metrics["agents_with_spend"] == 0


def test_api_status_reports_intake_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    monkeypatch.delenv("ALFRED_INTAKE_PROFILE", raising=False)
    assert client.get("/api/status").json()["intake_profile"] == "technical"

    monkeypatch.setenv("ALFRED_INTAKE_PROFILE", "plain")
    assert client.get("/api/status").json()["intake_profile"] == "plain"

    # A typo never silently downgrades into plain mode.
    monkeypatch.setenv("ALFRED_INTAKE_PROFILE", "PLAINish")
    assert client.get("/api/status").json()["intake_profile"] == "technical"


def test_api_schedule_returns_cron_and_interval_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    repo = tmp_path / "repo"
    conf = repo / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "# Per-agent launchd config.\n"
        "alfred.lucius\tlucius.py\tinterval:600\tyes\t\topus\tSingle-repo engineer\n"
        "alfred.batman\tbatman.py\tinterval:3600\tno\talfred.batman\tcross-repo architect\n"
        "alfred.bane\tbane.py\tcron:2:00\tyes\t\topus\tDaily test author\n"
        "alfred.cold-backup\talfred-cold-backup.py\tcron:0:2:00\tno\t\t\tWeekly cold backup\n"
        "# alfred.huntress\thuntress.py\tinterval:5400\tyes\t\topus\tDisabled\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_REPO", str(repo))

    client = TestClient(create_app(FilesystemReader(state_root=state)))
    runs = client.get("/api/schedule").json()["runs"]
    by_codename = {run["codename"]: run for run in runs}

    # The commented-out Huntress row is not surfaced.
    assert set(by_codename) == {"lucius", "batman", "bane", "cold-backup"}

    # interval rows carry a cadence string but no guessed next-fire timestamp.
    assert by_codename["lucius"]["kind"] == "interval"
    assert by_codename["lucius"]["cadence"] == "every 10m"
    assert by_codename["lucius"]["next_fire_at"] is None
    assert by_codename["lucius"]["role"] == "Single-repo engineer"
    assert by_codename["batman"]["role"] == "cross-repo architect"

    # cron rows compute a concrete next-fire.
    assert by_codename["bane"]["kind"] == "cron-daily"
    assert by_codename["bane"]["cadence"] == "daily 02:00"
    assert by_codename["bane"]["next_fire_at"] is not None

    assert by_codename["cold-backup"]["kind"] == "cron-weekly"
    assert by_codename["cold-backup"]["cadence"] == "Sunday 02:00"


def test_server_agents_conf_path_prefers_checkout_before_default_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.schedule as schedule_mod

    checkout = tmp_path / "checkout"
    (checkout / "clients").mkdir(parents=True)
    (checkout / "bin").mkdir()
    (checkout / "bin" / "alfred").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (checkout / "lib" / "server").mkdir(parents=True)
    checkout_conf = checkout / "launchd" / "agents.conf"
    checkout_conf.parent.mkdir()
    checkout_conf.write_text(
        "alfred.batman\tbatman.py\tinterval:3600\tno\talfred.batman\tarchitect\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    home_conf = home / ".alfred" / "launchd" / "agents.conf"
    home_conf.parent.mkdir(parents=True)
    home_conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tno\talfred.lucius\tengineer\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.chdir(checkout / "clients")

    assert schedule_mod.agents_conf_path() == checkout_conf


def test_server_agents_conf_path_uses_default_home_when_no_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.schedule as schedule_mod

    home = tmp_path / "home"
    home_conf = home / ".alfred" / "launchd" / "agents.conf"
    home_conf.parent.mkdir(parents=True)
    home_conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tno\talfred.lucius\tengineer\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.chdir(tmp_path)

    assert schedule_mod.agents_conf_path() == home_conf


def test_state_root_for_conf_handles_runtime_layouts(tmp_path: Path) -> None:
    import server.schedule as schedule_mod

    home = tmp_path / "alfred-home"

    assert schedule_mod._state_root_for_conf(home / "launchd" / "agents.conf") == home / "state"
    assert (
        schedule_mod._state_root_for_conf(home / "infra" / "agents" / "launchd" / "agents.conf")
        == home / "state"
    )
    assert (
        schedule_mod._state_root_for_conf(home / "agents" / "launchd" / "agents.conf")
        == home / "state"
    )


def test_scheduled_codenames_is_not_limited_by_schedule_endpoint_cap(tmp_path: Path) -> None:
    import server.schedule as schedule_mod

    conf = tmp_path / "launchd" / "agents.conf"
    conf.parent.mkdir()
    conf.write_text(
        "\n".join(
            f"alfred.worker-{index:04d}\tworker.py\tinterval:600\tno\t"
            f"alfred.worker-{index:04d}\tworker"
            for index in range(1005)
        )
        + "\n",
        encoding="utf-8",
    )

    assert len(schedule_mod.scheduled_codenames(conf_path=conf)) == 1005


def test_api_schedule_reads_deployed_runtime_conf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    home = tmp_path / "alfred-home"
    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tSingle-repo engineer\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    client = TestClient(create_app(FilesystemReader(state_root=state)))
    runs = client.get("/api/schedule").json()["runs"]

    assert [run["codename"] for run in runs] == ["lucius"]
    assert runs[0]["cadence"] == "every 20m"


def test_api_schedule_accepts_existing_dot_suffixed_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    repo = tmp_path / "repo"
    conf = repo / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "my.fleet.lucius\tlucius.py\tinterval:600\tyes\t\topus\tSingle-repo engineer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_REPO", str(repo))

    client = TestClient(create_app(FilesystemReader(state_root=state)))
    runs = client.get("/api/schedule").json()["runs"]

    assert [run["codename"] for run in runs] == ["lucius"]
    assert runs[0]["cadence"] == "every 10m"


def test_api_schedule_empty_when_conf_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("ALFRED_REPO", str(tmp_path / "nonexistent-repo"))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    body = client.get("/api/schedule").json()
    assert body["runs"] == []


def test_custom_agents_appear_in_status_and_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_agents import CustomAgentStore

    state = tmp_path / "state"
    monkeypatch.setenv("ALFRED_REPO", str(tmp_path / "missing-repo"))
    CustomAgentStore.from_state_root(state).upsert(
        {
            "codename": "release-captain",
            "display_name": "Release Captain",
            "role_title": "Release coordinator",
            "purpose": "Checks release readiness before handoff.",
            "prompt": "Review release readiness and summarize blockers for the operator.",
            "engine": "hybrid",
            "schedule": "30m",
            "repos": ["acme/api"],
        }
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    agents = client.get("/api/status").json()["agents"]
    assert agents[0]["codename"] == "release-captain"
    assert agents[0]["display_name"] == "Release Captain"
    assert agents[0]["role_title"] == "Release coordinator"
    assert agents[0]["last_summary"] == "no firings yet"

    runs = client.get("/api/schedule").json()["runs"]
    assert [run["codename"] for run in runs] == ["release-captain"]
    assert runs[0]["cadence"] == "every 30m"
    assert runs[0]["display_name"] == "Release Captain"


def test_custom_agent_schedule_rows_skip_base_conf_codename_collisions(
    tmp_path: Path,
) -> None:
    import server.schedule as schedule_mod

    home = tmp_path / "alfred-home"
    state = home / "state"
    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "my.fleet.release-captain\tlucius.py\tinterval:600\tno\t\tFeature dev\n",
        encoding="utf-8",
    )
    manifest = state / "custom-agents" / "custom-agents.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness before handoff.",
                        "prompt": "Review release readiness and summarize blockers for the operator.",
                        "engine": "hybrid",
                        "schedule": "interval:1800",
                        "repos": ["acme/api"],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    runs = schedule_mod.upcoming_runs(conf_path=conf, state_root=state)

    assert [run.codename for run in runs] == ["release-captain"]
    assert runs[0].cadence == "every 10m"
    assert schedule_mod.scheduled_codenames(conf_path=conf, state_root=state) == ["release-captain"]


def test_draft_from_payload_filters_invalid_repo_slugs() -> None:
    # The one-shot draft loader must route repos through the same slug gate the
    # converse path uses, so a dot-traversal slug is never persisted into a
    # stored draft where a later consumer could resolve it to a workspace path.
    draft = server_views._draft_from_payload(
        {
            "title": "Add CSV export",
            "repos": ["acme/frontend", "acme/..", "../acme", "acme/frontend"],
        }
    )
    assert draft.repos == ["acme/frontend"]


# ---------------------------------------------------------------------------
# Plan go/no-go decisions: the in-app approve/decline writes the same marker
# Batman's file-poll fallback watches, and a decided plan reflects its state.
# ---------------------------------------------------------------------------


def _write_batman_plan(tmp_path: Path, issue_num: int, *, title: str) -> Path:
    """Save a Batman plan exactly where ``draft_plan`` writes one.

    ``batman-plans`` is the sibling of the reader's ``state`` root, so it lands
    at ``tmp_path/batman-plans/{issue_num}-plan.md`` (state_root.parent).
    """
    plans = tmp_path / "batman-plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{issue_num}-plan.md"
    path.write_text(
        f"# Batman Plan for Issue #{issue_num}\n\n"
        f"**Title:** {title}\n\n"
        "**Status:** Draft (awaiting approval)\n\n"
        f"**Issue URL:** https://github.com/example-org/alfred/issues/{issue_num}\n\n"
        "**Affected Repos:** backend, frontend\n",
        encoding="utf-8",
    )
    return path


def test_plan_decision_approve_writes_batman_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/13-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "approve"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "approve"
    assert body["issue_number"] == 13
    assert body["status"] == "approved"
    # The marker lands exactly where Batman's file poll watches it:
    # $ALFRED_HOME/batman/approvals/{issue_num}.approved (state_root.parent).
    approved = tmp_path / "batman" / "approvals" / "13.approved"
    rejected = tmp_path / "batman" / "approvals" / "13.rejected"
    record = tmp_path / "batman" / "approval-decisions" / "13.json"
    assert approved.exists()
    assert not rejected.exists()
    assert record.exists()
    assert str(approved) == body["marker_path"]


def test_plan_decision_decline_writes_rejected_marker_with_reason(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 21, title="Risky migration")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/21-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "decline", "reason": "scope too broad"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    rejected = tmp_path / "batman" / "approvals" / "21.rejected"
    assert rejected.exists()
    # Batman reads the reject body as a short detail string; ours carries the
    # source and the operator reason.
    contents = rejected.read_text(encoding="utf-8")
    assert "declined via Alfred client" in contents
    assert "scope too broad" in contents


def test_plan_decision_flip_clears_contradicting_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 7, title="Flip me")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    client.post(
        "/api/plans/7-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "approve"},
    )
    client.post(
        "/api/plans/7-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "decline"},
    )

    approvals = tmp_path / "batman" / "approvals"
    assert not (approvals / "7.approved").exists()
    assert (approvals / "7.rejected").exists()


def test_plan_decision_requires_token(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    # Same-origin but no token: the synchronizer-token gate must reject it.
    response = client.post(
        "/api/plans/13-plan/decision",
        headers={"origin": "http://testserver"},
        json={"decision": "approve"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    assert not (tmp_path / "batman" / "approvals" / "13.approved").exists()


def test_plan_decision_rejects_cross_origin_posts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/13-plan/decision",
        headers=_auth_headers(state, origin="https://example.invalid"),
        json={"decision": "approve"},
    )

    assert response.status_code == 403
    assert not (tmp_path / "batman" / "approvals" / "13.approved").exists()


def test_plan_decision_rejects_invalid_decision(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/13-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "maybe"},
    )

    assert response.status_code == 400
    assert not (tmp_path / "batman" / "approvals").exists()


def test_plan_decision_refuses_non_batman_plan(tmp_path: Path) -> None:
    # A Slack follow-up is not a go/no-go plan: it has no issue marker Batman
    # would ever poll, so the decision endpoint must refuse it.
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    (followups / "slack-C1-1716480000.000000.md").write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [example-org/alfred#120](https://github.com/example-org/alfred/issues/120)\n",
        encoding="utf-8",
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.post(
        "/api/plans/slack-C1-1716480000.000000/decision",
        headers=_auth_headers(state),
        json={"decision": "approve"},
    )

    assert response.status_code == 400


def test_decided_batman_plan_reflects_status_and_leaves_queue(tmp_path: Path) -> None:
    # A genuine Batman go/no-go plan reads as "draft" until decided. Once the
    # marker exists, the reader reflects approved/declined so the client's
    # Needs-you filter (source==batman + waiting status) drops it.
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    reader = FilesystemReader(state_root=state)

    before = reader.get_plan("13-plan")
    assert before is not None
    assert before.source == "batman"
    assert "draft" in before.status.lower()

    client = TestClient(create_app(reader))
    client.post(
        "/api/plans/13-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "approve"},
    )

    after = reader.get_plan("13-plan")
    assert after is not None
    assert after.status == "approved"
    # And in the list feed the client polls:
    rows = client.get("/api/plans").json()["rows"]
    decided = next(row for row in rows if row["plan_id"] == "13-plan")
    assert decided["status"] == "approved"


def test_decided_batman_plan_stays_decided_after_marker_is_consumed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    _write_batman_plan(tmp_path, 13, title="Add CSV export")
    reader = FilesystemReader(state_root=state)
    client = TestClient(create_app(reader))

    response = client.post(
        "/api/plans/13-plan/decision",
        headers=_auth_headers(state),
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    marker = tmp_path / "batman" / "approvals" / "13.approved"
    assert marker.exists()

    marker.unlink()

    after = reader.get_plan("13-plan")
    assert after is not None
    assert after.status == "approved"


# Each failure-path test raises with its own sentinel string. If a sentinel
# ever shows up in an HTTP response body, that handler is leaking the exception
# message to the client (py/stack-trace-exposure). A per-route sentinel proves
# the route's *own* detail is suppressed, so a leak on one route cannot be
# masked by a different route's generic body.
def _exc_sentinel(route: str) -> str:
    return f"sentinel-leak-canary-{route}-/private/state/token-9f8a"


def _assert_no_exc_leak(payload: object, marker: str) -> None:
    blob = json.dumps(payload)
    # 1. The exception's own message text (the per-route sentinel the _boom
    #    raised) must never reach the client. This proves the *detail* text is
    #    gone, not merely the word "RuntimeError".
    assert marker not in blob
    # 2. No traceback framing may leak (py/stack-trace-exposure).
    assert "Traceback" not in blob
    assert 'File "' not in blob
    # 3. No exception class name of any kind ("RuntimeError", "ValueError",
    #    "OSError", ...). Broadened from the old RuntimeError-only check so a
    #    future _boom raising a different class is still covered.
    assert not re.search(r"\b\w+(?:Error|Exception)\b", blob), blob


def test_api_shipped_board_failure_returns_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    marker = _exc_sentinel("shipped")

    def _boom(*_a: object, **_kw: object) -> dict[str, object]:
        raise RuntimeError(marker)

    import shipped_board

    monkeypatch.setattr(shipped_board, "build_board", _boom)
    monkeypatch.setattr(shipped_board, "resolve_repos", lambda *_a, **_kw: [])
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/shipped")
    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "internal error"
    assert body["columns"] == {"queued": [], "in_progress": [], "shipped": []}
    _assert_no_exc_leak(body, marker)


def test_api_schedule_failure_returns_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    marker = _exc_sentinel("schedule")

    def _boom(*_a: object, **_kw: object) -> list[object]:
        raise RuntimeError(marker)

    import server.schedule as schedule_mod

    monkeypatch.setattr(schedule_mod, "upcoming_runs", _boom)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/schedule")
    assert response.status_code == 200
    body = response.json()
    assert body["runs"] == []
    assert body["error"] == "internal error"
    _assert_no_exc_leak(body, marker)


def test_api_memory_candidates_failure_returns_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    marker = _exc_sentinel("memory-candidates")

    class ExplodingBrain:
        def health(self) -> dict[str, bool]:
            return {"ok": True}

        def list_memory_candidates(self, **_kw: object) -> list[object]:
            raise RuntimeError(marker)

    monkeypatch.setattr(
        server_views,
        "_memory_brain",
        lambda *_a, **_kw: (ExplodingBrain(), None),
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/memory/candidates")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["error"] == "internal error"
    _assert_no_exc_leak(body, marker)


def test_api_memory_brain_unavailable_returns_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead fleet brain must not surface its exception text to the client."""
    state = tmp_path / "state"
    marker = _exc_sentinel("memory-brain-unavailable")

    import fleet_brain

    def _boom(*_a: object, **_kw: object) -> object:
        raise RuntimeError(marker)

    monkeypatch.setattr(fleet_brain.FleetBrain, "from_env", staticmethod(_boom))
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/memory/candidates")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["error"] == "internal error"
    _assert_no_exc_leak(body, marker)


def test_api_memory_routes_work_with_empty_real_fleet_brain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean OSS install has an empty memory DB, not a broken memory panel."""
    state = tmp_path / "state"
    home = tmp_path / "alfred-home"
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_FLEET_BRAIN_DB", raising=False)
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    candidates = client.get("/api/memory/candidates")
    lessons = client.get("/api/memory/lessons")

    assert candidates.status_code == 200
    assert candidates.json() == {"rows": []}
    assert lessons.status_code == 200
    assert lessons.json() == {"rows": []}


def test_plain_compose_title_redos_input_is_bounded() -> None:
    """A whitespace-padded compose request must not hang the title heuristic.

    Regression guard for py/polynomial-redos: the title regexes used to run on
    raw request text with unbounded ``\\s+`` quantifiers. The handler now
    collapses whitespace first, so even a pathological run of spaces parses in
    linear time and yields a bounded title.
    """
    import time

    hostile = "the " + " " * 5000 + "x is hard to scan at small window sizes"
    start = time.perf_counter()
    title = server_views._plain_compose_title(hostile)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0
    assert len(title) <= 93
    assert isinstance(title, str) and title


def test_plain_compose_title_repeated_the_prefix_is_bounded() -> None:
    """Repeated ``the ...`` prefixes must not drive quadratic scan-title time.

    Regression guard for the second py/polynomial-redos shape: the title scan
    used ``\\bthe (.+?) ... sizes`` which retried ``\\bthe`` at every "the"
    occurrence while the lazy ``(.+?)`` rescanned the remainder when the final
    word failed. A body of tens of thousands of "the " tokens that never reaches
    the suffix could tie up ``POST /api/plans/draft``. The scan is now a single
    linear ``str.find`` pass, so this parses in well under the bound.
    """
    import time

    hostile = ("the " * 5000) + "x"
    start = time.perf_counter()
    title = server_views._plain_compose_title(hostile)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5
    assert isinstance(title, str) and title
    assert len(title) <= 93


def test_plain_compose_title_scan_subject_unchanged() -> None:
    """The linear scan-title rewrite must yield the same titles as before.

    Confirms the ReDoS fix did not change the heuristic's output for the normal
    "the SUBJECT is hard to scan at small window sizes" phrasing, including the
    earliest-``the`` selection the old lazy regex used.
    """
    assert (
        server_views._plain_compose_title(
            "The review queue is hard to scan at small window sizes. Make it usable."
        )
        == "Make review queue usable at small sizes"
    )
    # Earliest "the" wins, matching the old left-to-right ``\bthe`` anchor.
    assert (
        server_views._scan_title_subject("the foo the bar is hard to scan at small window sizes")
        == "foo the bar"
    )
    # "breathe" is not a "the" word boundary; the real subject is still found.
    assert (
        server_views._scan_title_subject(
            "we breathe the dashboard is hard to scan at small window sizes"
        )
        == "dashboard"
    )
    # A run-on after "sizes" (no trailing word boundary) is not a match.
    assert (
        server_views._scan_title_subject("the queue is hard to scan at small window sizesxyz") == ""
    )


# --------------------------------------------------------------------------
# Roster theme persistence (GET/POST /api/roster-theme)
# --------------------------------------------------------------------------


def test_roster_theme_defaults_to_batman(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.get("/api/roster-theme")
    assert resp.status_code == 200
    body = resp.json()
    assert body["theme"] == "batman"
    assert body["custom_names"] == {}
    assert body["custom_roles"] == {}


def test_roster_theme_set_preset_round_trips(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "justice-league"},
    )
    assert resp.status_code == 200
    assert resp.json()["theme"] == "justice-league"

    # A fresh GET sees the persisted choice, proving it hit the state dir.
    again = client.get("/api/roster-theme")
    assert again.json()["theme"] == "justice-league"


def test_roster_theme_set_custom_names_and_roles_persist(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={
            "theme": "custom",
            "custom_names": {"batman": "Sherlock", "fleet-doctor": "Watson"},
            "custom_roles": {"batman": "Lead detective"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["theme"] == "custom"
    assert body["custom_names"] == {"batman": "Sherlock", "fleet-doctor": "Watson"}
    assert body["custom_roles"] == {"batman": "Lead detective"}

    # The file under the state root is the single source of truth across surfaces.
    stored = json.loads((state / "roster-theme" / "roster-theme.json").read_text(encoding="utf-8"))
    assert stored["theme"] == "custom"
    assert stored["custom_names"]["batman"] == "Sherlock"


def test_roster_theme_switch_to_preset_retains_custom_roster(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "custom", "custom_names": {"batman": "Sherlock"}},
    )
    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "transformers"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["theme"] == "transformers"
    # Switching to a preset does NOT delete the authored roster: it is retained so
    # a later switch back to custom (or a restart) restores it.
    assert body["custom_names"] == {"batman": "Sherlock"}

    # Switching back to custom with no payload restores the authored names.
    back = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "custom"},
    )
    assert back.json()["custom_names"] == {"batman": "Sherlock"}


def test_roster_theme_rejects_unknown_theme(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "not-a-real-theme"},
    )
    assert resp.status_code == 400
    # The error is generic: the rejected (attacker-controlled) value must never
    # be echoed back in the response body (CodeQL information-exposure guard).
    assert "not-a-real-theme" not in resp.text
    # The persisted default is untouched by a rejected write.
    assert client.get("/api/roster-theme").json()["theme"] == "batman"


def test_roster_theme_rejects_bad_custom_name_payload(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    # A key that is not a fleet codename is rejected.
    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "custom", "custom_names": {"Not A Codename!": "X"}},
    )
    assert resp.status_code == 400

    # An empty label is rejected.
    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        json={"theme": "custom", "custom_names": {"batman": "   "}},
    )
    assert resp.status_code == 400


def test_roster_theme_post_requires_token_and_same_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    # No token, no same-origin header.
    resp = client.post("/api/roster-theme", json={"theme": "justice-league"})
    assert resp.status_code == 403

    # Cross-origin even with a valid token is refused.
    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state, origin="http://evil.example"),
        json={"theme": "justice-league"},
    )
    assert resp.status_code == 403
    assert client.get("/api/roster-theme").json()["theme"] == "batman"


def test_roster_theme_rejects_non_object_body(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.post(
        "/api/roster-theme",
        headers=_auth_headers(state),
        content="[1, 2, 3]",
    )
    assert resp.status_code == 400


def test_custom_agents_api_create_list_delete(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    create = client.post(
        "/api/custom-agents",
        headers=_auth_headers(state),
        json={
            "codename": "release-captain",
            "display_name": "Release Captain",
            "role_title": "Release coordinator",
            "purpose": "Checks release readiness before handoff.",
            "prompt": "Review release readiness and summarize blockers for the operator.",
            "engine": "codex",
            "schedule": "daily@09:15",
            "repos": ["acme/api"],
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["ok"] is True
    assert body["deploy_required"] is True
    assert body["agent"]["schedule"] == "cron:9:15"

    listed = client.get("/api/custom-agents").json()
    assert listed["count"] == 1
    assert listed["agents"][0]["codename"] == "release-captain"
    assert "prompt" not in listed["agents"][0]

    deleted = client.delete("/api/custom-agents/release-captain", headers=_auth_headers(state))
    assert deleted.status_code == 200
    assert deleted.json()["removed"] is True
    assert client.get("/api/custom-agents").json()["count"] == 0


def test_custom_agents_api_requires_token_and_valid_payload(tmp_path: Path) -> None:
    state = tmp_path / "state"
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    missing_token = client.post(
        "/api/custom-agents",
        json={
            "codename": "release-captain",
            "display_name": "Release Captain",
            "role_title": "Release coordinator",
            "prompt": "Review release readiness and summarize blockers for the operator.",
        },
    )
    assert missing_token.status_code == 403

    bad_payload = client.post(
        "/api/custom-agents",
        headers=_auth_headers(state),
        json={
            "codename": "lucius",
            "display_name": "Lucius",
            "role_title": "Builder",
            "prompt": "Review release readiness and summarize blockers for the operator.",
        },
    )
    assert bad_payload.status_code == 400
