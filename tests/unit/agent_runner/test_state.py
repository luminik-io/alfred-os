"""Focused tests for ``lib.agent_runner.state``."""

from __future__ import annotations

import json


def test_event_log_appends_jsonl(fresh_agent_runner, tmp_path):
    """EventLog writes one typed, sequenced JSON-per-line record with auto
    timestamp + firing id. Event types are validated against the closed set, so
    these use real members; the legacy top-level ``event`` field is preserved."""
    ar = fresh_agent_runner
    log = ar.EventLog(agent="lucius", firing_id="test-1", path=tmp_path / "events.jsonl")
    log.emit("firing_started")
    log.emit("issue_picked", number=42)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    # Legacy ``event`` field (== ``type``) is preserved for existing consumers.
    assert first["event"] == "firing_started"
    assert first["type"] == "firing_started"
    assert first["agent"] == "lucius"
    assert first["firing_id"] == "test-1"
    assert "ts" in first
    # Monotonic per-firing seq stamped at append time.
    assert first["seq"] == 1
    assert json.loads(lines[1])["seq"] == 2


def test_spend_state_increment_persists(fresh_agent_runner):
    """SpendState.increment writes counters atomically through to disk."""
    ar = fresh_agent_runner
    spend = ar.SpendState(agent="lucius")
    spend.increment(firings_today=1, turns_today=12)
    spend2 = ar.SpendState(agent="lucius")  # reload from disk
    assert spend2.state["firings_today"] == 1
    assert spend2.state["turns_today"] == 12


def test_global_block_set_and_clear(fresh_agent_runner):
    """set_global_block writes the ledger; is_globally_blocked reads it."""
    ar = fresh_agent_runner
    assert ar.is_globally_blocked() is None
    ar.set_global_block(hours=1, reason="test")
    reason = ar.is_globally_blocked()
    assert reason is not None
    assert "test" in reason


def test_enable_disable_agent(fresh_agent_runner):
    """enable / disable round-trip through FLEET_ENABLED_FILE."""
    ar = fresh_agent_runner
    assert ar.list_enabled_agents() == []
    ar.enable_agent("lucius")
    ar.enable_agent("nightwing")
    assert sorted(ar.list_enabled_agents()) == ["lucius", "nightwing"]
    ar.disable_agent("lucius")
    assert ar.list_enabled_agents() == ["nightwing"]


def test_maybe_set_global_block_only_for_provider_limit(fresh_agent_runner):
    """maybe_set_global_block_for_result only fires for provider-limit subtypes."""
    ar = fresh_agent_runner

    class _R:
        subtype = "success"

    assert ar.maybe_set_global_block_for_result("lucius", _R()) is None

    class _R2:
        subtype = "error_rate_limit"

    until = ar.maybe_set_global_block_for_result("lucius", _R2())
    assert until is not None


def test_agent_lock_acquire_release(fresh_agent_runner, tmp_path, monkeypatch):
    """AgentLock acquires once and writes a pid+metadata file."""
    ar = fresh_agent_runner
    lock = ar.AgentLock("lucius")
    lock._lock_dir = tmp_path / "lock-lucius"
    monkeypatch.setattr(ar, "pid_start_key", lambda pid: "start-x")
    try:
        assert lock.acquire() is True
        assert (lock._lock_dir / "pid").exists()
        meta = json.loads((lock._lock_dir / "metadata.json").read_text())
        assert meta["agent"] == "lucius"
        assert meta["pid_start_key"] == "start-x"
    finally:
        lock.release()
    assert not lock._lock_dir.exists()
