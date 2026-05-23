"""Focused tests for ``lib.agent_runner.github``."""

from __future__ import annotations

from types import SimpleNamespace


def test_full_repo_resolves_with_gh_org(fresh_agent_runner, monkeypatch):
    """A bare slug becomes ``<GH_ORG>/<slug>``."""
    ar = fresh_agent_runner
    monkeypatch.setattr(ar.github, "GH_ORG", "acme")
    assert ar.github._full_repo("backend") == "acme/backend"
    assert ar.github._full_repo("other/repo") == "other/repo"


def test_full_repo_dry_run_fake_org(fresh_agent_runner, monkeypatch):
    """In dry-run mode, missing GH_ORG falls back to a fake org placeholder."""
    ar = fresh_agent_runner
    monkeypatch.setattr(ar.github, "GH_ORG", "")
    ar.set_dry_run(True)
    try:
        assert ar.github._full_repo("backend") == "dry-run-org/backend"
    finally:
        ar.set_dry_run(False)


def test_parse_claim_comment_roundtrip(fresh_agent_runner):
    """_parse_claim_comment reads back the structured fields."""
    ar = fresh_agent_runner
    body = (
        "<!-- agent-claim:codename=lucius firing_id=2026-05-22-1300-aa "
        "ts=2026-05-22T13:00:00Z -->"
    )
    meta = ar._parse_claim_comment(body)
    assert meta["codename"] == "lucius"
    assert meta["firing_id"] == "2026-05-22-1300-aa"
    assert meta["ts"] == "2026-05-22T13:00:00Z"


def test_is_repo_paused_missing_file_fail_open(fresh_agent_runner):
    """Missing paused-repos.json means no repo is paused."""
    ar = fresh_agent_runner
    assert not ar.is_repo_paused("any/repo")


def test_set_repo_paused_round_trip(fresh_agent_runner):
    """set_repo_paused writes the file; is_repo_paused reads it."""
    ar = fresh_agent_runner
    ar.set_repo_paused("acme/backend", True)
    assert ar.is_repo_paused("acme/backend")
    ar.set_repo_paused("acme/backend", False)
    assert not ar.is_repo_paused("acme/backend")


def test_gh_issue_edit_dry_run_no_subprocess(fresh_agent_runner, monkeypatch):
    """gh_issue_edit in dry-run does not shell out."""
    ar = fresh_agent_runner
    monkeypatch.setattr(ar.github, "GH_ORG", "acme")
    called = []
    monkeypatch.setattr(
        ar,
        "run",
        lambda *a, **kw: called.append(a) or SimpleNamespace(returncode=0),
    )
    ar.set_dry_run(True)
    try:
        assert ar.gh_issue_edit(
            "backend", 12, add_labels=["agent:done"]
        )
    finally:
        ar.set_dry_run(False)
    assert called == []
