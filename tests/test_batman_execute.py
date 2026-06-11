"""Tests for Batman's plan-approve-execute-report lifecycle.

Heavyweight wiring (Slack reactions, gh CLI, network) is replaced with
in-memory fakes so the tests stay offline and deterministic.

Five scenarios pinned here, matching the spec in the task description:

1. Well-formed parent issue -> plan with N children.
2. Approval timeout -> ``ExecuteResult(executed=False, reason="approval_timeout")``.
3. Operator rejects with :x: -> ``reason="rejected_by_operator"``.
4. Approval :white_check_mark: + happy-path execute -> N child issues filed
   across the listed repos.
5. Partial execute failure (3 of 5 succeed) -> ``reason="partial"`` with the
   children that did land in ``created_issue_urls``.

Bonus: parser robustness (unknown short-repo name skipped, no children
parsed surfaces ``EXEC_NO_CHILDREN``).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_sys_path(tmp_path, monkeypatch):
    """Make sure the worktree's ``lib/`` wins on the import path, and
    purge cached modules between tests so the labels/slack_approval
    shims do not bleed across cases."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "your-org")
    # Strip env vars the lifecycle reads so cases can opt-in explicitly.
    for k in (
        "BATMAN_AUTO_EXECUTE",
        "BATMAN_PARENT_REPO",
        "BATMAN_PICKER",
        "BATMAN_BUNDLE_SLUG_PREFIX",
        "BATMAN_APPROVAL_TIMEOUT_S",
        "BATMAN_APPROVAL_MODE",
        "BATMAN_SLACK_CHANNEL",
    ):
        monkeypatch.delenv(k, raising=False)
    for mod in list(sys.modules):
        if mod in ("batman", "labels", "slack_approval", "slack_format"):
            del sys.modules[mod]
    libdir = str(REPO / "lib")
    if libdir not in sys.path:
        sys.path.insert(0, libdir)
    yield


# ---------- sample parent-issue body ----------

SAMPLE_TITLE = "Bundle: billing-v2 rollout"
SAMPLE_BODY = """Bundle: billing-v2 rollout

Repos:
- your-org/your-backend
- your-org/your-frontend
- your-org/your-mobile

Children:
- backend: introduce BillingV2Service
- backend: migrate /api/v1/invoices
- frontend: pricing page rewrite
- mobile: settings screen v2

Done when:
- All children merged to main
- Tests green across all repos
"""


# ---------- fakes ----------


@dataclass
class FakeGitHubClient:
    """In-memory ``GitHubChildIssueClient``.

    ``fail_repos`` is the set of ``owner/repo`` slugs that should fail
    (return ``None``) on ``create_issue``. Everything else gets a
    synthetic URL with a monotonically-increasing issue number.
    """

    fail_repos: set[str] = None  # type: ignore[assignment]
    issued: list[dict] = None  # type: ignore[assignment]
    next_issue_number: int = 100

    def __post_init__(self) -> None:
        if self.fail_repos is None:
            self.fail_repos = set()
        if self.issued is None:
            self.issued = []

    def create_issue(self, repo, *, title, body, labels):
        self.issued.append({"repo": repo, "title": title, "body": body, "labels": list(labels)})
        if repo in self.fail_repos:
            return None
        url = f"https://github.com/{repo}/issues/{self.next_issue_number}"
        self.next_issue_number += 1
        return url


@dataclass
class FakeApprovalResult:
    approved: bool
    verdict: str
    detail: str = ""
    elapsed_s: float = 0.0
    feedback: tuple[object, ...] = ()


