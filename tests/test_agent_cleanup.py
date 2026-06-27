"""Coverage for the ``sweep_extra_paths`` helper in bin/agent-cleanup.py.

The OSS cleanup script is procedural (top-level execution); we load it
as a module and target the testable functions inside. ``sweep_extra_paths``
is the new helper from the cleanup-scope-hole fix - it sweeps operator-
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


def test_cleanup_preflight_has_no_runtime_env_requirement(cleanup):
    assert cleanup.PREFLIGHT.env_vars == []
    assert cleanup.PREFLIGHT.bins == ["git"]
    assert cleanup.PREFLIGHT.check_disk is False


def _exec_cleanup(tmp_path, monkeypatch, *, argv, home):
    """Run the procedural cleanup body once with a controlled env + HOME."""
    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    (workspace / "product").mkdir(parents=True)
    (alfred / "state").mkdir(parents=True)
    (alfred / "worktrees").mkdir(parents=True)
    monkeypatch.setenv("ALFRED_HOME", str(alfred))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("ALFRED_CLEANUP_EXTRA_PATHS", raising=False)
    monkeypatch.setenv("ALFRED_CLAIM_SWEEP_REPOS", "")
    monkeypatch.setenv("ALFRED_SLACK_WEBHOOK_URL", "")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod == "agent_cleanup":
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    spec = importlib.util.spec_from_file_location("agent_cleanup", CLEANUP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_cleanup"] = mod
    with contextlib.suppress(SystemExit):
        spec.loader.exec_module(mod)
    return mod


def _seed_dev_caches(home):
    derived = home / "Library" / "Developer" / "Xcode" / "DerivedData" / "App-abc"
    derived.mkdir(parents=True)
    (derived / "build.o").write_bytes(b"x" * 4096)
    npm_cache = home / ".npm" / "_cacache" / "content-v2"
    npm_cache.mkdir(parents=True)
    (npm_cache / "blob").write_bytes(b"y" * 4096)


def test_emergency_reclaims_dev_caches(tmp_path, monkeypatch):
    """--emergency clears regenerable Xcode DerivedData + npm cache under HOME."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", raising=False)
    home = tmp_path / "home"
    _seed_dev_caches(home)
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    assert not (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert not (home / ".npm" / "_cacache").exists()
    assert mod.dev_caches_cleared == 2
    assert mod.dev_cache_freed_mb > 0


def test_emergency_dev_cache_skips_symlinks_but_still_clears(tmp_path, monkeypatch):
    """A symlink inside a cache is not counted and never blocks the rmtree."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", raising=False)
    home = tmp_path / "home"
    derived = home / "Library" / "Developer" / "Xcode" / "DerivedData" / "App"
    derived.mkdir(parents=True)
    (derived / "real.o").write_bytes(b"x" * 4096)
    external = tmp_path / "outside-target.bin"
    external.write_bytes(b"z" * 1_000_000)
    (derived / "link").symlink_to(external)
    (home / ".npm" / "_cacache").mkdir(parents=True)

    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)

    # DerivedData is removed despite containing a symlink...
    assert not (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    # ...the symlink target outside the cache is untouched...
    assert external.exists()
    # ...and the freed count reflects only the real file, not the 1 MB target.
    assert mod.dev_cache_freed_mb < 0.5


def test_non_emergency_leaves_dev_caches_untouched(tmp_path, monkeypatch):
    """A normal (non-emergency) sweep never touches machine-wide dev caches."""
    monkeypatch.delenv("ALFRED_CLEANUP_SCHEDULED_RECLAIM", raising=False)
    home = tmp_path / "home"
    _seed_dev_caches(home)
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py"], home=home)
    assert (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert (home / ".npm" / "_cacache").exists()
    assert mod.dev_caches_cleared == 0


def test_emergency_skip_env_preserves_dev_caches(tmp_path, monkeypatch):
    """ALFRED_EMERGENCY_SKIP_DEV_CACHES=1 opts out even under --emergency."""
    monkeypatch.setenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", "1")
    home = tmp_path / "home"
    _seed_dev_caches(home)
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    assert (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert (home / ".npm" / "_cacache").exists()
    assert mod.dev_caches_cleared == 0


def _patch_docker(
    monkeypatch,
    *,
    present,
    stdout,
    version_stdout="24.0.0\n",
    version_returncode=0,
):
    """Route docker prune calls to a fake while leaving real subprocess intact.

    The procedural cleanup body runs ``subprocess.run`` for git/worktree work
    during module exec, so we only intercept ``docker`` invocations and
    delegate everything else to the genuine implementation.
    """
    real_run = subprocess.run
    real_which = __import__("shutil").which
    calls: list[list[str]] = []

    def fake_which(name, *args, **kwargs):
        if name == "docker":
            return "/usr/bin/docker" if present else None
        return real_which(name, *args, **kwargs)

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]).endswith("docker"):
            calls.append(list(cmd))
            if len(cmd) > 1 and cmd[1] == "version":
                return subprocess.CompletedProcess(
                    cmd,
                    version_returncode,
                    stdout=version_stdout,
                    stderr="",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("shutil.which", fake_which)
    monkeypatch.setattr("subprocess.run", fake_run)
    return calls


def test_emergency_reclaims_docker_and_sums(tmp_path, monkeypatch):
    """--emergency runs the 3 safe docker prunes and sums their reclaimed space."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    assert mod.dock_n == 3
    assert mod.dock_freed_mb == pytest.approx(1536.0 * 3, rel=1e-3)
    assert [c[1:] for c in calls] == [
        ["builder", "prune", "-f"],
        ["image", "prune", "-f"],
        ["volume", "prune", "--all", "-f"],
    ]
    for cmd in calls:
        assert "container" not in cmd
        assert "-a" not in cmd


def test_emergency_docker_absent_reclaims_nothing(tmp_path, monkeypatch):
    """When the docker binary is absent, no prune runs and nothing is reclaimed."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=False, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    assert mod.dock_n == 0
    assert mod.dock_freed_mb == 0.0
    assert calls == []


def test_emergency_skip_env_preserves_docker(tmp_path, monkeypatch):
    """ALFRED_EMERGENCY_SKIP_DOCKER=1 opts out even under --emergency."""
    monkeypatch.setenv("ALFRED_EMERGENCY_SKIP_DOCKER", "1")
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    assert mod.dock_n == 0
    assert mod.dock_freed_mb == 0.0
    assert calls == []


def test_non_emergency_leaves_docker_untouched(tmp_path, monkeypatch):
    """A normal (non-emergency) sweep never runs docker prunes."""
    monkeypatch.delenv("ALFRED_CLEANUP_SCHEDULED_RECLAIM", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py"], home=home)
    assert mod.dock_n == 0
    assert mod.dock_freed_mb == 0.0
    assert calls == []


def test_scheduled_flag_reclaims_dev_caches_without_emergency(tmp_path, monkeypatch):
    """--scheduled reclaims regenerable dev caches on a normal (non-emergency) pass."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", raising=False)
    home = tmp_path / "home"
    _seed_dev_caches(home)
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--scheduled"], home=home)
    assert not (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert not (home / ".npm" / "_cacache").exists()
    assert mod.dev_caches_cleared == 2
    assert mod.dev_cache_freed_mb > 0
    # --scheduled must NOT flip the aggressive emergency thresholds on.
    assert mod.EMERGENCY is False
    assert mod.SCHEDULED_RECLAIM is True


def test_scheduled_env_reclaims_dev_caches_without_flag(tmp_path, monkeypatch):
    """ALFRED_CLEANUP_SCHEDULED_RECLAIM=1 opts the daily pass in with no flag."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", raising=False)
    monkeypatch.setenv("ALFRED_CLEANUP_SCHEDULED_RECLAIM", "1")
    home = tmp_path / "home"
    _seed_dev_caches(home)
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py"], home=home)
    assert not (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert mod.dev_caches_cleared == 2
    assert mod.SCHEDULED_RECLAIM is True


def test_scheduled_flag_reclaims_docker_without_emergency(tmp_path, monkeypatch):
    """--scheduled runs safe Docker prunes, with anonymous-only volumes on 23+.

    Unlike --emergency it must not pass --all to volume prune, since that also
    removes named orphaned volumes which can hold local dev data.
    """
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--scheduled"], home=home)
    assert mod.dock_n == 3
    assert [c[1:] for c in calls if "prune" in c] == [
        ["builder", "prune", "-f"],
        ["image", "prune", "-f"],
        ["volume", "prune", "-f"],
    ]
    assert [c[1:] for c in calls if len(c) > 1 and c[1] == "version"] == [
        ["version", "--format", "{{.Server.Version}}"]
    ]
    for cmd in calls:
        assert "--all" not in cmd
        assert "container" not in cmd


def test_scheduled_old_docker_skips_volume_prune(tmp_path, monkeypatch):
    """Older Docker bare volume prune can delete named volumes, so skip it."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(
        monkeypatch,
        present=True,
        stdout="Total reclaimed space: 1.5GB\n",
        version_stdout="20.10.24\n",
    )
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--scheduled"], home=home)
    assert mod.dock_n == 2
    assert [c[1:] for c in calls if "prune" in c] == [
        ["builder", "prune", "-f"],
        ["image", "prune", "-f"],
    ]


