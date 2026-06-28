"""Focused tests for ``lib.agent_runner.paths``."""

from __future__ import annotations

import re
from pathlib import Path


def test_alfred_home_defaults_to_user_home(fresh_agent_runner, monkeypatch):
    """ALFRED_HOME defaults to ~/.alfred when env var is unset."""
    import sys

    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    import agent_runner

    assert Path("~/.alfred").expanduser() == agent_runner.ALFRED_HOME
    assert Path("~/code").expanduser() == agent_runner.WORKSPACE_ROOT
    assert agent_runner.WORKSPACE == agent_runner.WORKSPACE_ROOT / "product"


def test_state_paths_derive_from_alfred_home(fresh_agent_runner, tmp_path):
    """Derived path constants all live under ALFRED_HOME."""
    ar = fresh_agent_runner
    expected_home = tmp_path / "alfred"
    assert expected_home / "state" == ar.STATE_ROOT
    assert expected_home / "worktrees" == ar.WORKTREE_ROOT
    assert ar.WORKTREES_ROOT == ar.WORKTREE_ROOT  # alias
    assert ar.TRANSCRIPTS_ROOT == ar.STATE_ROOT / "transcripts"
    assert ar.CODEX_TRANSCRIPTS_ROOT == ar.STATE_ROOT / "codex"
    assert expected_home / "prompts" == ar.PROMPTS_ROOT
    assert expected_home / "lib" == ar.LIB_DIR
    assert expected_home / "bin" == ar.BIN_DIR


def test_now_iso_and_today_str_have_expected_shape(fresh_agent_runner):
    """Datetime helpers return well-formed ISO / date strings."""
    ar = fresh_agent_runner
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ar.now_iso())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ar.today_str())


def test_binary_defaults_respect_env(fresh_agent_runner, monkeypatch):
    """CLAUDE_BIN / CODEX_BIN respect env-var overrides on a fresh import."""
    import sys

    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")
    monkeypatch.setenv("CODEX_BIN", "/opt/codex/bin/codex")
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    import agent_runner

    assert agent_runner.CLAUDE_BIN == "/opt/claude/bin/claude"
    assert agent_runner.CODEX_BIN == "/opt/codex/bin/codex"


