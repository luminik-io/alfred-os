"""Tests for ``alfred serve`` v1.

Covers:

* Empty state: missing state root renders a clean empty fleet view.
* Populated state via ``tmp_path``: events JSONL is parsed and surfaced
  on /, /firings, and /firings/<id>.
* 404 for unknown firing id.
* Filter by codename on /firings.
* HTMX partial swap on /.
* Path-traversal rejection on the firing id.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# Skip the entire module when the `serve` extra is not installed. CI runs
# default `pip install -e .` which does not pull fastapi/uvicorn/jinja2;
# those tests only make sense when the operator has chosen to install
# the serve dashboard. Pytest's importorskip skips with a clear marker
# rather than letting a collection-time ImportError crash the suite.
pytest.importorskip("fastapi")

# lib/ is not a package on install yet, add it to sys.path explicitly.
REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import server.views as server_views  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from server import FilesystemReader, create_app  # noqa: E402
from server.formatting import friendly_time, short_firing_id  # noqa: E402
from spec_helper import IssueDraft  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_state(tmp_path: Path) -> Path:
    """An ``$ALFRED_HOME/state`` directory that exists but is empty."""
    state = tmp_path / "state"
    state.mkdir()
    return state


@pytest.fixture()
def populated_state(tmp_path: Path) -> Path:
    """A state tree with two codenames, three firings, mixed statuses."""
    state = tmp_path / "state"
    (state / "lucius" / "events").mkdir(parents=True)
    (state / "drake" / "events").mkdir(parents=True)
    # Also seed a reserved subdir so we can confirm it is not enumerated
    # as a codename.
    (state / "transcripts").mkdir()

    _write_jsonl(
        state / "lucius" / "events" / "2026-05-23-1200-aa.jsonl",
        [
            {"ts": "2026-05-23T12:00:00Z", "event": "firing_started", "agent": "lucius"},
            {
                "ts": "2026-05-23T12:01:30Z",
                "event": "issue_picked",
                "repo": "your-org/api",
                "number": 42,
            },
            {
                "ts": "2026-05-23T12:05:00Z",
                "event": "firing_ended",
                "agent": "lucius",
                "result": "pr_opened",
            },
        ],
    )
    _write_jsonl(
        state / "lucius" / "events" / "2026-05-22-0900-bb.jsonl",
        [
            {"ts": "2026-05-22T09:00:00Z", "event": "firing_started", "agent": "lucius"},
            {"ts": "2026-05-22T09:00:45Z", "event": "firing_failed", "reason": "rate_limit"},
        ],
    )
    _write_jsonl(
        state / "drake" / "events" / "2026-05-23-1100-cc.jsonl",
        [
            {"ts": "2026-05-23T11:00:00Z", "event": "firing_started", "agent": "drake"},
            {"ts": "2026-05-23T11:02:00Z", "event": "firing_ended", "agent": "drake"},
        ],
    )
    return state


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _client(state_root: Path) -> TestClient:
    return TestClient(create_app(FilesystemReader(state_root=state_root)))


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_state_root_does_not_exist(tmp_path: Path) -> None:
    """Reader must tolerate a totally missing state root."""
    client = _client(tmp_path / "does-not-exist")
    response = client.get("/")
    assert response.status_code == 200
    assert "No codenames found" in response.text


def test_empty_state_root_empty_dir(empty_state: Path) -> None:
    client = _client(empty_state)
    response = client.get("/")
    assert response.status_code == 200
    assert "No codenames found" in response.text

    response = client.get("/firings")
    assert response.status_code == 200
    assert "No firings to show" in response.text


# ---------------------------------------------------------------------------
# Populated state
# ---------------------------------------------------------------------------


def test_fleet_view_lists_codenames(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="fleet-table"' in response.text
    assert 'class="table-shell"' in response.text
    assert 'class="ops-table fleet-table"' in response.text
    assert "lucius" in response.text
    assert "drake" in response.text
    # Reserved subdir must not be enumerated as a codename.
    assert "transcripts</strong>" not in response.text


def test_fleet_view_htmx_partial(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "fleet-table" in response.text
    assert "<header" not in response.text  # partial, not full shell


def test_fleet_view_htmx_partial_skips_reliability_report(populated_state: Path) -> None:
    class CountingReader(FilesystemReader):
        reliability_calls = 0

        def reliability_report(self) -> dict[str, object]:
            self.reliability_calls += 1
            return {
                "status": "ok",
                "actions": [],
                "failure_patterns": [],
                "stale_workers": [],
                "promotion_suggestions": [],
            }

    reader = CountingReader(state_root=populated_state)
    client = TestClient(create_app(reader))

    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "fleet-table" in response.text
    assert reader.reliability_calls == 0

    response = client.get("/")
    assert response.status_code == 200
    assert reader.reliability_calls == 1


def test_firings_view_lists_recent(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings")
    assert response.status_code == 200
    assert 'class="table-shell"' in response.text
    assert 'class="ops-table firings-table"' in response.text
    assert "2026-05-23-1200-aa" in response.text
    assert "2026-05-22-0900-bb" in response.text
    assert "2026-05-23-1100-cc" in response.text
    assert "time-pill" in response.text


def test_firings_view_filter_by_codename(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings", params={"codename": "drake"})
    assert response.status_code == 200
    assert "2026-05-23-1100-cc" in response.text
    assert "2026-05-23-1200-aa" not in response.text


def test_json_api_status_and_firings(populated_state: Path) -> None:
    client = _client(populated_state)

    status = client.get("/api/status")
    assert status.status_code == 200
    payload = status.json()
    assert {row["codename"] for row in payload["agents"]} == {"drake", "lucius"}
    assert "status" in payload["reliability"]

    firings = client.get("/api/firings", params={"codename": "lucius", "limit": 1})
    assert firings.status_code == 200
    rows = firings.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["codename"] == "lucius"

    detail = client.get("/api/firings/2026-05-23-1200-aa")
    assert detail.status_code == 200
    assert detail.json()["raw_events"][1]["event"] == "issue_picked"

    missing = client.get("/api/firings/does-not-exist")
    assert missing.status_code == 404


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


def test_api_slack_trusted_users_adds_and_removes_local_collaborator(
    empty_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UENV1")
    client = _client(empty_state)

    before = client.get("/api/slack/trusted-users")
    assert before.status_code == 200
    before_rows = {row["user_id"]: row for row in before.json()["users"]}
    assert before_rows["UOPERATOR"]["sources"] == ["operator"]
    assert before_rows["UENV1"]["sources"] == ["env"]

    added = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "<@UTEAM1>"},
        headers={"Origin": "http://testserver"},
    )
    assert added.status_code == 200
    rows = {row["user_id"]: row for row in added.json()["users"]}
    assert added.json()["added"] is True
    assert rows["UTEAM1"]["sources"] == ["local"]
    assert rows["UTEAM1"]["can_remove"] is True

    removed = client.post(
        "/api/slack/trusted-users/UTEAM1/remove",
        headers={"Origin": "http://testserver"},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert "UTEAM1" not in {row["user_id"] for row in removed.json()["users"]}


def test_api_slack_trusted_users_rejects_bad_origin_and_bad_ids(empty_state: Path) -> None:
    client = _client(empty_state)

    forbidden = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "UTEAM1"},
        headers={"Origin": "https://evil.example"},
    )
    assert forbidden.status_code == 403

    bad_id = client.post(
        "/api/slack/trusted-users",
        json={"user_id": "not a user"},
        headers={"Origin": "http://testserver"},
    )
    assert bad_id.status_code == 400


def test_firing_complete_marks_record_finished(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "lucius" / "events").mkdir(parents=True)
    _write_jsonl(
        state / "lucius" / "events" / "2026-05-27-1224-aa.jsonl",
        [
            {"ts": "2026-05-27T12:24:00Z", "event": "firing_started", "agent": "lucius"},
            {
                "ts": "2026-05-27T12:25:00Z",
                "event": "firing_complete",
                "agent": "lucius",
                "outcome": "silent_no_work",
            },
        ],
    )
    record = FilesystemReader(state_root=state).get_firing("2026-05-27-1224-aa")

    assert record is not None
    assert record.status == "ok"
    assert record.ended_at == "2026-05-27T12:25:00Z"


def test_firing_detail_renders_events(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/2026-05-23-1200-aa")
    assert response.status_code == 200
    assert "issue_picked" in response.text
    assert "your-org/api" in response.text
    assert "firing_ended" in response.text


def test_firing_detail_failed_marks_error(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/2026-05-22-0900-bb")
    assert response.status_code == 200
    assert "dot-error" in response.text
    assert "rate_limit" in response.text


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_firing_returns_404(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/does-not-exist")
    assert response.status_code == 404
    assert "not located" in response.text


def test_friendly_time_and_short_firing_id_are_scan_friendly() -> None:
    now = datetime(2026, 5, 27, 12, 30, tzinfo=UTC)

    assert friendly_time("2026-05-27T12:24:59Z", now=now) == "5m ago"
    assert friendly_time("2026-05-26T12:24:59Z", now=now) == "yesterday 12:24"
    assert short_firing_id("20260527-122459-1d31") == "20260527-122459-1d31"
    assert short_firing_id("20260527-122459-extra-long-1d31") == "20260527-122459...1d31"


def test_plans_view_lists_saved_batman_plans(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    plans = tmp_path / "batman-plans"
    plans.mkdir()
    (plans / "61-plan.md").write_text(
        "# Batman Plan for Issue #61\n\n"
        "**Status:** Draft (awaiting approval)\n\n"
        "**Issue URL:** https://github.com/example/repo/issues/61\n\n"
        "**Affected Repos:** backend, frontend\n\n"
        "Plan Preview:\n",
        encoding="utf-8",
    )
    client = _client(state)

    response = client.get("/plans")

    assert response.status_code == 200
    assert "Batman Plan for Issue #61" in response.text
    assert "backend, frontend" in response.text
    assert 'target="_blank" rel="noopener noreferrer"' in response.text

    detail = client.get("/plans/61-plan")
    assert detail.status_code == 200
    assert 'target="_blank" rel="noopener noreferrer"' in detail.text

    api_list = client.get("/api/plans")
    assert api_list.status_code == 200
    assert api_list.json()["rows"][0]["plan_id"] == "61-plan"

    api_detail = client.get("/api/plans/61-plan")
    assert api_detail.status_code == 200
    assert api_detail.json()["title"] == "Batman Plan for Issue #61"


def test_plans_view_lists_slack_planning_drafts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    drafts = state / "planning-drafts"
    drafts.mkdir(parents=True)
    (drafts / "slack-20260529-0400-E1.json").write_text(
        json.dumps(
            {
                "source": "slack",
                "created_at": "2026-05-29T04:00:00Z",
                "updated_at": "2026-05-29T04:05:00Z",
                "draft": {
                    "title": "Add threaded plan revisions",
                    "problem": "Operators need to revise Alfred plans before implementation.",
                    "desired_behavior": "Replies update the saved plan draft.",
                    "repos": ["luminik-io/alfred-os"],
                    "acceptance_criteria": ["Thread replies update readiness."],
                },
                "spec_body": "# Spec\n\nThread replies update readiness.",
                "readiness": {"ok": True, "score": 92, "questions": []},
                "revision_count": 2,
            }
        ),
        encoding="utf-8",
    )
    client = _client(state)

    response = client.get("/plans")

    assert response.status_code == 200
    assert "Add threaded plan revisions" in response.text
    assert "slack" in response.text
    assert "ready" in response.text
    assert "92/100" in response.text
    assert "2 revisions" in response.text
    assert "Operators need to revise Alfred plans before implementation." in response.text

    detail = client.get("/plans/slack-20260529-0400-E1")
    assert detail.status_code == 200
    assert "# Spec" in detail.text
    assert "2 revisions" in detail.text

    api_detail = client.get("/api/plans/slack-20260529-0400-E1")
    assert api_detail.status_code == 200
    payload = api_detail.json()
    assert payload["source"] == "slack"
    assert payload["revision_count"] == 2
    assert payload["readiness_score"] == 92


def test_plans_view_lists_slack_followups(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    (followups / "slack-C1-1716480000.000000.md").write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Captured: 2026-05-29T06:45:00Z\n"
        "- Thread: C1 / 1716480000.000000\n"
        "- Parent: [luminik-io/alfred-os#120](https://github.com/luminik-io/alfred-os/issues/120)\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `change`: add a manual docs smoke test\n",
        encoding="utf-8",
    )
    client = _client(state)

    response = client.get("/plans")

    assert response.status_code == 200
    assert "Follow-up for Improve planning loop" in response.text
    assert "needs follow-up" in response.text
    assert "add a manual docs smoke test" in response.text

    detail = client.get("/plans/slack-C1-1716480000.000000")
    assert detail.status_code == 200
    assert "Slack Follow-up Feedback" in detail.text

    api_detail = client.get("/api/plans/slack-C1-1716480000.000000")
    assert api_detail.status_code == 200
    payload = api_detail.json()
    assert payload["source"] == "followup"
    assert payload["title"] == "Follow-up for Improve planning loop"
    assert payload["status"] == "needs follow-up"
    assert payload["parent"] == "https://github.com/luminik-io/alfred-os/issues/120"
    assert "manual docs smoke test" in payload["preview"]


def test_followup_can_be_converted_to_planning_draft(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Captured: 2026-05-29T06:45:00Z\n"
        "- Thread: C1 / 1716480000.000000\n"
        "- Parent: [luminik-io/alfred-os#120](https://github.com/luminik-io/alfred-os/issues/120)\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `change`: add a manual docs smoke test\n",
        encoding="utf-8",
    )
    client = _client(state)

    response = client.post(
        "/plans/slack-C1-1716480000.000000/convert-followup",
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
    assert payload["draft"]["repos"] == ["luminik-io/alfred-os"]
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
        "- Created: https://github.com/your-org/api/pull/42, "
        "https://github.com/your-org/web/issues/77\n\n"
        "## Slack Follow-up Feedback\n\n"
        "### Items\n\n"
        "- `test`: add a smoke test to both shipped slices\n",
        encoding="utf-8",
    )
    client = _client(state)

    response = client.post("/api/plans/20260529-bundle/convert-followup")

    assert response.status_code == 200
    draft_path = Path(response.json()["draft_path"])
    payload = json.loads(draft_path.read_text(encoding="utf-8"))
    assert payload["draft"]["repos"] == ["your-org/api", "your-org/web"]


def test_followup_actions_reject_cross_origin_posts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    followups = state / "followups"
    followups.mkdir(parents=True)
    source = followups / "slack-C1-1716480000.000000.md"
    source.write_text(
        "# Follow-up for Improve planning loop\n\n"
        "- Parent: [your-org/api#120](https://github.com/your-org/api/issues/120)\n\n"
        "Cross-origin pages should not be able to mutate this inbox.\n",
        encoding="utf-8",
    )
    client = _client(state)

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
        "- Parent: [luminik-io/alfred-os#120](https://github.com/luminik-io/alfred-os/issues/120)\n\n"
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

    response = client.post("/api/plans/slack-C1-1716480000.000000/convert-followup")

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
        "- Parent: [luminik-io/alfred-os#120](https://github.com/luminik-io/alfred-os/issues/120)\n\n"
        "Already answered in the PR thread.\n",
        encoding="utf-8",
    )
    client = _client(state)

    response = client.post(
        "/api/plans/slack-C1-1716480000.000000/mark-handled",
    )

    assert response.status_code == 200
    assert not source.exists()
    archived_path = Path(response.json()["archived_path"])
    assert archived_path.exists()
    assert archived_path.parent.name == "handled"
    assert "Follow-up action: handled" in archived_path.read_text(encoding="utf-8")
    plans = client.get("/api/plans").json()["rows"]
    assert plans == []


def test_slack_planning_draft_empty_body_does_not_render_raw_event(tmp_path: Path) -> None:
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
                    "repos": ["luminik-io/alfred-os"],
                },
                "issue_body": "",
                "spec_body": "",
                "event": {"user": "USECRET", "channel": "CSECRET", "text": "raw slack text"},
            }
        ),
        encoding="utf-8",
    )
    client = _client(state)

    detail = client.get("/plans/slack-empty-body")

    assert detail.status_code == 200
    assert "Operators need a clean preview." in detail.text
    assert "USECRET" not in detail.text
    assert "raw slack text" not in detail.text


def test_plans_view_skips_invalid_newer_draft_to_fill_limit(tmp_path: Path) -> None:
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
    client = _client(state)

    response = client.get("/api/plans", params={"limit": 1})

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["title"] == "Valid older draft"


def test_planning_view_assesses_and_saves_draft(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)

    response = client.get("/planning")
    assert response.status_code == 200
    assert "Planning" in response.text

    vague = client.post(
        "/planning",
        data={
            "title": "Make it better",
            "problem": "Confusing.",
            "desired_behavior": "Improve stuff.",
            "repos": "luminik-io/alfred-os",
            "acceptance_criteria": "Make it nice",
            "action": "preview",
        },
    )
    assert vague.status_code == 200
    assert "Needs scope" in vague.text
    assert "What problem is the user facing today?" in vague.text

    refined = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "luminik-io/alfred-os\nexample-org/web",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "chat_message": (
                "acceptance: the child issue body includes approved Slack amendments\n"
                "remove repo: example-org/web"
            ),
            "action": "refine",
        },
    )
    assert refined.status_code == 200
    assert "2 amendment(s) applied" in refined.text
    assert "the child issue body includes approved Slack amendments" in refined.text
    assert "luminik-io/alfred-os" in refined.text

    clear = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "luminik-io/alfred-os",
            "acceptance_criteria": (
                "A plan with unresolved questions is marked needs-scope.\n"
                "Slack plan messages tell the operator how to reply with changes."
            ),
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "action": "save",
        },
    )
    assert clear.status_code == 200
    assert "Ready for Alfred" in clear.text
    assert "Draft saved" in clear.text
    saved = list((tmp_path / "planning-drafts").glob("*.md"))
    assert len(saved) == 1
    assert "## Acceptance Criteria" in saved[0].read_text(encoding="utf-8")

    spec = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "luminik-io/alfred-os",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "action": "save_spec",
        },
    )
    assert spec.status_code == 200
    assert "Spec saved" in spec.text
    specs = list((tmp_path / "spec-drafts").glob("*.md"))
    assert len(specs) == 1
    assert "## Implementation Guardrails" in specs[0].read_text(encoding="utf-8")

    spec_with_chat = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            "user": "Repo owner or teammate",
            "current_behavior": "Batman posts a plan and waits for emoji approval.",
            "desired_behavior": (
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            "repos": "luminik-io/alfred-os\nexample-org/web",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "chat_message": (
                "acceptance: saved specs include chat amendments\nremove repo: example-org/web"
            ),
            "action": "save_spec",
        },
    )
    assert spec_with_chat.status_code == 200
    specs = list((tmp_path / "spec-drafts").glob("*.md"))
    assert specs
    saved_spec = max(specs, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8")
    assert "saved specs include chat amendments" in saved_spec
    assert "example-org/web" not in saved_spec


def test_planning_refine_engine_uses_existing_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.views as views

    captured: dict[str, Path] = {}

    def fake_engine_refiner_from_env(*, workdir: Path):
        captured["workdir"] = workdir

        def fake_refiner(draft, messages):
            return {"title": "Engine refined Slack plan"}

        return fake_refiner

    monkeypatch.setattr(views, "engine_refiner_from_env", fake_engine_refiner_from_env)
    client = _client(tmp_path / "state")

    response = client.post(
        "/planning",
        data={
            "title": "Add Slack plan revision flow",
            "problem": (
                "Operators and teammates need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            "desired_behavior": (
                "Batman keeps implementation paused while plan feedback is collected."
            ),
            "repos": "luminik-io/alfred-os",
            "acceptance_criteria": "Slack plan feedback is acknowledged in thread.",
            "test_plan": "Run planning assistant server tests.",
            "out_of_scope": "No hosted workflow.",
            "chat_message": "Make the title friendlier.",
            "action": "refine",
        },
    )

    assert response.status_code == 200
    assert captured["workdir"] == tmp_path
    assert not (tmp_path / "planning-drafts").exists()
    assert "Engine refined Slack plan" in response.text


def test_planning_page_surfaces_memory_and_queues_spec_candidate(tmp_path: Path) -> None:
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
            return f"candidate-{len(self.candidates)}"

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
            "repos": "luminik-io/alfred-os",
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
    assert memory.candidates[0]["repo"] == "luminik-io/alfred-os"
    assert json.loads(memory.candidates[0]["evidence"])["kind"] == "planning_spec"


def test_planning_memory_candidate_uses_writable_provider_inside_chain(tmp_path: Path) -> None:
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
            "repos": "luminik-io/alfred-os",
            "acceptance_criteria": "Slack plan messages tell the operator how to reply.",
            "test_plan": "Run Batman unit tests and manually inspect the Slack payload.",
            "out_of_scope": "No automatic GitHub issue creation from the planning UI.",
            "action": "save_spec",
        },
    )

    assert response.status_code == 200
    assert chain.writable.candidates
    assert chain.writable.candidates[0]["repo"] == "luminik-io/alfred-os"


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
        repos=["luminik-io/alfred-os"],
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
    client = _client(state)

    response = client.post(
        "/api/plans/draft",
        json={"text": "title: Add a desktop compose panel"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_id"].startswith("compose-")
    assert isinstance(payload["readiness"]["score"], int)
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

    plans = client.get("/api/plans").json()["rows"]
    assert any(row["plan_id"] == payload["draft_id"] for row in plans)


def test_compose_draft_iterates_on_same_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)

    first = client.post(
        "/api/plans/draft",
        json={
            "text": (
                "title: Ship the compose panel\n"
                "problem: Operators cannot author specs inside the desktop client today."
            )
        },
    ).json()
    draft_id = first["draft_id"]
    assert first["readiness"]["ok"] is False

    second = client.post(
        "/api/plans/draft",
        json={
            "draft_id": draft_id,
            "text": (
                "desired: The desktop client lets users describe work and shows "
                "the readiness score plus clarifying questions.\n"
                "repo: luminik-io/alfred-os\n"
                "acceptance: Submitting intent renders the readiness score.\n"
                "test: Vitest covers the compose submit path with a mocked api."
            ),
        },
    ).json()

    assert second["draft_id"] == draft_id
    assert second["revision_count"] == 2
    assert second["readiness"]["score"] > first["readiness"]["score"]
    assert "luminik-io/alfred-os" in second["draft"]["repos"]

    drafts = client.get("/api/plans/drafts").json()["rows"]
    matching = [row for row in drafts if row["draft_id"] == draft_id]
    assert len(matching) == 1
    assert matching[0]["revision_count"] == 2


def test_compose_draft_requires_intent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)

    response = client.post("/api/plans/draft", json={"text": "   "})

    assert response.status_code == 400
    assert not (state / "planning-drafts").exists()


def test_compose_draft_rejects_cross_origin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)

    response = client.post(
        "/api/plans/draft",
        json={"text": "title: Cross-origin attempt"},
        headers={"origin": "https://example.invalid"},
    )

    assert response.status_code == 403
    assert not (state / "planning-drafts").exists()


def test_compose_draft_rejects_foreign_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)

    response = client.post(
        "/api/plans/draft",
        json={"draft_id": "slack-20260529-0400-E1", "text": "title: Sneaky"},
    )

    assert response.status_code == 200
    assert response.json()["draft_id"].startswith("compose-")


def test_compose_draft_ignores_unknown_compose_draft_id(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    client = _client(state)
    unknown = "compose-20260531-9999-never-seen"

    response = client.post(
        "/api/plans/draft",
        json={"draft_id": unknown, "text": "title: Safe new draft"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["draft_id"].startswith("compose-")
    assert payload["draft_id"] != unknown
    assert not (state / "planning-drafts" / f"{unknown}.json").exists()


def test_plan_detail_rejects_path_traversal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    reader = FilesystemReader(state_root=state)

    assert reader.get_plan("../secrets") is None
    assert reader.get_plan(".hidden") is None


def test_firing_id_rejects_path_traversal(populated_state: Path) -> None:
    """Operator-supplied firing id must not be able to read arbitrary files."""
    _client(populated_state)
    # Hitting "%2E%2E%2F" decoded equals "../"  FastAPI normalizes path
    # params so the safest assertion is that any explicit traversal char
    # is rejected by the reader.
    reader = FilesystemReader(state_root=populated_state)
    assert reader.get_firing("../etc/passwd") is None
    assert reader.get_firing(".hidden") is None
    assert reader.get_firing("a\\b") is None


def test_healthz(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_reliability_report_missing_brain_db_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "missing-fleet-brain.db"
    monkeypatch.setenv("ALFRED_FLEET_BRAIN_DB", str(db_path))

    report = FilesystemReader(state_root=tmp_path / "state").reliability_report()

    assert report["status"] == "unknown"
    assert "not initialized" in report["error"]
    assert not db_path.exists()


def test_malformed_jsonl_does_not_crash(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "lucius" / "events").mkdir(parents=True)
    path = state / "lucius" / "events" / "2026-05-23-1300-zz.jsonl"
    path.write_text(
        '{"ts":"2026-05-23T13:00:00Z","event":"firing_started"}\n'
        "this is not json\n"
        '{"ts":"2026-05-23T13:01:00Z","event":"firing_ended"}\n',
        encoding="utf-8",
    )
    client = _client(state)
    response = client.get("/firings/2026-05-23-1300-zz")
    assert response.status_code == 200
    # The good lines survive; the bad one is dropped silently.
    assert "firing_started" in response.text
    assert "firing_ended" in response.text
