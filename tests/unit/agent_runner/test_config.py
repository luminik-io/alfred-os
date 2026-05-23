"""Focused tests for ``lib.agent_runner.config``."""

from __future__ import annotations


def test_env_int_clamps_to_range(fresh_agent_runner, monkeypatch):
    """env_int clamps both the env value and the fallback to the range."""
    ar = fresh_agent_runner
    monkeypatch.setenv("FOO", "1000")
    assert ar.env_int("FOO", default=5, minimum=1, maximum=10) == 10
    monkeypatch.setenv("FOO", "garbage")
    assert ar.env_int("FOO", default=7, minimum=1, maximum=10) == 7
    monkeypatch.delenv("FOO", raising=False)
    assert ar.env_int("FOO", default=99, minimum=1, maximum=10) == 10


def test_optional_env_int_returns_none_when_unset(fresh_agent_runner, monkeypatch):
    """optional_env_int returns None for missing/unparseable, otherwise clamped int."""
    ar = fresh_agent_runner
    monkeypatch.delenv("BAR", raising=False)
    assert ar.optional_env_int("BAR") is None
    monkeypatch.setenv("BAR", "not-an-int")
    assert ar.optional_env_int("BAR") is None
    monkeypatch.setenv("BAR", "5")
    assert ar.optional_env_int("BAR", minimum=10) == 10


def test_normalize_engine_legacy_both_alias(fresh_agent_runner):
    """The legacy ``both`` alias collapses to ``hybrid``."""
    ar = fresh_agent_runner
    assert ar.normalize_engine("both") == "hybrid"
    assert ar.normalize_engine("CODEX") == "codex"
    assert ar.normalize_engine("garbage", default="codex") == "codex"
    assert ar.normalize_engine(None) == "hybrid"


def test_agent_engine_env_precedence(fresh_agent_runner, monkeypatch):
    """ALFRED_<AGENT>_ENGINE wins over fleet-wide ALFRED_ENGINE."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_ENGINE", "codex")
    monkeypatch.setenv("ALFRED_LUCIUS_ENGINE", "claude")
    assert ar.agent_engine("lucius") == "claude"


def test_engine_preflight_bins_modes(fresh_agent_runner):
    """codex needs codex; hybrid defaults to claude-only; opt-in adds codex."""
    ar = fresh_agent_runner
    assert ar.engine_preflight_bins("codex") == [ar.CODEX_BIN]
    assert ar.engine_preflight_bins("hybrid") == [ar.CLAUDE_BIN]
    assert ar.engine_preflight_bins("hybrid", hybrid_requires_codex=True) == [
        ar.CLAUDE_BIN,
        ar.CODEX_BIN,
    ]


def test_doctor_mode_truthy_env(fresh_agent_runner, monkeypatch):
    """doctor_mode honours common truthy strings."""
    ar = fresh_agent_runner
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    assert not ar.doctor_mode()
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    assert ar.doctor_mode()
    monkeypatch.setenv("ALFRED_DOCTOR", "0")
    assert not ar.doctor_mode()


def test_dry_run_toggle(fresh_agent_runner, monkeypatch):
    """set_dry_run writes the env var; is_dry_run picks it up."""
    ar = fresh_agent_runner
    monkeypatch.delenv("ALFRED_DRY_RUN", raising=False)
    assert not ar.is_dry_run()
    ar.set_dry_run(True)
    assert ar.is_dry_run()
    ar.set_dry_run(False)
    assert not ar.is_dry_run()


def test_codex_sandbox_for_agent_precedence(fresh_agent_runner, monkeypatch):
    """ALFRED_<AGENT>_CODEX_SANDBOX > legacy alias > CODEX_WRITE > default."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_LUCIUS_CODEX_WRITE", "1")
    assert ar.codex_sandbox_for_agent("lucius") == "workspace-write"
    monkeypatch.setenv("LUCIUS_CODEX_SANDBOX", "danger-full-access")
    assert ar.codex_sandbox_for_agent("lucius") == "danger-full-access"
    monkeypatch.setenv("ALFRED_LUCIUS_CODEX_SANDBOX", "read-only")
    assert ar.codex_sandbox_for_agent("lucius") == "read-only"