def test_launcher_env_matches_agent_launch_tilde_home(fresh_agent_runner, monkeypatch, tmp_path):
    """The bash launcher does not expand a literal ``~`` read from rc files."""
    import agent_runner.paths as paths_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    (tmp_path / ".alfredrc").write_text("ALFRED_HOME=~/runtime\n", encoding="utf-8")
    expanded_env = tmp_path / "runtime" / ".env"
    expanded_env.parent.mkdir(parents=True)
    expanded_env.write_text("ALFRED_QUEUE_REPOS=org/expanded\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(tmp_path / "runtime")
    assert env["ALFRED_QUEUE_REPOS"] == "org/expanded"


def test_launcher_env_lets_env_file_repo_scope_override_rc(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    home.mkdir(parents=True)
    (tmp_path / ".alfredrc").write_text(
        f"ALFRED_HOME={home}\nALFRED_SHIPPED_REPOS=org/old\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("ALFRED_SHIPPED_REPOS=org/new\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["ALFRED_SHIPPED_REPOS"] == "org/new"


def test_launcher_env_preserves_real_env_repo_scope_over_env_file(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "org/process")
    home.mkdir(parents=True)
    (tmp_path / ".alfredrc").write_text(
        f"ALFRED_HOME={home}\nALFRED_SHIPPED_REPOS=org/old\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("ALFRED_SHIPPED_REPOS=org/new\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["ALFRED_SHIPPED_REPOS"] == "org/process"


def test_launcher_env_lets_env_file_code_memory_settings_override_rc(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    home.mkdir(parents=True)
    (tmp_path / ".alfredrc").write_text(
        f"ALFRED_HOME={home}\nALFRED_CODE_MEMORY_REPOS=org/old\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("ALFRED_CODE_MEMORY_REPOS=org/new\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["ALFRED_CODE_MEMORY_REPOS"] == "org/new"


def test_launcher_env_preserves_real_env_code_memory_setting_over_env_file(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "org/process")
    home.mkdir(parents=True)
    (tmp_path / ".alfredrc").write_text(
        f"ALFRED_HOME={home}\nALFRED_CODE_MEMORY_REPOS=org/old\n",
        encoding="utf-8",
    )
    (home / ".env").write_text("ALFRED_CODE_MEMORY_REPOS=org/new\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["ALFRED_CODE_MEMORY_REPOS"] == "org/process"


def test_launcher_env_skips_stale_rc_code_memory_settings_for_custom_home(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    other_runtime = tmp_path / "other-runtime"
    home.mkdir()
    runtime.mkdir()
    other_runtime.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    (home / ".alfredrc").write_text(
        f"ALFRED_HOME={other_runtime}\nALFRED_CODE_MEMORY_REPOS=org/stale\n",
        encoding="utf-8",
    )

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(runtime)
    assert "ALFRED_CODE_MEMORY_REPOS" not in env


def test_launcher_env_loads_non_repo_rc_for_custom_home_but_skips_stale_repo_scope(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    (home / ".alfredrc").write_text(
        "\n".join(
            [
                "WORKSPACE_ROOT=$HOME/code space",
                "CLAUDE_CODE_OAUTH_TOKEN=from-rc",
                "ALFRED_QUEUE_REPOS=org/from-rc",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/from-env\n", encoding="utf-8")

    env = paths_mod.launcher_env()

    assert env["WORKSPACE_ROOT"] == f"{home}/code space"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "from-rc"
    assert env["ALFRED_QUEUE_REPOS"] == "org/from-env"


def test_today_str_uses_utc_not_local_time(fresh_agent_runner, monkeypatch):
    """The daily spend ledger filename must rotate at UTC midnight, not at
    the operator's local midnight. Otherwise a non-UTC operator firing
    right after local midnight reads a freshly rotated empty ledger and
    can burn an extra cap's worth of turns before the cap-check catches
    up. Pin the freeze via patching ``datetime`` inside
    ``agent_runner.paths`` (where ``today_str`` resolves it) so the
    assertion holds regardless of the machine's timezone."""
    ar = fresh_agent_runner
    import datetime as _dt

    import agent_runner.paths as paths_mod

    # 2026-05-24 23:30 UTC = 2026-05-25 01:30 in CET (UTC+2). today_str
    # must report 2026-05-24 (the UTC day), not 2026-05-25.
    frozen_utc = _dt.datetime(2026, 5, 24, 23, 30, tzinfo=_dt.UTC)

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # If today_str is still calling datetime.now() without UTC,
                # this returns local time and the date assertion fails loud.
                return frozen_utc.astimezone().replace(tzinfo=None)
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(paths_mod, "datetime", _FrozenDateTime)
    assert ar.today_str() == "2026-05-24"


def test_workspace_subdir_defaults_to_product(fresh_agent_runner):
    """Back-compat: no ``WORKSPACE_SUBDIR`` env var keeps the historical
    ``WORKSPACE_ROOT / product`` shape every existing operator relies on."""
    ar = fresh_agent_runner
    assert ar.WORKSPACE == ar.WORKSPACE_ROOT / "product"


def test_workspace_subdir_overrides_via_env(monkeypatch, tmp_path):
    """Operators with an existing workspace under a different subdir
    (``~/code/src``, ``~/code/repos``, ``~/Claude Workspace``) can rename
    the segment without symlinking. Closes the rigid-layout half of #98."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "src")
    import sys

    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
    import agent_runner

    assert agent_runner.WORKSPACE == agent_runner.WORKSPACE_ROOT / "src"


def test_workspace_subdir_empty_collapses_to_workspace_root(monkeypatch, tmp_path):
    """An empty ``WORKSPACE_SUBDIR`` lets the operator point Alfred at a
    workspace root that already IS the per-repo parent (no intervening
    ``product/`` segment). Matches the ``~/Claude Workspace`` /
    ``~/repos`` shape several Anthropic-onboarding-style setups use."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    import sys

    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
    import agent_runner

    assert agent_runner.WORKSPACE == agent_runner.WORKSPACE_ROOT
