"""Coverage for the ``sweep_extra_paths`` helper in bin/agent-cleanup.py.

The OSS cleanup script is procedural (top-level execution); we load it
as a module and target the testable functions inside. ``sweep_extra_paths``
is the new helper from the cleanup-scope-hole fix — it sweeps operator-
managed worktree pools outside ``$ALFRED_HOME/worktrees`` that the
fleet sweep would never touch.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLEANUP = REPO / "bin" / "agent-cleanup.py"


def test_help_prints_usage_without_running_cleanup(tmp_path):
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(tmp_path / "alfred")
    env["WORKSPACE_ROOT"] = str(tmp_path / "workspace")

    res = subprocess.run(
        [sys.executable, str(CLEANUP), "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )

    assert res.returncode == 0
    assert "usage: agent-cleanup.py" in res.stdout
    assert "[cleanup]" not in res.stdout
    assert res.stderr == ""


@pytest.fixture
def cleanup(tmp_path, monkeypatch):
    """Load the sweep_extra_paths helper from bin/agent-cleanup.py.

    The cleanup script is procedural (top-level execution) so importing
    it normally runs the whole sweep. We side-step that by extracting
    the helper function via importlib in a controlled environment: a
    tmp ALFRED_HOME + WORKSPACE_ROOT, and we trap the eventual
    ``sys.exit(0)`` so the function objects are captured before the
    procedural body completes.
    """
    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    (workspace / "product").mkdir(parents=True)
    (alfred / "state").mkdir(parents=True)
    (alfred / "worktrees").mkdir(parents=True)

    monkeypatch.setenv("ALFRED_HOME", str(alfred))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    # No extra paths configured so the module-level call to
    # sweep_extra_paths is a no-op during fixture load. Tests invoke
    # sweep_extra_paths directly with their own paths.
    monkeypatch.delenv("ALFRED_CLEANUP_EXTRA_PATHS", raising=False)
    # No claim sweep work.
    monkeypatch.setenv("ALFRED_CLAIM_SWEEP_REPOS", "")

    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod == "agent_cleanup":
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))

    spec = importlib.util.spec_from_file_location("agent_cleanup", CLEANUP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_cleanup"] = mod
    # The procedural body posts to Slack on dirty-worktree skips; stub
    # via env before loading.
    monkeypatch.setenv("ALFRED_SLACK_WEBHOOK_URL", "")
    with contextlib.suppress(SystemExit):
        spec.loader.exec_module(mod)
    return mod


def test_sweep_extra_paths_removes_old_clean_worktrees(cleanup, tmp_path, monkeypatch):
    extra_root = tmp_path / "extra-worktrees"
    extra_root.mkdir()
    old = extra_root / "old-wt"
    old.mkdir()
    (old / ".git").write_text("placeholder")
    fresh = extra_root / "fresh-wt"
    fresh.mkdir()
    (fresh / ".git").write_text("placeholder")

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    monkeypatch.setattr(cleanup, "WORKSPACE", empty_workspace)
    monkeypatch.setattr(cleanup, "dirty_worktree_reason", lambda wt: None)

    aged = time.time() - 49 * 3600
    os.utime(old, (aged, aged))

    stats = cleanup.sweep_extra_paths(
        paths=str(extra_root),
        max_age_hours=48,
        now=time.time(),
    )

    assert stats["removed"] == 1
    assert stats["skipped"] == 0
    assert not old.exists()
    assert fresh.exists()


def test_sweep_extra_paths_skips_dirty_worktrees(cleanup, tmp_path, monkeypatch):
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    dirty = extra_root / "dirty-wt"
    dirty.mkdir()
    (dirty / ".git").write_text("placeholder")

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    monkeypatch.setattr(cleanup, "WORKSPACE", empty_workspace)
    monkeypatch.setattr(cleanup, "dirty_worktree_reason", lambda wt: "dirty")

    aged = time.time() - 100 * 3600
    os.utime(dirty, (aged, aged))

    stats = cleanup.sweep_extra_paths(
        paths=str(extra_root),
        max_age_hours=48,
        now=time.time(),
    )

    assert stats["removed"] == 0
    assert stats["skipped"] == 1
    assert dirty.exists()


def test_sweep_extra_paths_creates_recovery_ref_for_risky_worktree(cleanup, tmp_path, monkeypatch):
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    risky = extra_root / "risky-wt"
    risky.mkdir()
    (risky / ".git").write_text("placeholder")

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    recovery_calls: list[Path] = []
    monkeypatch.setattr(cleanup, "WORKSPACE", empty_workspace)
    monkeypatch.setattr(cleanup, "dirty_worktree_reason", lambda wt: "ahead-of-upstream")
    monkeypatch.setattr(
        cleanup,
        "create_recovery_ref",
        lambda wt: recovery_calls.append(wt) or "recovery/risky",
    )

    aged = time.time() - 100 * 3600
    os.utime(risky, (aged, aged))

    stats = cleanup.sweep_extra_paths(
        paths=str(extra_root),
        max_age_hours=48,
        now=time.time(),
    )

    assert stats["removed"] == 0
    assert stats["skipped"] == 1
    assert recovery_calls == [risky]
    assert risky.exists()


def test_dirty_worktree_reason_preserves_ahead_branches(cleanup, tmp_path, monkeypatch):
    wt = tmp_path / "ahead-wt"
    wt.mkdir()
    (wt / ".git").write_text("placeholder")

    calls: list[Path] = []
    monkeypatch.setattr(
        cleanup,
        "worktree_risk_reason",
        lambda path: calls.append(path) or "ahead-of-upstream",
    )

    assert cleanup.dirty_worktree_reason(wt) == "ahead-of-upstream"
    assert calls == [wt]


def test_sweep_extra_paths_handles_missing_directory(cleanup, tmp_path):
    stats = cleanup.sweep_extra_paths(
        paths=str(tmp_path / "does-not-exist"),
        max_age_hours=48,
    )
    assert stats == {"removed": 0, "skipped": 0, "freed_mb": 0.0}


def test_sweep_extra_paths_empty_input_is_noop(cleanup):
    stats = cleanup.sweep_extra_paths(paths="", max_age_hours=48)
    assert stats == {"removed": 0, "skipped": 0, "freed_mb": 0.0}
