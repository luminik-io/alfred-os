"""Focused tests for ``lib.agent_runner.result``."""

from __future__ import annotations


def test_derive_success_stop_reason_wins(fresh_agent_runner):
    """stop_reason in the healthy set forces success=True even on a non-success subtype."""
    ar = fresh_agent_runner
    assert ar._derive_success("error_max_turns", "end_turn") is True
    assert ar._derive_success("success", "error") is False
    # Fallback: when stop_reason is None we lean on the legacy subtype.
    assert ar._derive_success("success", None) is True
    assert ar._derive_success("error_max_turns", None) is False


def test_build_claude_result_reclassifies_error_envelope(fresh_agent_runner):
    """is_error=True with overload markers gets bumped to error_overloaded."""
    ar = fresh_agent_runner
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 4,
        "total_cost_usd": 0.01,
        "result": (
            '{"type":"error","message":{"type":"overloaded_error",'
            '"message":"Anthropic API overloaded"}}'
        ),
    }
    res = ar._build_claude_result(raw)
    assert res.subtype == "error_overloaded"
    assert res.stop_reason == "error"
    assert res.success is False


def test_build_claude_result_happy_path(fresh_agent_runner):
    """A clean success raw event maps to a happy ClaudeResult."""
    ar = fresh_agent_runner
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "num_turns": 12,
        "total_cost_usd": 0.42,
        "session_id": "abc-123",
        "result": "all good",
    }
    res = ar._build_claude_result(raw)
    assert res.success
    assert res.subtype == "success"
    assert res.num_turns == 12
    assert res.cost_usd == 0.42
    assert res.session_id == "abc-123"
    assert res.stop_reason == "end_turn"


def test_dry_run_claude_result_is_synthetic(fresh_agent_runner):
    """dry_run_claude_result is always success, zero-cost, clearly labelled."""
    ar = fresh_agent_runner
    res = ar.dry_run_claude_result("hello", engine="codex")
    assert res.success
    assert res.cost_usd == 0.0
    assert res.session_id == "dry-run-codex-session"
    assert "synthetic codex result" in res.result_text
