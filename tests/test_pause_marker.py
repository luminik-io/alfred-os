"""Coverage for the pause-marker honoring helpers in agent_runner.state.

Mirrors the upstream fix: ``alfred pause <agent>`` writes a marker file
at ``$ALFRED_HOME/state/_paused/<codename>``. The bash ``alfred run``
CLI honors that marker, but launchd-spawned firings bypass it. The
``with_lock`` helper now consults the marker before acquiring the
per-agent mutex so every entrypoint respects the pause without
touching per-codename runner scripts.

Tests target the pure-python helpers (is_agent_paused,
write_agent_pause_marker, clear_agent_pause_marker,
reset_consecutive_failures) and the integration via with_lock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    """Point ALFRED_HOME at a clean tmp dir before importing agent_runner."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def test_is_agent_paused_false_when_no_marker():
    import agent_runner as ar

    assert ar.is_agent_paused("lucius") is False


def test_write_and_clear_pause_marker_round_trip():
    import agent_runner as ar

    marker = ar.write_agent_pause_marker("lucius", reason="fail_streak=8")
    assert marker == ar.agent_pause_marker_path("lucius")
    assert marker.is_file()
    assert "fail_streak=8" in marker.read_text()
    assert ar.is_agent_paused("lucius") is True

    assert ar.clear_agent_pause_marker("lucius") is True
    assert ar.is_agent_paused("lucius") is False
    assert ar.clear_agent_pause_marker("lucius") is False


def test_reset_consecutive_failures_zeroes_the_counter():
    import agent_runner as ar

    spend = ar.SpendState("lucius")
    spend.set(consecutive_failures=8, failures_today=8)
    assert ar.SpendState("lucius").state["consecutive_failures"] == 8

    ar.reset_consecutive_failures("lucius")

    reread = ar.SpendState("lucius")
    assert reread.state["consecutive_failures"] == 0
    # Other counters untouched so today's metrics stay intact.
    assert reread.state["failures_today"] == 8


def test_reset_consecutive_failures_noop_when_already_zero():
    """Resume on a healthy agent must not raise; missing spend file is OK."""
    import agent_runner as ar

    ar.reset_consecutive_failures("lucius")


def test_with_lock_exits_when_pause_marker_present():
    """Launchd-bypass fix: with_lock honors the marker without an
    extra runner-side check, so paused agents stop firing even when
    launchd invokes them directly."""
    import agent_runner as ar

    ar.write_agent_pause_marker("lucius", reason="manual pause for test")

    with pytest.raises(SystemExit) as excinfo:
        ar.with_lock("lucius")
    assert excinfo.value.code == 0

    # Marker must still be there post-exit (with_lock does not consume it).
    assert ar.is_agent_paused("lucius") is True
