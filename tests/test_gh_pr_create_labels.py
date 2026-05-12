"""Tests for ``gh_pr_create`` label handling and stderr surfacing.

Regression coverage for the gh_pr_create label bootstrap path, where
label-not-found failures used to make PR-open failures opaque.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    for mod in list(sys.modules):
        if mod.startswith("agent_runner"):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def test_standard_labels_includes_batman_and_large_feature():
    import agent_runner as ar

    names = {name for name, _, _ in ar.STANDARD_LABELS}
    assert "batman-pr-open" in names
    assert "agent:large-feature" in names


def test_gh_pr_create_creates_adhoc_labels_not_in_standard(monkeypatch, tmp_path):
    """A caller can pass a label not in STANDARD_LABELS and gh_pr_create
    auto-creates it with a neutral grey colour, so a fresh repo + fresh
    label combination doesn't fail the PR-open path."""
    import agent_runner as ar

    body = tmp_path / "body.md"
    body.write_text("test")

    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        # Simulate ensure_labels and label-create being no-ops, then
        # a successful pr-create that returns the URL on stdout.
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/myorg/backend/pull/123\n", ""
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ar, "run", fake_run)
    url = ar.gh_pr_create(
        "backend",
        title="t",
        body_file=body,
        labels=["agent:large-feature", "fancy-new-label"],
    )
    assert url == "https://github.com/myorg/backend/pull/123"
    label_creates = [
        c for c in cmds if c[:3] == ["gh", "label", "create"] and "fancy-new-label" in c
    ]
    assert label_creates, "ad-hoc label not in STANDARD_LABELS should be created"


def test_gh_pr_create_logs_stderr_on_failure(monkeypatch, tmp_path, capsys):
    """gh-pr-create failure surfaces the gh stderr to process stderr so
    the runner's Slack alert carries the actual error instead of the
    generic 'PR open failed' string."""
    import agent_runner as ar

    body = tmp_path / "body.md"
    body.write_text("test")

    def fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 1, "", "could not add label batman-pr-open: not found in target repo"
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ar, "run", fake_run)
    url = ar.gh_pr_create("backend", title="t", body_file=body, labels=["batman-pr-open"])
    assert url is None
    err = capsys.readouterr().err
    assert "[gh_pr_create] FAILED" in err
    assert "could not add label" in err


def test_gh_pr_create_can_open_draft_pr(monkeypatch, tmp_path):
    import agent_runner as ar

    body = tmp_path / "body.md"
    body.write_text("test")
    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/myorg/backend/pull/125\n", ""
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ar, "run", fake_run)
    url = ar.gh_pr_create("backend", title="t", body_file=body, draft=True)

    assert url == "https://github.com/myorg/backend/pull/125"
    pr_create = next(c for c in cmds if c[:3] == ["gh", "pr", "create"])
    assert "--draft" in pr_create


def test_gh_pr_create_does_not_recreate_standard_labels(monkeypatch, tmp_path):
    """STANDARD_LABELS labels are created by ensure_labels (cached
    per-process); the ad-hoc loop should NOT call gh label create on
    them again."""
    import agent_runner as ar

    body = tmp_path / "body.md"
    body.write_text("test")

    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/myorg/backend/pull/124\n", ""
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ar, "run", fake_run)
    ar.gh_pr_create(
        "backend",
        title="t",
        body_file=body,
        labels=["batman-pr-open", "agent:large-feature"],
    )
    # Both labels are in STANDARD_LABELS; the ad-hoc loop should not have
    # appended any additional gh label create calls beyond ensure_labels.
    adhoc_creates = [
        c
        for c in cmds
        if c[:3] == ["gh", "label", "create"]
        and ("Auto-created by gh_pr_create on first use" in (c[-3] if len(c) >= 3 else ""))
    ]
    assert adhoc_creates == []