class FakeGate:
    """Returns a pre-canned approval result, ignoring args."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def await_approval(
        self,
        channel,
        message_ts,
        *,
        timeout_s=86400,
        poll_interval_s=30,
        kill_check=None,
        feedback_callback=None,
    ):
        self.calls.append((channel, message_ts))
        if kill_check is not None and kill_check():
            return FakeApprovalResult(
                approved=False,
                verdict="rejected",
                detail="killed",
                elapsed_s=0.25,
            )
        if feedback_callback is not None and self._result.feedback:
            feedback_callback(self._result.feedback)
        return self._result


class FakeReporter:
    """Records calls and hands back a deterministic Slack (channel_id, ts).

    The channel_id is what Slack's chat.postMessage echoes back after
    resolving the channel name; a real bot post returns the ID even when
    the caller passed a name. Tests pin this so the BatmanLifecycle
    contract for ``ApprovalEnvelope.channel`` (must be an ID, not a
    name) stays asserted.
    """

    def __init__(
        self,
        ts: str | None = "1700000000.000100",
        channel_id: str = "C0FAKE123",
    ):
        self._ts = ts
        self._channel_id = channel_id
        self.plans: list[dict] = []
        self.reports: list[dict] = []
        self.feedback: list[dict] = []

    def post_plan(self, plan, *, channel):
        self.plans.append({"plan": plan, "channel": channel})
        if self._ts is None:
            return None
        return (self._channel_id, self._ts)

    def post_report(self, envelope, *, channel):
        self.reports.append({"envelope": envelope, "channel": channel})
        return True

    def post_plan_feedback(self, *, channel, message_ts, feedback, **kwargs):
        self.feedback.append(
            {
                "channel": channel,
                "message_ts": message_ts,
                "feedback": feedback,
                "kwargs": kwargs,
            }
        )
        return True


@dataclass
class FakeThreadHandle:
    channel: str = "C0REPORT"
    ts: str = "1700000000.000200"
    permalink: str | None = None


# ---------- regression: ApprovalEnvelope carries the channel ID, not name ----


def test_request_approval_stores_channel_id_from_postmessage_not_config_name():
    """Regression for the bug where ``ApprovalEnvelope.channel`` got the
    config'd channel NAME (``"alfred-fleet"``) instead of the channel
    ID (``"C0..."``) that Slack's ``chat.postMessage`` actually
    resolved. Downstream ``reactions.get`` fails with ``channel_not_found``
    on private channels and some bot scope sets when handed a name.

    The fake reporter is configured to post into ``alfred-fleet`` but
    return the resolved id ``C0LIVECH1``; the envelope must surface
    the id, not the config name.
    """
    from batman import BatmanLifecycle, BatmanLifecycleConfig

    reporter = FakeReporter(channel_id="C0LIVECH1")
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(slack_channel="alfred-fleet"),
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )

    envelope = lifecycle.request_approval(plan)
    assert envelope is not None
    assert envelope.channel == "C0LIVECH1"  # NOT "alfred-fleet"
    assert envelope.message_ts == "1700000000.000100"
    # The reporter still received the config name (so it can resolve
    # to an ID on Slack's side); the post-call propagation is what
    # matters.
    assert reporter.plans[-1]["channel"] == "alfred-fleet"


def test_request_approval_returns_none_when_reporter_returns_none():
    """When the reporter cannot resolve a channel + ts (no bot token,
    transport down), ``request_approval`` returns ``None`` so the
    caller falls back to the BATMAN_AUTO_EXECUTE policy."""
    from batman import BatmanLifecycle, BatmanLifecycleConfig

    reporter = FakeReporter(ts=None)
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(slack_channel="alfred-fleet"),
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    assert envelope is None


# ---------- scenario 1: well-formed parent -> plan with N children ----------


def test_plan_parses_well_formed_parent_into_four_children():
    from batman import BatmanLifecycle, BatmanLifecycleConfig

    lifecycle = BatmanLifecycle(config=BatmanLifecycleConfig())
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    assert plan.bundle_slug == "billing-v2"
    assert plan.affected_repos == (
        "your-org/your-backend",
        "your-org/your-frontend",
        "your-org/your-mobile",
    )
    assert len(plan.children) == 4
    repos = [c.repo for c in plan.children]
    assert repos == [
        "your-org/your-backend",
        "your-org/your-backend",
        "your-org/your-frontend",
        "your-org/your-mobile",
    ]
    # Every child carries the bundle label and the lifecycle label.
    for c in plan.children:
        assert "agent:implement" in c.labels
        assert "agent:bundle:billing-v2" in c.labels
    # Done-when block round-tripped.
    assert "All children merged to main" in plan.done_when
    # Plan markdown shows up readable.
    assert "billing-v2" in plan.plan_markdown
    assert "pricing page rewrite" in plan.plan_markdown
    assert (
        "<https://github.com/your-org/your-product/issues/42|your-org/your-product#42>"
        in plan.plan_markdown
    )
    assert "Next step" in plan.plan_markdown
    assert "Readiness:* ready for approval" in plan.plan_markdown


# ---------- scenario 2: approval timeout ----------


def test_approval_timeout_returns_no_execute():
    from batman import (
        EXEC_APPROVAL_TIMEOUT,
        BatmanLifecycle,
        BatmanLifecycleConfig,
    )
    from slack_approval import APPROVAL_TIMEOUT

    gh = FakeGitHubClient()
    reporter = FakeReporter()
    gate = FakeGate(FakeApprovalResult(approved=False, verdict=APPROVAL_TIMEOUT))

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
            approval_timeout_s=1,
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    assert envelope is not None
    verdict = lifecycle.await_approval(envelope)
    assert verdict.approved is False
    assert verdict.verdict == EXEC_APPROVAL_TIMEOUT
    # Approval failed -> execute MUST NOT run. No children filed.
    assert gh.issued == []


def test_in_app_approval_marker_grants_without_slack_poll(tmp_path: Path):
    from batman import EXEC_OK, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_TIMEOUT

    gh = FakeGitHubClient()
    reporter = FakeReporter()
    gate = FakeGate(FakeApprovalResult(approved=False, verdict=APPROVAL_TIMEOUT))

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    assert envelope is not None
    marker = tmp_path / "alfred" / "batman" / "approvals" / "42.approved"
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")

    verdict = lifecycle.await_approval(envelope)

    assert verdict.approved is True
    assert verdict.verdict == EXEC_OK
    assert "Alfred client" in verdict.detail
    assert gate.calls == []
    assert not marker.exists()
    record = tmp_path / "alfred" / "batman" / "approval-decisions" / "42.json"
    assert json.loads(record.read_text(encoding="utf-8"))["decision"] == "approve"


def test_in_app_approval_marker_interrupts_active_slack_wait(tmp_path: Path):
    from batman import EXEC_OK, BatmanLifecycle, BatmanLifecycleConfig

    gh = FakeGitHubClient()
    reporter = FakeReporter()
    marker = tmp_path / "alfred" / "batman" / "approvals" / "42.approved"

    class MarkerGate:
        calls = 0

        def await_approval(self, *_args, kill_check=None, **_kwargs):
            self.calls += 1
            marker.parent.mkdir(parents=True)
            marker.write_text("", encoding="utf-8")
            assert kill_check is not None
            assert kill_check() is True
            return FakeApprovalResult(
                approved=False,
                verdict="rejected",
                detail="killed",
                elapsed_s=1.5,
            )

    gate = MarkerGate()
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    assert envelope is not None

    verdict = lifecycle.await_approval(envelope)

    assert verdict.approved is True
    assert verdict.verdict == EXEC_OK
    assert verdict.elapsed_s == 1.5
    assert gate.calls == 1
    assert not marker.exists()


def test_file_approval_mode_consumes_rejection_marker_without_slack(tmp_path: Path):
    from batman import EXEC_REJECTED, BatmanLifecycle, BatmanLifecycleConfig

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            approval_mode="file",
            approval_timeout_s=0,
        ),
        gate=None,
        gh_client=FakeGitHubClient(),
        reporter=FakeReporter(),
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    assert envelope is not None
    marker = tmp_path / "alfred" / "batman" / "approvals" / "42.rejected"
    marker.parent.mkdir(parents=True)
    marker.write_text("declined via Alfred client: scope too broad\n", encoding="utf-8")

    verdict = lifecycle.await_approval(envelope)

    assert verdict.approved is False
    assert verdict.verdict == EXEC_REJECTED
    assert "scope too broad" in verdict.detail
    assert not marker.exists()
    record = tmp_path / "alfred" / "batman" / "approval-decisions" / "42.json"
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["decision"] == "decline"
    assert "scope too broad" in payload["reason"]


# ---------- scenario 3: operator rejects ----------


def test_operator_rejection_returns_rejected_by_operator():
    from batman import EXEC_REJECTED, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_REJECTED

    gh = FakeGitHubClient()
    reporter = FakeReporter()
    gate = FakeGate(FakeApprovalResult(approved=False, verdict=APPROVAL_REJECTED))

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    verdict = lifecycle.await_approval(envelope)
    assert verdict.verdict == EXEC_REJECTED
    assert verdict.approved is False
    assert gh.issued == []


# ---------- scenario 4: approval ok -> happy-path execute ----------


def test_approval_granted_then_happy_path_files_all_children():
    from batman import EXEC_OK, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_GRANTED

    gh = FakeGitHubClient()
    reporter = FakeReporter()
    gate = FakeGate(FakeApprovalResult(approved=True, verdict=APPROVAL_GRANTED))

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    verdict = lifecycle.await_approval(envelope)
    assert verdict.approved is True
    assert verdict.verdict == EXEC_OK

    result = lifecycle.execute(plan)
    assert result.executed is True
    assert result.reason == EXEC_OK
    assert len(result.created_issue_urls) == 4
    assert result.failed_repos == ()
    # All four children landed in the listed repos.
    assert [i["repo"] for i in gh.issued] == [
        "your-org/your-backend",
        "your-org/your-backend",
        "your-org/your-frontend",
        "your-org/your-mobile",
    ]
    # The bundle label propagated.
    for issued in gh.issued:
        assert "agent:bundle:billing-v2" in issued["labels"]
        assert "agent:implement" in issued["labels"]

    lifecycle.report(plan, result)
    assert reporter.reports, "report should be posted on happy path"
    envelope_out = reporter.reports[0]["envelope"]
    assert envelope_out.bundle_slug == "billing-v2"
    assert len(envelope_out.created) == 4


def test_approval_thread_feedback_is_appended_to_child_issues():
    from batman import EXEC_OK, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_GRANTED

    gh = FakeGitHubClient()
    gate = FakeGate(
        FakeApprovalResult(
            approved=True,
            verdict=APPROVAL_GRANTED,
            feedback=({"text": "Use the simpler onboarding copy requested by the operator."},),
        )
    )

    reporter = FakeReporter()
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    verdict = lifecycle.await_approval(envelope)

    assert verdict.approved is True
    assert verdict.verdict == EXEC_OK
    assert verdict.feedback == ("Use the simpler onboarding copy requested by the operator.",)
    assert reporter.feedback == [
        {
            "channel": "C0FAKE123",
            "message_ts": "1700000000.000100",
            "feedback": ("Use the simpler onboarding copy requested by the operator.",),
            "kwargs": {
                "plan": plan,
                "all_feedback": ("Use the simpler onboarding copy requested by the operator.",),
                "revised_repos": plan.affected_repos,
            },
        }
    ]

    result = lifecycle.execute(plan)

    assert result.reason == EXEC_OK
    assert "## Operator Slack Amendments" in gh.issued[0]["body"]
    assert "Treat these as approved plan changes" in gh.issued[0]["body"]
    assert "Use the simpler onboarding copy requested by the operator." in gh.issued[0]["body"]
    assert "Planning Assistant Interpretation" in gh.issued[0]["body"]


def test_approval_repo_feedback_changes_child_issue_scope():
    from batman import EXEC_OK, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_GRANTED

    gh = FakeGitHubClient()
    gate = FakeGate(
        FakeApprovalResult(
            approved=True,
            verdict=APPROVAL_GRANTED,
            feedback=(
                {
                    "text": "remove repo: mobile\nadd repo: your-org/your-admin",
                },
            ),
        )
    )
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=FakeReporter(),
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    lifecycle.await_approval(envelope)

    result = lifecycle.execute(plan)

    assert result.reason == EXEC_OK
    assert [item["repo"] for item in gh.issued] == [
        "your-org/your-backend",
        "your-org/your-backend",
        "your-org/your-frontend",
        "your-org/your-admin",
    ]
    assert all(item["repo"] != "your-org/your-mobile" for item in gh.issued)
    assert gh.issued[-1]["title"] == "your-admin: implement billing-v2"
    assert "Remove repository scope: mobile" in gh.issued[-1]["body"]
    assert "Add repository scope: your-org/your-admin" in gh.issued[-1]["body"]


def test_approval_feedback_with_open_question_blocks_execution():
    from batman import EXEC_NEEDS_SCOPE, BatmanLifecycle, BatmanLifecycleConfig
    from slack_approval import APPROVAL_GRANTED

    gh = FakeGitHubClient()
    gate = FakeGate(
        FakeApprovalResult(
            approved=True,
            verdict=APPROVAL_GRANTED,
            feedback=({"text": "question: Should this include mobile?"},),
        )
    )
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="approval-gate",
            parent_repo="your-org/your-product",
            slack_channel="alfred-fleet",
        ),
        gate=gate,
        gh_client=gh,
        reporter=FakeReporter(),
    )
    plan = lifecycle.plan(
        body=SAMPLE_BODY,
        title=SAMPLE_TITLE,
        parent_repo="your-org/your-product",
        parent_issue_number=42,
    )
    envelope = lifecycle.request_approval(plan)
    verdict = lifecycle.await_approval(envelope)

    assert verdict.approved is False
    assert verdict.verdict == EXEC_NEEDS_SCOPE
    result = lifecycle.execute(plan)
    assert result.reason == EXEC_NEEDS_SCOPE
    assert gh.issued == []


# ---------- scenario 5: partial execute failure ----------


def test_partial_execute_failure_reports_landed_children():
    """3 of 5 succeed -> reason=partial, created lists the survivors."""
    from batman import EXEC_PARTIAL, BatmanLifecycle, BatmanLifecycleConfig

    body = """Bundle: five-repo-fanout