def test_scheduled_unknown_docker_version_skips_volume_prune(tmp_path, monkeypatch):
    """If the server version cannot be proven safe, scheduled skips volume prune."""
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(
        monkeypatch,
        present=True,
        stdout="Total reclaimed space: 1.5GB\n",
        version_stdout="",
        version_returncode=1,
    )
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--scheduled"], home=home)
    assert mod.dock_n == 2
    assert [c[1:] for c in calls if "prune" in c] == [
        ["builder", "prune", "-f"],
        ["image", "prune", "-f"],
    ]


def test_scheduled_respects_skip_envs(tmp_path, monkeypatch):
    """The existing skip envs opt out of the scheduled reclaim too."""
    monkeypatch.setenv("ALFRED_EMERGENCY_SKIP_DEV_CACHES", "1")
    monkeypatch.setenv("ALFRED_EMERGENCY_SKIP_DOCKER", "1")
    home = tmp_path / "home"
    _seed_dev_caches(home)
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 1.5GB\n")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--scheduled"], home=home)
    assert (home / "Library" / "Developer" / "Xcode" / "DerivedData").exists()
    assert mod.dev_caches_cleared == 0
    assert mod.dock_n == 0
    assert calls == []


def test_volume_prune_uses_all_for_named_orphans(tmp_path, monkeypatch):
    """The volume prune must pass --all so named orphaned volumes are reclaimed.

    Since Docker 23.0 a bare ``docker volume prune -f`` removes only anonymous
    volumes; named orphaned volumes (the ~2.3 GB that caused the incident) are
    left on disk. --all restores the pre-23.0 reclaim-everything behavior.
    """
    monkeypatch.delenv("ALFRED_EMERGENCY_SKIP_DOCKER", raising=False)
    home = tmp_path / "home"
    calls = _patch_docker(monkeypatch, present=True, stdout="Total reclaimed space: 2.3GB\n")
    _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py", "--emergency"], home=home)
    volume_cmds = [c[1:] for c in calls if "volume" in c]
    assert volume_cmds == [["volume", "prune", "--all", "-f"]]


