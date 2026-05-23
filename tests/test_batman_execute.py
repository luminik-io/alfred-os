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


class FakeGate:
    """Returns a pre-canned approval result, ignoring args."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def await_approval(self, channel, message_ts, *, timeout_s=900, poll_interval_s=30):
        self.calls.append((channel, message_ts))
        return self._result


class FakeReporter:
    """Records calls and hands back a deterministic Slack ts."""

    def __init__(self, ts: str | None = "1700000000.000100"):
        self._ts = ts
        self.plans: list[dict] = []
        self.reports: list[dict] = []

    def post_plan(self, plan, *, channel):
        self.plans.append({"plan": plan, "channel": channel})
        return self._ts

    def post_report(self, envelope, *, channel):
        self.reports.append({"envelope": envelope, "channel": channel})
        return True


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


def test_config_from_env_validates_auto_execute(monkeypatch):
    from batman import (
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
