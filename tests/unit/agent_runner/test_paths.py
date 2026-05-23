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