def test_parse_docker_reclaimed_mb_handles_lowercase_kb(tmp_path, monkeypatch):
    """Docker reports kilobytes with a lowercase ``k`` (e.g. ``820.4kB``).

    The parser must match the lowercase unit and convert it via the matching
    ``"k"`` factor instead of returning 0.0 or raising KeyError.
    """
    home = tmp_path / "home"
    _patch_docker(monkeypatch, present=False, stdout="")
    mod = _exec_cleanup(tmp_path, monkeypatch, argv=["agent-cleanup.py"], home=home)
    assert mod._parse_docker_reclaimed_mb("Total reclaimed space: 820.4kB\n") == pytest.approx(
        820.4 / 1024, rel=1e-6
    )
    # Uppercase K stays equivalent, and the empty (bare-byte) unit is unchanged.
    assert mod._parse_docker_reclaimed_mb("Total reclaimed space: 820.4KB\n") == pytest.approx(
        820.4 / 1024, rel=1e-6
    )
    assert mod._parse_docker_reclaimed_mb("Total reclaimed space: 0B\n") == 0.0


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


# ---------------------------------------------------------------------------
# Auto-discovery of .worktrees pools under WORKSPACE (the incident fix).
# ---------------------------------------------------------------------------


def test_discover_worktree_pools_finds_dot_worktrees(cleanup, tmp_path):
    workspace = tmp_path / "ws"
    # A per-project pool nested two levels under WORKSPACE.
    pool = workspace / "product" / "backend" / ".worktrees"
    pool.mkdir(parents=True)
    (pool / "feat-x").mkdir()
    # A noise directory that must NOT be reported.
    (workspace / "product" / "frontend" / "node_modules").mkdir(parents=True)

    found = cleanup.discover_worktree_pools(root=workspace)
    assert found == [str(pool)]


