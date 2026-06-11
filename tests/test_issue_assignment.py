"""Tests for label-free issue assignment routing."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import issue_assignment as ia  # noqa: E402
from labels import (  # noqa: E402
    DO_NOT_PICKUP,
    ENHANCEMENT,
    FEATURE,
    IMPLEMENT,
    LARGE_FEATURE,
    NEEDS_HUMAN_REVIEW,
    NEEDS_HUMAN_SCOPE,
    NEEDS_INFO,
)


def _issue(
    *,
    repo: str = "acme-io/acme-backend",
    number: int = 12,
    title: str = "Implement retry banner",
    body: str = "Add a retry banner for failed checkout calls.",
    labels: tuple[str, ...] = (),
    state: str = "OPEN",
) -> ia.IssueSnapshot:
    return ia.IssueSnapshot(
        repo=repo,
        number=number,
        title=title,
        body=body,
        labels=labels,
        state=state,
        url=f"https://github.com/{repo}/issues/{number}",
    )


def _allowlist(monkeypatch, value: str) -> None:
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", value)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)


def test_single_repo_work_routes_to_lucius() -> None:
    decision = ia.decide_assignment(_issue())

    assert decision.route == ia.ROUTE_LUCIUS
    assert decision.agent == "lucius"
    assert decision.add_labels == (IMPLEMENT,)
    assert "single-repo" in decision.reason


def test_multi_surface_work_routes_to_batman() -> None:
    decision = ia.decide_assignment(
        _issue(
            title="Roll out attendee sync across backend and frontend",
            body="Backend API, frontend dashboard, and mobile error states all need coordinated changes.",
        )
    )

    assert decision.route == ia.ROUTE_BATMAN
    assert decision.agent == "batman"
    assert decision.add_labels == (LARGE_FEATURE,)
    assert "multiple product surfaces" in decision.reason


def test_vague_work_routes_to_human_scope() -> None:
    decision = ia.decide_assignment(_issue(title="Maybe?", body=""))

    assert decision.route == ia.ROUTE_HUMAN_SCOPE
    assert decision.agent == "human"
    assert decision.add_labels == (NEEDS_HUMAN_SCOPE,)


def test_existing_agent_labels_are_noops() -> None:
    assert ia.decide_assignment(_issue(labels=(IMPLEMENT,))).route == ia.ROUTE_ALREADY_ROUTED
    assert ia.decide_assignment(_issue(labels=(LARGE_FEATURE,))).agent == "batman"


def test_human_blocking_labels_block_assignment() -> None:
    for label in (NEEDS_INFO, NEEDS_HUMAN_REVIEW, NEEDS_HUMAN_SCOPE):
        decision = ia.decide_assignment(_issue(labels=(label,)))

        assert decision.route == ia.ROUTE_BLOCKED
        assert not decision.changed
        assert label in decision.reason


def test_lucius_product_labels_block_assignment_until_triaged() -> None:
    decision = ia.decide_assignment(_issue(labels=(FEATURE, ENHANCEMENT)))

    assert decision.route == ia.ROUTE_BLOCKED
    assert not decision.changed
    assert FEATURE in decision.reason
    assert ENHANCEMENT in decision.reason


def test_do_not_pickup_still_can_be_cleared_by_assignment() -> None:
    decision = ia.decide_assignment(_issue(labels=(DO_NOT_PICKUP,)))

    assert decision.route == ia.ROUTE_LUCIUS
    assert decision.add_labels == (IMPLEMENT,)
    assert decision.remove_labels == (DO_NOT_PICKUP,)


def test_closed_issue_is_blocked() -> None:
    decision = ia.decide_assignment(_issue(state="CLOSED"))

    assert decision.route == ia.ROUTE_BLOCKED
    assert not decision.changed
    assert "closed" in decision.reason


def test_assign_issue_dry_run_does_not_mutate(monkeypatch) -> None:
    _allowlist(monkeypatch, "acme-io/acme-backend")
    calls: list[list[str]] = []

    def runner(cmd: list[str]):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = ia.assign_issue(
        "acme-io/acme-backend",
        12,
        dry_run=True,
        fetcher=lambda _repo, _number: _issue(),
        runner=runner,
    )

    assert result.ok
    assert result.dry_run is True
    assert "Dry run" in result.detail
    assert calls == []


def test_assign_issue_applies_decided_label(monkeypatch) -> None:
    _allowlist(monkeypatch, "acme-io/acme-backend")
    calls: list[list[str]] = []

    def runner(cmd: list[str]):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = ia.assign_issue(
        "acme-io/acme-backend",
        12,
        fetcher=lambda _repo, _number: _issue(),
        runner=runner,
    )

    assert result.ok
    assert result.changed
    edit = calls[-1]
    assert edit[1:4] == ["issue", "edit", "12"]
    assert "--add-label" in edit
    assert IMPLEMENT in edit


def test_assign_issue_rejects_blocked_assignment(monkeypatch) -> None:
    _allowlist(monkeypatch, "acme-io/acme-backend")
    calls: list[list[str]] = []

    def runner(cmd: list[str]):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = ia.assign_issue(
        "acme-io/acme-backend",
        12,
        fetcher=lambda _repo, _number: _issue(labels=(NEEDS_INFO,)),
        runner=runner,
    )

    assert not result.ok
    assert not result.changed
    assert NEEDS_INFO in result.error
    assert calls == []


def test_assign_issue_requires_allowlisted_repo(monkeypatch) -> None:
    _allowlist(monkeypatch, "acme-io/acme-frontend")

    result = ia.assign_issue(
        "acme-io/acme-backend",
        12,
        fetcher=lambda _repo, _number: _issue(),
    )

    assert not result.ok
    assert "allowlist" in result.error
