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