def test_discover_worktree_pools_respects_depth_bound(cleanup, tmp_path):
    workspace = tmp_path / "ws"
    # Pool buried deeper than the default max_depth=3 must be skipped.
    deep = workspace / "a" / "b" / "c" / "d" / ".worktrees"
    deep.mkdir(parents=True)
    found = cleanup.discover_worktree_pools(root=workspace, max_depth=3)
    assert found == []


def test_discover_worktree_pools_depth_bound_is_inclusive(cleanup, tmp_path):
    """The bound is ``<= max_depth`` (matching the docstring): a pool at
    exactly ``max_depth`` is discovered; one level deeper is not.

    root is depth 0, so ``a/b/.worktrees`` puts ``.worktrees`` at depth 3,
    which must be found with ``max_depth=3``; ``a/b/c/.worktrees`` (depth 4)
    must not.
    """
    at_bound = tmp_path / "ws_ok"
    (at_bound / "a" / "b" / ".worktrees").mkdir(parents=True)
    assert cleanup.discover_worktree_pools(root=at_bound, max_depth=3) == [
        str(at_bound / "a" / "b" / ".worktrees")
    ]

    one_too_deep = tmp_path / "ws_deep"
    (one_too_deep / "a" / "b" / "c" / ".worktrees").mkdir(parents=True)
    assert cleanup.discover_worktree_pools(root=one_too_deep, max_depth=3) == []


def test_discover_worktree_pools_missing_root_is_empty(cleanup, tmp_path):
    found = cleanup.discover_worktree_pools(root=tmp_path / "nope")
    assert found == []


def test_discover_does_not_descend_into_node_modules(cleanup, tmp_path):
    workspace = tmp_path / "ws"
    # A .worktrees that only exists *inside* a node_modules tree must not
    # be discovered - we never walk into node_modules.
    buried = workspace / "repo" / "node_modules" / "pkg" / ".worktrees"
    buried.mkdir(parents=True)
    found = cleanup.discover_worktree_pools(root=workspace)
    assert found == []


# ---------------------------------------------------------------------------
# Hard safety rule: never touch non-Alfred paths; always skip dirty.
# ---------------------------------------------------------------------------