Repos:
- your-org/repo-a
- your-org/repo-b
- your-org/repo-c
- your-org/repo-d
- your-org/repo-e

Children:
- repo-a: scope a
- repo-b: scope b
- repo-c: scope c
- repo-d: scope d
- repo-e: scope e

Done when:
- Child issues are created for every listed repo.
"""

    gh = FakeGitHubClient(fail_repos={"your-org/repo-b", "your-org/repo-d"})
    reporter = FakeReporter()

    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(
            auto_execute="1",
            parent_repo="your-org/your-product",
        ),
        gh_client=gh,
        reporter=reporter,
    )
    plan = lifecycle.plan(
        body=body,
        title="Bundle: five-repo-fanout",
        parent_repo="your-org/your-product",
        parent_issue_number=99,
    )
    assert len(plan.children) == 5

    result = lifecycle.execute(plan)
    assert result.reason == EXEC_PARTIAL
    assert result.executed is True  # 3 landed; partial-with-some-survivors
    assert len(result.created_issue_urls) == 3
    assert set(result.failed_repos) == {"your-org/repo-b", "your-org/repo-d"}


def test_slack_reporter_captures_trusted_report_followup(tmp_path):
    from batman import ReportEnvelope, SlackReporter

    root_calls = []
    replies = []

    def fake_root(**kwargs):
        root_calls.append(kwargs)
        return FakeThreadHandle()

    def fake_reader(channel, ts):
        assert channel == "C0REPORT"
        assert ts == "1700000000.000200"
        return (
            "change: keep the onboarding copy warmer",
            "question: should this include the docs page too?",
        )

    def fake_reply(handle, *, text, severity):
        replies.append({"handle": handle, "text": text, "severity": severity})
        return True

    reporter = SlackReporter(
        firing_id="20260528-120000-test",
        thread_root=fake_root,
        report_feedback_timeout_s=0,
        feedback_reader=fake_reader,
        feedback_reply=fake_reply,
        followup_dir=tmp_path / "followups",
    )

    posted = reporter.post_report(
        ReportEnvelope(
            bundle_slug="planning-loop",
            parent_title="Improve planning loop",
            created=("https://github.com/your-org/your-product/issues/123",),
            failed_repos=(),
            reason="ok",
        ),
        channel="alfred",
    )

    assert posted is True
    assert root_calls
    assert replies
    assert replies[0]["severity"] == "warn"
    assert "Follow-up feedback captured" in replies[0]["text"]
    assert "Needs a decision before more work" in replies[0]["text"]
    saved = list((tmp_path / "followups").glob("*.md"))
    assert len(saved) == 1
    body = saved[0].read_text()
    assert "# Follow-up for Improve planning loop" in body
    assert "Slack Follow-up Feedback" in body
    assert "`change`: Change: keep the onboarding copy warmer" in body
    assert "`question` needs decision" in body


def test_slack_reporter_default_followups_are_visible_to_plans(tmp_path, monkeypatch):
    from batman import ReportEnvelope, SlackReporter

    home = tmp_path / "alfred"
    monkeypatch.setenv("ALFRED_HOME", str(home))

    def fake_root(**_kwargs):
        return FakeThreadHandle(channel="C0REPORT", ts="1700000000.000200")

    reporter = SlackReporter(
        firing_id="20260528-120000-test",
        thread_root=fake_root,
        report_feedback_timeout_s=0,
        feedback_reader=lambda _channel, _ts: ("change: add a docs smoke test",),
        feedback_reply=lambda *_args, **_kwargs: True,
    )

    assert reporter.post_report(
        ReportEnvelope(
            bundle_slug="planning-loop",
            parent_title="Improve planning loop",
            created=("https://github.com/your-org/your-product/issues/123",),
            failed_repos=(),
            reason="ok",
        ),
        channel="alfred",
    )

    followups = list((home / "state" / "followups").glob("*.md"))
    assert len(followups) == 1
    body = followups[0].read_text()
    assert "# Follow-up for Improve planning loop" in body
    assert "add a docs smoke test" in body


def test_slack_reporter_registers_plan_thread(tmp_path, monkeypatch):
    from batman import SlackReporter, parse_parent_issue
    from slack_thread_registry import SlackThreadRegistry

    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))

    def fake_root(**_kwargs):
        return FakeThreadHandle(channel="C0PLAN", ts="1700000000.000300")

    reporter = SlackReporter(
        firing_id="20260528-120000-test",
        thread_root=fake_root,
        report_feedback_timeout_s=0,
    )
    plan = parse_parent_issue(
        body="Repos:\n- your-org/backend\n\nChildren:\n- backend: add a clear setup page\n",
        title="Bundle: setup-page",
        parent_repo="your-org/parent",
        parent_issue_number=77,
    )

    assert reporter.post_plan(plan, channel="alfred") == ("C0PLAN", "1700000000.000300")

    record = SlackThreadRegistry(tmp_path / "alfred" / "state" / "slack-threads").lookup(
        "C0PLAN", "1700000000.000300"
    )
    assert record is not None
    assert record.kind == "plan"
    assert record.parent_repo == "your-org/parent"
    assert record.parent_issue == 77
    assert record.plan_path
    assert Path(record.plan_path) == tmp_path / "alfred" / "batman-plans" / "77-plan.md"
    assert Path(record.plan_path).exists()


def test_slack_reporter_writes_local_plan_copy_when_slack_post_fails(tmp_path, monkeypatch):
    from batman import SlackReporter, parse_parent_issue

    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fallback_posts = []
    reporter = SlackReporter(
        firing_id="20260528-120000-test",
        thread_root=lambda **_kwargs: None,
        fallback_post=lambda message, **kwargs: fallback_posts.append((message, kwargs)),
        report_feedback_timeout_s=0,
    )
    plan = parse_parent_issue(
        body="Repos:\n- your-org/backend\n\nChildren:\n- backend: add a clear setup page\n",
        title="Bundle: setup-page",
        parent_repo="your-org/parent",
        parent_issue_number=77,
    )

    assert reporter.post_plan(plan, channel="alfred") is None

    plan_path = tmp_path / "alfred" / "batman-plans" / "77-plan.md"
    assert plan_path.exists()
    assert "setup-page" in plan_path.read_text(encoding="utf-8")
    assert fallback_posts


# ---------- bonus: parser robustness ----------


def test_no_children_yields_no_children_exec_reason():
    from batman import EXEC_NO_CHILDREN, BatmanLifecycle, BatmanLifecycleConfig

    body = """Bundle: empty-test

