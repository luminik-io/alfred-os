"""Tests for the runner-side dedup helpers.

Covers ``find_open_authored_pr_for_issue`` (label filter, substring
guard, gh-failure fallback) and ``find_existing_worktree`` /
``reuse_or_make_worktree`` (find, reuse, replace-when-stale).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    for mod in list(sys.modules):
        if mod.startswith("agent_runner"):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


# ---------------------------------------------------------------------------
# find_open_authored_pr_for_issue
# ---------------------------------------------------------------------------


def test_find_open_authored_pr_returns_match(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(
        ar,
        "gh_json",
        lambda *a, **kw: [
            {
                "number": 100,
                "url": "https://github.com/myorg/backend/pull/100",
                "state": "open",
                "labels": [{"name": "agent:authored"}],
                "title": "fix issue #27",
                "body": "Closes #27",
            }
        ],
    )
    pr = ar.find_open_authored_pr_for_issue("backend", 27)
    assert pr is not None
    assert pr["number"] == 100


def test_find_open_authored_pr_skips_human_authored(monkeypatch):
    """A human PR that mentions the issue does NOT lock the queue."""
    import agent_runner as ar

    monkeypatch.setattr(
        ar,
        "gh_json",
        lambda *a, **kw: [
            {
                "number": 200,
                "url": "https://github.com/myorg/backend/pull/200",
                "state": "open",
                "labels": [{"name": "human-authored"}],
                "title": "drive-by fix #27",
                "body": "Just doing a fix on #27 myself",
            }
        ],
    )
    assert ar.find_open_authored_pr_for_issue("backend", 27) is None


def test_find_open_authored_pr_substring_false_positive(monkeypatch):
    """Issue #27 must not be locked by a PR that closes #2750, gh's
    text search substring-matches."""
    import agent_runner as ar

    monkeypatch.setattr(
        ar,
        "gh_json",
        lambda *a, **kw: [
            {
                "number": 300,
                "url": "https://github.com/myorg/backend/pull/300",
                "state": "open",
                "labels": [{"name": "agent:authored"}],
                "title": "feat: closes #2750",
                "body": "fixes #2750 and #275",
            }
        ],
    )
    assert ar.find_open_authored_pr_for_issue("backend", 27) is None


def test_find_open_authored_pr_handles_gh_failure(monkeypatch):
    """A transient gh failure must NOT lock the picker, return None."""
    import agent_runner as ar

    monkeypatch.setattr(ar, "gh_json", lambda *a, **kw: [])
    assert ar.find_open_authored_pr_for_issue("backend", 27) is None


# ---------------------------------------------------------------------------
# find_existing_worktree / reuse_or_make_worktree
# ---------------------------------------------------------------------------


def test_find_existing_worktree_returns_none_when_root_missing(tmp_path):
    import agent_runner as ar

    # WORKTREE_ROOT under ALFRED_HOME doesn't exist yet.
    assert ar.find_existing_worktree("backend", "lucius", "275") is None


def test_find_existing_worktree_returns_most_recent_match(tmp_path, monkeypatch):
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    older = ar.WORKTREE_ROOT / "eng-lucius-backend-275-100"
    newer = ar.WORKTREE_ROOT / "eng-lucius-backend-275-200"
    older.mkdir()
    newer.mkdir()
    # Force the mtime ordering.
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (time.time(), time.time()))
    found = ar.find_existing_worktree("backend", "lucius", "275")
    assert found == newer


def test_find_existing_worktree_ignores_other_targets(tmp_path):
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    (ar.WORKTREE_ROOT / "eng-lucius-backend-275-1").mkdir()
    (ar.WORKTREE_ROOT / "eng-lucius-backend-99-1").mkdir()
    (ar.WORKTREE_ROOT / "eng-batman-backend-275-1").mkdir()
    found = ar.find_existing_worktree("backend", "lucius", "275")
    assert found is not None
    assert found.name == "eng-lucius-backend-275-1"


def test_reuse_or_make_worktree_falls_back_to_make_when_no_existing(monkeypatch, tmp_path):
    import agent_runner as ar

    fake_path = tmp_path / "fresh-wt"
    fake_path.mkdir()

    def fake_make(local_repo, agent, target, base="origin/main"):
        return fake_path, "lucius/275-fresh"

    monkeypatch.setattr(ar, "make_worktree", fake_make)
    wt, branch, reused = ar.reuse_or_make_worktree("backend", "lucius", "275")
    assert wt == fake_path
    assert branch == "lucius/275-fresh"
    assert reused is False


def test_reuse_or_make_worktree_reuses_healthy_existing(monkeypatch, tmp_path):
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    existing = ar.WORKTREE_ROOT / "eng-lucius-backend-275-1"
    existing.mkdir()

    monkeypatch.setattr(ar, "_worktree_is_stale", lambda *a, **kw: False)
    monkeypatch.setattr(ar, "_worktree_branch", lambda wt: "lucius/275-prev")
    # Avoid touching git in the reuse path.
    monkeypatch.setattr(ar, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0, "", ""))

    wt, branch, reused = ar.reuse_or_make_worktree("backend", "lucius", "275")
    assert wt == existing
    assert branch == "lucius/275-prev"
    assert reused is True


def test_reuse_or_make_worktree_replaces_stale_existing(monkeypatch, tmp_path):
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    existing = ar.WORKTREE_ROOT / "eng-lucius-backend-275-1"
    existing.mkdir()

    monkeypatch.setattr(ar, "_worktree_is_stale", lambda *a, **kw: True)
    removed: list = []
    monkeypatch.setattr(ar, "remove_worktree", lambda lr, wt: removed.append(wt))

    fresh_path = tmp_path / "fresh"
    fresh_path.mkdir()
    monkeypatch.setattr(ar, "make_worktree", lambda *a, **kw: (fresh_path, "lucius/275-fresh"))

    wt, _branch, reused = ar.reuse_or_make_worktree("backend", "lucius", "275")
    assert removed == [existing]
    assert wt == fresh_path
    assert reused is False


def test_reuse_or_make_worktree_replaces_when_branch_unavailable(monkeypatch, tmp_path):
    """A worktree whose HEAD we cannot read is treated as wedged and replaced."""
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    existing = ar.WORKTREE_ROOT / "eng-lucius-backend-275-1"
    existing.mkdir()

    monkeypatch.setattr(ar, "_worktree_is_stale", lambda *a, **kw: False)
    monkeypatch.setattr(ar, "_worktree_branch", lambda wt: None)
    monkeypatch.setattr(ar, "remove_worktree", lambda *a, **kw: None)

    fresh_path = tmp_path / "fresh"
    fresh_path.mkdir()
    monkeypatch.setattr(ar, "make_worktree", lambda *a, **kw: (fresh_path, "lucius/275-fresh"))
    wt, _branch, reused = ar.reuse_or_make_worktree("backend", "lucius", "275")
    assert wt == fresh_path
    assert reused is False
