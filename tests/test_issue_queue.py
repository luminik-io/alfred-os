#!/usr/bin/env python3
"""Tests for operator pickup-queue control (lib/issue_queue.py + Slack verbs)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import issue_queue as iq  # noqa: E402
from labels import DO_NOT_PICKUP, IMPLEMENT  # noqa: E402


def test_parse_issue_ref_accepts_url_and_slug():
    assert iq.parse_issue_ref("https://github.com/org/repo/issues/12") == (
        "org/repo",
        12,
    )
    assert iq.parse_issue_ref("<https://github.com/org/repo/issues/7>") == (
        "org/repo",
        7,
    )
    assert iq.parse_issue_ref("github.com/org/repo/issues/3") == ("org/repo", 3)
    assert iq.parse_issue_ref("org/repo#5") == ("org/repo", 5)
    assert iq.parse_issue_ref("org/repo 5") == ("org/repo", 5)


def test_parse_issue_ref_rejects_ambiguous():
    assert iq.parse_issue_ref("5") is None  # bare number: no repo
    assert iq.parse_issue_ref("just some words") is None
    assert iq.parse_issue_ref("") is None


def _capture_gh(monkeypatch):
    calls: dict = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(iq, "_gh_bin", lambda: "gh")
    monkeypatch.setattr(iq, "_gh_subprocess_env", lambda: {})
    monkeypatch.setattr(iq.subprocess, "run", fake_run)
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "org/repo")
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    return calls


def test_set_issue_pickup_queue_adds_implement(monkeypatch):
    calls = _capture_gh(monkeypatch)
    ok, detail = iq.set_issue_pickup("org/repo", 12, hold=False)
    assert ok
    cmd = calls["cmd"]
    assert cmd[:4] == ["gh", "issue", "edit", "12"]
    assert "--add-label" in cmd and IMPLEMENT in cmd
    assert "--remove-label" in cmd and DO_NOT_PICKUP in cmd
    assert "queued" in detail


def test_set_issue_pickup_hold_adds_do_not_pickup(monkeypatch):
    calls = _capture_gh(monkeypatch)
    ok, detail = iq.set_issue_pickup("org/repo", 9, hold=True)
    assert ok
    cmd = calls["cmd"]
    add_idx = cmd.index("--add-label")
    assert cmd[add_idx + 1] == DO_NOT_PICKUP
    remove_idx = cmd.index("--remove-label")
    assert cmd[remove_idx + 1] == IMPLEMENT
    assert "held" in detail


def test_set_issue_pickup_rejects_bad_repo(monkeypatch):
    _capture_gh(monkeypatch)
    ok, _ = iq.set_issue_pickup("notaslug", 1, hold=False)
    assert not ok


def test_set_issue_pickup_requires_allowlisted_repo(monkeypatch):
    _capture_gh(monkeypatch)
    ok, detail = iq.set_issue_pickup("other/repo", 1, hold=False)
    assert not ok
    assert "allowlist" in detail


def test_set_issue_pickup_requires_configured_allowlist(monkeypatch):
    _capture_gh(monkeypatch)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    ok, detail = iq.set_issue_pickup("org/repo", 1, hold=False)
    assert not ok
    assert "allowlist is not configured" in detail


def test_allowed_queue_repos_accepts_fallback_env(monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "org/api, org/web")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "org/extra")
    assert iq.allowed_queue_repos() == {"org/api", "org/web", "org/extra"}


def test_close_issue_runs_gh_issue_close(monkeypatch):
    calls = _capture_gh(monkeypatch)
    ok, detail = iq.close_issue("org/repo", 12)
    assert ok
    cmd = calls["cmd"]
    assert cmd[:4] == ["gh", "issue", "close", "12"]
    assert cmd[-2:] == ["-R", "org/repo"]
    # No label flags: Done uses GitHub's native closed state, not a label.
    assert "--add-label" not in cmd
    assert "--remove-label" not in cmd
    assert "closed" in detail


def test_close_issue_rejects_bad_repo(monkeypatch):
    _capture_gh(monkeypatch)
    ok, _ = iq.close_issue("notaslug", 1)
    assert not ok


def test_close_issue_requires_allowlisted_repo(monkeypatch):
    _capture_gh(monkeypatch)
    ok, detail = iq.close_issue("other/repo", 1)
    assert not ok
    assert "allowlist" in detail


def test_close_issue_requires_configured_allowlist(monkeypatch):
    _capture_gh(monkeypatch)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    ok, detail = iq.close_issue("org/repo", 1)
    assert not ok
    assert "allowlist is not configured" in detail


def test_close_issue_surfaces_gh_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="gh: not authenticated")

    monkeypatch.setattr(iq, "_gh_bin", lambda: "gh")
    monkeypatch.setattr(iq, "_gh_subprocess_env", lambda: {})
    monkeypatch.setattr(iq.subprocess, "run", fake_run)
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "org/repo")
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    ok, detail = iq.close_issue("org/repo", 4)
    assert not ok
    assert "not authenticated" in detail


def test_slack_parses_queue_and_hold_verbs():
    from slack_control import parse_control_command

    cmd = parse_control_command("queue https://github.com/org/repo/issues/4")
    assert cmd is not None and cmd.verb == "queue"
    assert cmd.arg == "https://github.com/org/repo/issues/4"

    held = parse_control_command("hold org/repo#8")
    assert held is not None and held.verb == "hold" and held.arg == "org/repo#8"

    assigned = parse_control_command("assign org/repo#8")
    assert assigned is not None and assigned.verb == "assign"

    # No argument -> not a command (falls through to planning intake).
    assert parse_control_command("queue") is None


def _queue_handler(monkeypatch):
    """Build a SlackControlHandler with set_issue_pickup stubbed to record calls.

    Returns ``(handler, calls)``. The stub never shells out to gh, so these
    tests exercise only the operator gate and verb routing in ``_run_queue``.
    """
    import slack_control

    calls: list = []

    def fake_set_issue_pickup(repo, number, *, hold):
        calls.append((repo, number, hold))
        verb = "held (Alfred will not pick it up)" if hold else "queued for Alfred"
        return True, f"{repo}#{number} {verb}"

    # _run_queue does ``from issue_queue import ... set_issue_pickup``, so patch
    # the name on the issue_queue module it imports from.
    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)

    handler = slack_control.SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        operator_user_id="UOPERATOR",
    )
    return handler, calls


def test_slack_queue_arm_rejected_for_non_operator(monkeypatch):
    handler, calls = _queue_handler(monkeypatch)
    result = handler.handle("queue org/repo#7", trusted=True, actor_user_id="UTEAM")
    assert result.handled is True
    assert result.action == "queue_rejected"
    assert "Only the configured approver can queue work for Alfred" in result.text
    # The gate must short-circuit before any label mutation.
    assert calls == []


def test_slack_hold_allowed_for_non_operator(monkeypatch):
    handler, calls = _queue_handler(monkeypatch)
    result = handler.handle("hold org/repo#7", trusted=True, actor_user_id="UTEAM")
    assert result.handled is True
    assert result.action == "hold"
    assert calls == [("org/repo", 7, True)]


def test_slack_queue_arm_allowed_for_operator(monkeypatch):
    handler, calls = _queue_handler(monkeypatch)
    result = handler.handle("queue org/repo#7", trusted=True, actor_user_id="UOPERATOR")
    assert result.handled is True
    assert result.action == "queue"
    assert calls == [("org/repo", 7, False)]


def test_slack_assign_rejected_for_non_operator(monkeypatch):
    import issue_assignment
    import slack_control

    def _must_not_run(*args, **kwargs):
        raise AssertionError("assign must not run for a non-operator")

    monkeypatch.setattr(issue_assignment, "assign_issue", _must_not_run)
    handler = slack_control.SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        operator_user_id="UOPERATOR",
    )

    result = handler.handle("assign org/repo#7", trusted=True, actor_user_id="UTEAM")

    assert result.handled is True
    assert result.action == "assign_rejected"
    assert "Only the configured approver" in result.text


def test_slack_assign_allowed_for_operator(monkeypatch):
    import issue_assignment
    import slack_control

    calls: list[tuple[str, int]] = []

    def fake_assign_issue(repo, number):
        calls.append((repo, number))
        return SimpleNamespace(ok=True, detail=f"{repo}#{number} assigned to Lucius")

    monkeypatch.setattr(issue_assignment, "assign_issue", fake_assign_issue)
    handler = slack_control.SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        operator_user_id="UOPERATOR",
    )

    result = handler.handle("assign org/repo#7", trusted=True, actor_user_id="UOPERATOR")

    assert result.handled is True
    assert result.action == "assign"
    assert calls == [("org/repo", 7)]
    assert "<https://github.com/org/repo/issues/7|org/repo#7>" in result.text
    assert "assigned to Lucius" in result.text


def test_slack_queue_failure_hides_raw_gh_error(monkeypatch):
    import slack_control

    def fake_set_issue_pickup(repo, number, *, hold):
        return False, "fatal: gh token expired; HTTP 401 at github.com/api"

    monkeypatch.setattr(iq, "set_issue_pickup", fake_set_issue_pickup)
    handler = slack_control.SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
        operator_user_id="UOPERATOR",
    )
    result = handler.handle("queue org/repo#7", trusted=True, actor_user_id="UOPERATOR")
    assert result.handled is True
    assert result.action == "queue_failed"
    # Slack-facing text is generic; raw gh stderr never reaches chat.
    assert result.text == "*Queue update failed (gh error).*"
    assert "gh token" not in result.text
    assert "401" not in result.text
    # Raw detail is preserved on the structured field for server-side JSON.
    assert "gh token expired" in result.detail