Repos:
- your-org/only-repo

Done when:
- I get bored
"""
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(auto_execute="1"),
        gh_client=FakeGitHubClient(),
    )
    plan = lifecycle.plan(
        body=body,
        title="Bundle: empty-test",
        parent_repo="your-org/parent",
        parent_issue_number=1,
    )
    assert plan.children == ()
    result = lifecycle.execute(plan)
    assert result.reason == EXEC_NO_CHILDREN
    assert result.executed is False


def test_unknown_short_repo_in_children_is_skipped():
    from batman import BatmanLifecycle, BatmanLifecycleConfig

    body = """Bundle: typo-test

Repos:
- your-org/your-backend

Children:
- backend: real one
- frontent: typo, should be skipped
"""
    lifecycle = BatmanLifecycle(config=BatmanLifecycleConfig())
    plan = lifecycle.plan(
        body=body,
        title="Bundle: typo-test",
        parent_repo="your-org/parent",
        parent_issue_number=7,
    )
    assert len(plan.children) == 1
    assert plan.children[0].repo == "your-org/your-backend"
    assert plan.children[0].title == "real one"


def test_vague_child_scope_blocks_execution_before_filing():
    from batman import EXEC_NEEDS_SCOPE, BatmanLifecycle, BatmanLifecycleConfig

    body = """Bundle: vague-plan