def test_emergency_sweep_never_touches_user_files(cleanup, tmp_path, monkeypatch):
    """A clean, old worktree in a discovered pool is removed; a sibling
    user file/dir that is NOT a worktree child is left completely alone."""
    pool = tmp_path / "ws" / "proj" / ".worktrees"
    pool.mkdir(parents=True)
    old_wt = pool / "old-feat"
    old_wt.mkdir()
    (old_wt / ".git").write_text("placeholder")

    # A user file sitting OUTSIDE the pool, e.g. real source the operator
    # cares about. Nothing in the sweep should ever reach it.
    user_src = tmp_path / "ws" / "proj" / "important.txt"
    user_src.write_text("do not delete me")
    user_cache = tmp_path / "home" / ".npm"
    user_cache.mkdir(parents=True)
    (user_cache / "cache.bin").write_text("package cache")

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    monkeypatch.setattr(cleanup, "WORKSPACE", empty_workspace)
    monkeypatch.setattr(cleanup, "dirty_worktree_reason", lambda wt: None)

    aged = time.time() - 100 * 3600
    os.utime(old_wt, (aged, aged))

    stats = cleanup.sweep_extra_paths(paths=str(pool), max_age_hours=1, now=time.time())

    assert stats["removed"] == 1
    assert not old_wt.exists()  # the worktree child WAS reclaimed
    assert user_src.exists() and user_src.read_text() == "do not delete me"
    assert user_cache.exists() and (user_cache / "cache.bin").exists()


def test_emergency_sweep_still_skips_dirty_in_discovered_pool(cleanup, tmp_path, monkeypatch):
    """Even at the aggressive 1h emergency age threshold, a dirty worktree
    in an auto-discovered pool is preserved with a recovery ref."""
    pool = tmp_path / "ws" / "proj" / ".worktrees"
    pool.mkdir(parents=True)
    dirty = pool / "wip-feat"
    dirty.mkdir()
    (dirty / ".git").write_text("placeholder")

    empty_workspace = tmp_path / "empty-workspace"
    empty_workspace.mkdir()
    recovery_calls: list = []
    monkeypatch.setattr(cleanup, "WORKSPACE", empty_workspace)
    monkeypatch.setattr(cleanup, "dirty_worktree_reason", lambda wt: "dirty")
    monkeypatch.setattr(
        cleanup,
        "create_recovery_ref",
        lambda wt: recovery_calls.append(wt) or "recovery/wip",
    )

    aged = time.time() - 100 * 3600
    os.utime(dirty, (aged, aged))

    stats = cleanup.sweep_extra_paths(paths=str(pool), max_age_hours=1, now=time.time())

    assert stats["removed"] == 0
    assert stats["skipped"] == 1
    assert dirty.exists()  # dirty work preserved despite emergency thresholds
    assert recovery_calls == [dirty]


# ---------------------------------------------------------------------------
# Emergency-mode end-to-end: aggressive thresholds via the procedural body.
# ---------------------------------------------------------------------------


def test_emergency_run_uses_aggressive_thresholds(tmp_path, monkeypatch):
    """Loading the procedural body with --emergency must clear an Alfred
    /tmp debug dir that is NEWER than the 1-day gate a normal run honours."""
    import importlib.util

    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    (workspace / "product").mkdir(parents=True)
    (alfred / "state").mkdir(parents=True)
    (alfred / "worktrees").mkdir(parents=True)

    monkeypatch.setenv("ALFRED_HOME", str(alfred))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ALFRED_CLAIM_SWEEP_REPOS", "")
    monkeypatch.delenv("ALFRED_CLEANUP_EXTRA_PATHS", raising=False)
    # Disable autodiscovery so this test only exercises the /tmp age gate.
    monkeypatch.setenv("ALFRED_CLEANUP_AUTODISCOVER", "0")

    # A fresh Alfred-owned /tmp debug dir (mtime = now). A normal run would
    # skip it (under the 1-day gate); emergency must clear it. The prefix
    # must match a real bin/*.py stem so configured_tmp_prefixes() sweeps
    # it - ``lucius`` (bin/lucius.py) is one such Alfred-owned agent.
    fresh_debug = Path("/tmp") / "lucius-debug-emergencytest-xyz"
    fresh_debug.mkdir(exist_ok=True)
    (fresh_debug / "scratch").write_text("x")
    try:
        for mod in list(sys.modules):
            if mod.startswith("agent_runner") or mod == "agent_cleanup":
                del sys.modules[mod]
        sys.path.insert(0, str(REPO / "lib"))
        monkeypatch.setattr(sys, "argv", ["agent-cleanup.py", "--emergency"])

        spec = importlib.util.spec_from_file_location("agent_cleanup", CLEANUP)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["agent_cleanup"] = mod
        with contextlib.suppress(SystemExit):
            spec.loader.exec_module(mod)

        assert mod.EMERGENCY is True
        # Emergency dropped the /tmp age gate to 0 and shortened retention.
        assert mod.TMP_MIN_AGE_DAYS == 0.0
        assert mod.TRANSCRIPT_RETENTION_DAYS <= 3
        assert mod.EVENTS_RETENTION_DAYS <= 3
        # The fresh debug dir was cleared despite being well under 1 day old.
        assert not fresh_debug.exists()
    finally:
        if fresh_debug.exists():
            import shutil as _shutil

            _shutil.rmtree(fresh_debug, ignore_errors=True)


