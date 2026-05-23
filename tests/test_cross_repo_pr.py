"""Tests for ``lib/cross_repo_pr.py``.

GitHub interaction is injected via the ``GitHubClient`` Protocol, so
these tests run against an in-memory fake — no ``gh`` subprocess, no
network.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

from cross_repo_pr import (  # noqa: E402
    ChainState,
    CrossRepoPRChain,
    Plan,
    RepoTarget,
    build_pr_body,
    classify_status_checks,
    load_chain_state,
    save_chain_state,
    state_file_path,
    wait_for_ci_green,
)
from labels import AUTHORED, PR_OPEN  # noqa: E402


@dataclass
class FakeGitHub:
    """In-memory GitHub client. Records create / edit calls."""

    next_pr_number: int = 1
    fail_for: set[str] = field(default_factory=set)  # gh_repo to fail pr_create for
    created: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    check_payloads: list[list[dict]] = field(default_factory=list)

    def pr_create(
        self,
        gh_repo: str,
        *,
        title: str,
        body: str,
        head: str,
        labels: Sequence[str],
    ) -> str | None:
        if gh_repo in self.fail_for:
            return None
        url = f"https://github.com/{gh_repo}/pull/{self.next_pr_number}"
        self.next_pr_number += 1
        self.created.append(
            {
                "gh_repo": gh_repo,
                "title": title,
                "body": body,
                "head": head,
                "labels": list(labels),
                "url": url,
            }
        )
        return url

    def pr_edit_body(self, gh_repo: str, pr_url: str, *, body: str) -> bool:
        self.edits.append({"gh_repo": gh_repo, "pr_url": pr_url, "body": body})
        return True

    def pr_status_checks(self, gh_repo: str, pr_url: str) -> list[dict]:
        if self.check_payloads:
            return self.check_payloads.pop(0)
        return []


# --------------------------------------------------------------------------
# Plan generation.
# --------------------------------------------------------------------------


def _make_targets() -> list[RepoTarget]:
    return [
        RepoTarget(
            repo_name="backend",
            gh_repo="your-org/your-backend",
            branch="batman/42-backend",
            title="Add OAuth client config",
            acceptance_criteria="POST /oauth/clients returns 201.",
        ),
        RepoTarget(
            repo_name="frontend",
            gh_repo="your-org/your-frontend",
            branch="batman/42-frontend",
            title="Render OAuth client form",
            acceptance_criteria="Form posts to /oauth/clients.",
        ),
    ]


def test_plan_returns_dataclass_with_targets(tmp_path: Path) -> None:
    chain = CrossRepoPRChain(client=FakeGitHub(), state_dir=tmp_path / "state")
    plan = chain.plan(
        feature_id="oauth-rollout",
        feature_title="OAuth rollout",
        parent_repo="your-specs",
        parent_issue=42,
        parent_gh_org="your-org",
        targets=_make_targets(),
    )
    assert isinstance(plan, Plan)
    assert plan.total() == 2
    assert plan.targets[0].repo_name == "backend"
    assert plan.state_file == tmp_path / "state" / "oauth-rollout.json"
    assert PR_OPEN in plan.base_labels
    assert AUTHORED in plan.base_labels


def test_plan_rejects_empty_targets(tmp_path: Path) -> None:
    chain = CrossRepoPRChain(client=FakeGitHub(), state_dir=tmp_path / "state")
    with pytest.raises(ValueError, match="at least one"):
        chain.plan(
            feature_id="x",
            feature_title="x",
            parent_repo="your-specs",
            parent_issue=1,
            parent_gh_org="your-org",
            targets=[],
        )


def test_plan_rejects_duplicate_repo_names(tmp_path: Path) -> None:
    chain = CrossRepoPRChain(client=FakeGitHub(), state_dir=tmp_path / "state")
    targets = [
        RepoTarget("backend", "your-org/your-backend", "b1", "t"),
        RepoTarget("backend", "your-org/your-backend", "b2", "t"),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        chain.plan(
            feature_id="x",
            feature_title="x",
            parent_repo="your-specs",
            parent_issue=1,
            parent_gh_org="your-org",
            targets=targets,
        )


# --------------------------------------------------------------------------
# Execute path.
# --------------------------------------------------------------------------


def test_execute_opens_each_pr_in_order(tmp_path: Path) -> None:
    fake = FakeGitHub()
    chain = CrossRepoPRChain(client=fake, state_dir=tmp_path / "state", now=1_700_000_000)
    plan = chain.plan(
        feature_id="oauth-rollout",
        feature_title="OAuth rollout",
        parent_repo="your-specs",
        parent_issue=42,
        parent_gh_org="your-org",
        targets=_make_targets(),
    )
    result = chain.execute(plan)
    assert result.ok
    assert set(result.opened.keys()) == {"backend", "frontend"}
    assert [c["gh_repo"] for c in fake.created] == [
        "your-org/your-backend",
        "your-org/your-frontend",
    ]
    # The PR body for the first PR was re-rendered when the second PR opened
    # so it links forward.
    assert fake.edits, "expected earlier PR body to be refreshed"
    assert "frontend" in fake.edits[-1]["body"]


def test_execute_writes_state_atomically(tmp_path: Path) -> None:
    fake = FakeGitHub()
    chain = CrossRepoPRChain(client=fake, state_dir=tmp_path / "state", now=1_700_000_000)
    plan = chain.plan(
        feature_id="ox",
        feature_title="Feature",
        parent_repo="your-specs",
        parent_issue=10,
        parent_gh_org="your-org",
        targets=_make_targets(),
    )
    chain.execute(plan)

    state = load_chain_state(plan.state_file)
    assert state is not None
    assert state.feature_id == "ox"
    assert set(state.prs.keys()) == {"backend", "frontend"}


def test_execute_resumes_skipping_already_opened(tmp_path: Path) -> None:
    fake = FakeGitHub()
    chain = CrossRepoPRChain(client=fake, state_dir=tmp_path / "state", now=1)
    plan = chain.plan(
        feature_id="resume-test",
        feature_title="Resume",
        parent_repo="your-specs",
        parent_issue=5,
        parent_gh_org="your-org",
        targets=_make_targets(),
    )
    # First run.
    chain.execute(plan)
    fake.created.clear()
    fake.edits.clear()
    # Second run: nothing new to open.
    result = chain.execute(plan)
    assert result.ok
    assert fake.created == [], "should not open already-opened PRs"


def test_execute_records_failure(tmp_path: Path) -> None:
    fake = FakeGitHub(fail_for={"your-org/your-frontend"})
    chain = CrossRepoPRChain(client=fake, state_dir=tmp_path / "state", now=1)
    plan = chain.plan(
        feature_id="fail-test",
        feature_title="Fail",
        parent_repo="your-specs",
        parent_issue=7,
        parent_gh_org="your-org",
        targets=_make_targets(),
    )
    result = chain.execute(plan)
    assert not result.ok
    assert result.failed == ["frontend"]
    assert result.opened.keys() == {"backend"}


# --------------------------------------------------------------------------
# Body template.
# --------------------------------------------------------------------------


def test_build_pr_body_renders_parent_and_siblings(tmp_path: Path) -> None:
    targets = _make_targets()
    plan = Plan(
        feature_id="f1",
        feature_title="Feature One",
        parent_repo="your-specs",
        parent_issue=42,
        parent_gh_org="your-org",
        targets=tuple(targets),
        state_file=tmp_path / "f1.json",
    )
    siblings = {"backend": "https://github.com/your-org/your-backend/pull/1"}
    body = build_pr_body(plan=plan, index_1based=2, target=targets[1], siblings=siblings)

    assert "Part 2 of 2: Feature One" in body
    assert "your-org/your-specs/issues/42" in body
    assert "Part 1 (backend): https://github.com/your-org/your-backend/pull/1" in body
    assert "Part 2 (frontend): (this PR)" in body
    assert "Feature ID:** f1" in body
    assert "Form posts to /oauth/clients" in body


# --------------------------------------------------------------------------
# State persistence.
# --------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = state_file_path("xx", state_dir=tmp_path)
    state = ChainState(
        feature_id="xx",
        parent_repo="your-specs",
        parent_issue=1,
        repos=["backend", "frontend"],
        prs={"backend": "https://github.com/your-org/your-backend/pull/1"},
        created_at=123.0,
    )
    save_chain_state(state, path)
    loaded = load_chain_state(path)
    assert loaded is not None
    assert loaded.feature_id == "xx"
    assert loaded.prs == state.prs
    # Atomic write: no leftover .tmp file.
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_chain_state(tmp_path / "nope.json") is None


def test_load_returns_none_for_garbage(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    assert load_chain_state(path) is None


# --------------------------------------------------------------------------
# CI classification + wait_for_ci_green.
# --------------------------------------------------------------------------


def test_classify_status_checks_green_on_empty():
    assert classify_status_checks([]) == "green"
    assert classify_status_checks(None) == "green"


def test_classify_status_checks_red_dominates():
    checks = [
        {"conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS"},
        {"conclusion": "FAILURE"},
    ]
    assert classify_status_checks(checks) == "red"


def test_classify_status_checks_pending_when_in_progress():
    assert classify_status_checks([{"status": "IN_PROGRESS"}]) == "pending"
    assert classify_status_checks([{"status": "QUEUED"}]) == "pending"


def test_classify_status_checks_green_when_all_success():
    checks = [{"conclusion": "SUCCESS"}, {"conclusion": "NEUTRAL"}]
    assert classify_status_checks(checks) == "green"


def test_wait_for_ci_green_short_circuits_on_green():
    fake = FakeGitHub(check_payloads=[[{"conclusion": "SUCCESS"}]])
    called: list[int] = []

    def sleeper(_: float) -> None:
        called.append(1)

    ok = wait_for_ci_green(
        fake,
        "your-org/your-backend",
        "https://github.com/your-org/your-backend/pull/1",
        timeout_seconds=5,
        poll_interval=1,
        sleeper=sleeper,
    )
    assert ok is True
    assert called == [], "should not sleep when first check is green"


def test_wait_for_ci_green_returns_false_on_red():
    fake = FakeGitHub(check_payloads=[[{"conclusion": "FAILURE"}]])
    ok = wait_for_ci_green(
        fake,
        "your-org/your-backend",
        "https://github.com/your-org/your-backend/pull/1",
        timeout_seconds=5,
        poll_interval=1,
        sleeper=lambda _: None,
    )
    assert ok is False
