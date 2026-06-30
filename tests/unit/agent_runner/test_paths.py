"""Focused tests for ``lib.agent_runner.paths``."""

from __future__ import annotations

import re
from pathlib import Path


def test_alfred_home_defaults_to_user_home(fresh_agent_runner, monkeypatch):
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
    ar = fresh_agent_runner
    expected_home = tmp_path / "alfred"
    assert expected_home / "state" == ar.STATE_ROOT
    assert expected_home / "worktrees" == ar.WORKTREE_ROOT
    assert ar.WORKTREES_ROOT == ar.WORKTREE_ROOT
    assert expected_home / "state" / "transcripts" == ar.TRANSCRIPTS_ROOT
    assert expected_home / "state" / "codex" == ar.CODEX_TRANSCRIPTS_ROOT
    assert expected_home / "prompts" == ar.PROMPTS_ROOT
    assert expected_home / "lib" == ar.LIB_DIR
    assert expected_home / "bin" == ar.BIN_DIR


def test_now_iso_and_today_str_have_expected_shape(fresh_agent_runner):
    ar = fresh_agent_runner
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ar.now_iso())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", ar.today_str())


def test_binary_defaults_respect_env(fresh_agent_runner, monkeypatch):
    import sys

    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")
    monkeypatch.setenv("CODEX_BIN", "/opt/codex/bin/codex")
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    import agent_runner

    assert agent_runner.CLAUDE_BIN == "/opt/claude/bin/claude"
    assert agent_runner.CODEX_BIN == "/opt/codex/bin/codex"


def test_launcher_env_loads_runtime_env_from_process_home(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/runtime\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(runtime)
    assert env["ALFRED_QUEUE_REPOS"] == "org/runtime"


def test_launcher_env_expands_process_alfred_home_before_env_file(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "home"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/expanded\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", "~/runtime")
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(runtime)
    assert env["ALFRED_QUEUE_REPOS"] == "org/expanded"


def test_launcher_env_treats_empty_process_home_as_absent(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    home = tmp_path / "home"
    runtime = home / ".alfred"
    runtime.mkdir(parents=True)
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/default\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", "")
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(runtime)
    assert env["ALFRED_QUEUE_REPOS"] == "org/default"


def test_launcher_env_ignores_legacy_alfredrc_file(fresh_agent_runner, monkeypatch, tmp_path):
    import agent_runner.paths as paths_mod

    home = tmp_path / "home"
    runtime = home / ".alfred"
    runtime.mkdir(parents=True)
    (home / ".alfredrc").write_text("ALFRED_QUEUE_REPOS=org/legacy\n", encoding="utf-8")
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/env\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert "ALFREDRC" not in env
    assert env["ALFRED_HOME"] == str(runtime)
    assert env["ALFRED_QUEUE_REPOS"] == "org/env"


def test_launcher_env_ignores_legacy_alfredrc_pointer_env(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_QUEUE_REPOS=org/legacy\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("ALFREDRC", str(custom_rc))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_HOME"] == str(runtime)
    assert "ALFREDRC" not in env
    assert "ALFRED_QUEUE_REPOS" not in env


def test_launcher_env_strips_inline_comments_and_quotes(fresh_agent_runner, monkeypatch, tmp_path):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text(
        'ALFRED_QUEUE_REPOS="org/#quoted" # human note\n'
        "ALFRED_CODE_MEMORY_REPOS=org/memory # human note\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_QUEUE_REPOS"] == "org/#quoted"
    assert env["ALFRED_CODE_MEMORY_REPOS"] == "org/memory"


def test_launcher_env_preserves_real_env_repo_scope_over_env_file(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_SHIPPED_REPOS=org/env\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "org/process")

    env = paths_mod.launcher_env()

    assert env["ALFRED_SHIPPED_REPOS"] == "org/process"


def test_launcher_env_loads_code_memory_settings_when_process_absent(
    fresh_agent_runner, monkeypatch, tmp_path
):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    memory_home = tmp_path / "memory"
    runtime.mkdir()
    (runtime / ".env").write_text(
        f"ALFRED_CODE_MEMORY_HOME={memory_home}\nALFRED_CODE_MEMORY_DISCOVERY_LIMIT=9\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_CODE_MEMORY_HOME", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_DISCOVERY_LIMIT", raising=False)

    env = paths_mod.launcher_env()

    assert env["ALFRED_CODE_MEMORY_HOME"] == str(memory_home)
    assert env["ALFRED_CODE_MEMORY_DISCOVERY_LIMIT"] == "9"


def test_config_value_preserves_empty_runtime_value(fresh_agent_runner, monkeypatch, tmp_path):
    import agent_runner.paths as paths_mod

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_SHIPPED_REPOS=\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)

    assert paths_mod.config_value("ALFRED_SHIPPED_REPOS", "fallback") == ""


def test_today_str_uses_utc_not_local_time(fresh_agent_runner, monkeypatch):
    ar = fresh_agent_runner
    import datetime as _dt

    import agent_runner.paths as paths_mod

    frozen_utc = _dt.datetime(2026, 5, 24, 23, 30, tzinfo=_dt.UTC)

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_utc.astimezone().replace(tzinfo=None)
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(paths_mod, "datetime", _FrozenDateTime)
    assert ar.today_str() == "2026-05-24"


def test_workspace_subdir_defaults_to_product(fresh_agent_runner):
    ar = fresh_agent_runner
    assert ar.WORKSPACE == ar.WORKSPACE_ROOT / "product"