def test_fleet_worktree_reclamation_is_counted_in_total(tmp_path, monkeypatch):
    """The fleet-pool sweep rmtrees abandoned worktrees; the bytes it
    reclaims must be reflected in both the per-line worktree report and
    the grand ``total reclaimed`` figure (previously the fleet sweep
    freed space silently, so the total under-reported).
    """
    import importlib.util

    alfred = tmp_path / "alfred"
    workspace = tmp_path / "workspace"
    (workspace / "product").mkdir(parents=True)
    (alfred / "state").mkdir(parents=True)
    fleet_pool = alfred / "worktrees"
    fleet_pool.mkdir(parents=True)

    # An abandoned-but-clean fleet worktree with a known, non-trivial size.
    old_wt = fleet_pool / "old-feat"
    old_wt.mkdir()
    (old_wt / ".git").write_text("placeholder")
    (old_wt / "blob.bin").write_bytes(b"x" * (3 * 1024 * 1024))  # 3 MB
    aged = time.time() - 10 * 3600  # older than the 2h fleet gate
    os.utime(old_wt, (aged, aged))

    monkeypatch.setenv("ALFRED_HOME", str(alfred))
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ALFRED_CLAIM_SWEEP_REPOS", "")
    monkeypatch.delenv("ALFRED_CLEANUP_EXTRA_PATHS", raising=False)
    monkeypatch.setenv("ALFRED_CLEANUP_AUTODISCOVER", "0")
    monkeypatch.setenv("ALFRED_SLACK_WEBHOOK_URL", "")

    for mod_name in list(sys.modules):
        if mod_name.startswith("agent_runner") or mod_name == "agent_cleanup":
            del sys.modules[mod_name]
    sys.path.insert(0, str(REPO / "lib"))

    # Import agent_runner first and force this clean worktree to look safe:
    # the cleanup body does ``from agent_runner import worktree_risk_reason``
    # at load, so patching it here means the body binds our stub. A real
    # clean worktree would need an origin/main ref to pass the ahead-check.
    import agent_runner

    monkeypatch.setattr(agent_runner, "worktree_risk_reason", lambda wt, **kw: None)

    monkeypatch.setattr(sys, "argv", ["agent-cleanup.py"])
    spec = importlib.util.spec_from_file_location("agent_cleanup", CLEANUP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_cleanup"] = mod
    with contextlib.suppress(SystemExit):
        spec.loader.exec_module(mod)

    assert mod.wt_removed == 1
    assert not old_wt.exists()
    # The 3 MB worktree was measured and counted (allow slack for dir
    # overhead / fs rounding).
    assert mod.wt_freed_mb >= 2.9, f"wt_freed_mb={mod.wt_freed_mb}"
    # And the grand total includes the fleet reclamation.
    assert mod.total_freed_mb >= mod.wt_freed_mb
    assert mod.total_freed_mb >= 2.9