Repos:
- your-org/your-backend

Children:
- backend: TODO

Done when:
- Tests pass
"""
    gh = FakeGitHubClient()
    lifecycle = BatmanLifecycle(
        config=BatmanLifecycleConfig(auto_execute="1"),
        gh_client=gh,
    )

    plan = lifecycle.plan(
        body=body,
        title="Bundle: vague-plan",
        parent_repo="your-org/parent",
        parent_issue_number=9,
    )
    result = lifecycle.execute(plan)

    assert result.reason == EXEC_NEEDS_SCOPE
    assert result.executed is False
    assert gh.issued == []
    assert "too vague" in result.detail


def test_config_from_env_validates_auto_execute(monkeypatch):
    from batman import (
        APPROVAL_MODE_FILE,
        APPROVAL_MODE_SLACK_OR_FILE,
        AUTO_EXECUTE_FORCE,
        AUTO_EXECUTE_GATE,
        AUTO_EXECUTE_OFF,
        BatmanLifecycleConfig,
    )

    monkeypatch.setenv("BATMAN_AUTO_EXECUTE", "approval-gate")
    cfg = BatmanLifecycleConfig.from_env()
    assert cfg.auto_execute == AUTO_EXECUTE_GATE
    assert cfg.gate_enabled is True
    assert cfg.execute_enabled is True

    monkeypatch.setenv("BATMAN_AUTO_EXECUTE", "1")
    cfg = BatmanLifecycleConfig.from_env()
    assert cfg.auto_execute == AUTO_EXECUTE_FORCE
    assert cfg.gate_enabled is False
    assert cfg.execute_enabled is True

    monkeypatch.setenv("BATMAN_AUTO_EXECUTE", "junk")
    cfg = BatmanLifecycleConfig.from_env()
    assert cfg.auto_execute == AUTO_EXECUTE_OFF
    assert cfg.execute_enabled is False

    monkeypatch.setenv("BATMAN_AUTO_EXECUTE", "approval-gate")
    monkeypatch.setenv("BATMAN_APPROVAL_MODE", "file")
    cfg = BatmanLifecycleConfig.from_env()
    assert cfg.approval_mode == APPROVAL_MODE_FILE

    monkeypatch.setenv("BATMAN_APPROVAL_MODE", "nonsense")
    cfg = BatmanLifecycleConfig.from_env()
    assert cfg.approval_mode == APPROVAL_MODE_SLACK_OR_FILE
