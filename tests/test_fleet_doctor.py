"""Tests for ``bin/fleet-doctor.py``, read-only health checks.

We import the script as a module so the helper functions are
unit-testable without shelling out. The import path is set up the same
way the runner does at startup.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCTOR = REPO / "bin" / "fleet-doctor.py"


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod in ("slack_format", "fleet_doctor"):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    yield


def _load_doctor():
    """Load the bin/fleet-doctor.py file as a module under the name
    ``fleet_doctor`` (Python won't import a script with a hyphen
    automatically)."""
    spec = importlib.util.spec_from_file_location("fleet_doctor", DOCTOR)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_doctor"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_check_paused_repos_green_when_none():
    fd = _load_doctor()
    result = fd.check_paused_repos()
    assert result.severity == "green"


def test_check_paused_repos_yellow_when_paused(tmp_path):
    import agent_runner as ar

    ar.set_repo_paused("backend", True)
    fd = _load_doctor()
    result = fd.check_paused_repos()
    assert result.severity == "yellow"
    assert "backend" in result.message


def test_check_global_block_green_when_inactive():
    fd = _load_doctor()
    result = fd.check_global_block()
    assert result.severity == "green"


def test_check_stale_worktrees_green_when_no_root():
    fd = _load_doctor()
    result = fd.check_stale_worktrees()
    assert result.severity == "green"


def test_check_stale_worktrees_yellow_for_old_dir(tmp_path):
    import agent_runner as ar

    ar.WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    stale = ar.WORKTREE_ROOT / "eng-lucius-backend-stale"
    stale.mkdir()
    # Force the mtime back >24h.
    old = time.time() - (25 * 3600)
    os.utime(stale, (old, old))
    fd = _load_doctor()
    result = fd.check_stale_worktrees()
    assert result.severity == "yellow"
    assert "stale" in result.message


def test_check_enabled_agents_green_with_missing_file():
    fd = _load_doctor()
    result = fd.check_enabled_agents()
    assert result.severity == "green"
    assert "missing" in result.message.lower()
    assert "own defaults" in result.message


def test_check_enabled_agents_lists_codenames():
    import agent_runner as ar

    ar.enable_agent("batman")
    ar.enable_agent("lucius")
    fd = _load_doctor()
    result = fd.check_enabled_agents()
    assert result.severity == "green"
    assert "batman" in result.message
    assert "lucius" in result.message


def test_check_paused_agents_yellow_for_marker():
    import agent_runner as ar

    pause_dir = ar.STATE_ROOT / "_paused"
    pause_dir.mkdir(parents=True)
    (pause_dir / "lucius").write_text("paused\n")
    fd = _load_doctor()

    result = fd.check_paused_agents()

    assert result.severity == "yellow"
    assert "lucius" in result.message


def test_check_spend_state_alerts_on_failure_streak():
    import agent_runner as ar

    spend_dir = ar.STATE_ROOT / "lucius"
    spend_dir.mkdir(parents=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (spend_dir / f"spend-{today}.json").write_text(
        json.dumps({"consecutive_failures": 8, "failures_today": 8, "successes_today": 0})
    )
    fd = _load_doctor()

    result = fd.check_spend_state()

    assert result.severity == "alert"
    assert "8 consecutive failures" in result.message


def test_check_spend_state_warns_on_active_block():
    import agent_runner as ar

    spend_dir = ar.STATE_ROOT / "drake"
    spend_dir.mkdir(parents=True)
    today = datetime.now().strftime("%Y-%m-%d")
    blocked = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (spend_dir / f"spend-{today}.json").write_text(json.dumps({"blocked_until": blocked}))
    fd = _load_doctor()

    result = fd.check_spend_state()

    assert result.severity == "yellow"
    assert "blocked until" in result.message


def test_overall_severity_worst_wins():
    fd = _load_doctor()
    findings = [
        fd.Finding("a", "green", "ok"),
        fd.Finding("b", "yellow", "warn"),
        fd.Finding("c", "alert", "ah"),
    ]
    assert fd.overall_severity(findings) == "alert"


def test_format_summary_drops_empty_buckets():
    fd = _load_doctor()
    findings = [
        fd.Finding("a", "green", "ok"),
        fd.Finding("b", "green", "fine"),
    ]
    body = fd.format_summary(findings)
    assert "GREEN" in body
    assert "YELLOW" not in body
    assert "ALERT" not in body


def test_doctor_smoke_runs_in_doctor_mode(tmp_path):
    """ALFRED_DOCTOR=1 should print the OK sentinel and exit 0 without
    running the real checks (so a fresh-install doctor.sh probe does
    not mutate state or hit the network)."""
    env = {
        **os.environ,
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "ALFRED_DOCTOR": "1",
    }
    res = subprocess.run(
        [sys.executable, str(DOCTOR)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "[FLEET-DOCTOR-OK]" in res.stdout


# ---------------------------------------------------------------------------
# check_engine_auth_streak — concurrent Anthropic auth failures
# ---------------------------------------------------------------------------
def _write_event(events_dir: Path, firing_id: str, *records: dict) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{firing_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def test_engine_auth_streak_green_when_below_threshold(tmp_path, monkeypatch):
    """Two affected agents is below the default min_agents=3 threshold."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fd = _load_doctor()
    state = fd.STATE_ROOT
    ts = "2026-05-23T16:00:00.000000Z"
    _write_event(
        state / "lucius" / "events",
        "abc",
        {
            "ts": ts,
            "agent": "lucius",
            "event": "firing_complete",
            "subtype": "error_authentication",
            "engine": "claude",
        },
    )
    _write_event(
        state / "drake" / "events",
        "def",
        {
            "ts": ts,
            "agent": "drake",
            "event": "firing_complete",
            "subtype": "error_authentication",
            "engine": "claude",
        },
    )
    finding = fd.check_engine_auth_streak()
    assert finding.severity == "green"


def test_engine_auth_streak_red_when_three_agents_hit(tmp_path, monkeypatch):
    """Three affected agents within the window trips the alert."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fd = _load_doctor()
    state = fd.STATE_ROOT
    now = time.time()
    # Derive ts from `now` so the window check in fleet-doctor (last
    # 1h by default) does not age the event out when the test runs at
    # an arbitrary clock time. The earlier hard-coded literal worked
    # only when the test ran within 1h of 16:30 UTC.
    iso = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for agent in ("lucius", "drake", "bane"):
        events_dir = state / agent / "events"
        path = _write_event(
            events_dir,
            "fid-" + agent,
            {
                "ts": iso,
                "agent": agent,
                "event": "firing_complete",
                "subtype": "error_authentication",
                "engine": "claude",
            },
        )
        os.utime(path, (now, now))
    finding = fd.check_engine_auth_streak(now=now)
    assert finding.severity == "alert"
    assert "Engine auth failing" in finding.message
    assert "lucius" in finding.message
    assert "alfred claude probe" in finding.message


def test_engine_auth_streak_ignores_non_claude_engines(tmp_path, monkeypatch):
    """Codex engine-auth failures must not count toward the Claude streak."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fd = _load_doctor()
    state = fd.STATE_ROOT
    now = time.time()
    # Derive ts from `now` so the window check in fleet-doctor (last
    # 1h by default) does not age the event out when the test runs at
    # an arbitrary clock time. The earlier hard-coded literal worked
    # only when the test ran within 1h of 16:30 UTC.
    iso = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for agent in ("lucius", "drake", "bane"):
        events_dir = state / agent / "events"
        path = _write_event(
            events_dir,
            "fid-" + agent,
            {
                "ts": iso,
                "agent": agent,
                "event": "firing_complete",
                "subtype": "error_authentication",
                "engine": "codex",
            },
        )
        os.utime(path, (now, now))
    finding = fd.check_engine_auth_streak(now=now)
    assert finding.severity == "green"


def test_engine_auth_streak_ignores_other_subtypes(tmp_path, monkeypatch):
    """A rate-limit error with engine=claude must not trigger this check."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fd = _load_doctor()
    state = fd.STATE_ROOT
    now = time.time()
    # Derive ts from `now` so the window check in fleet-doctor (last
    # 1h by default) does not age the event out when the test runs at
    # an arbitrary clock time. The earlier hard-coded literal worked
    # only when the test ran within 1h of 16:30 UTC.
    iso = datetime.fromtimestamp(now, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    for agent in ("lucius", "drake", "bane"):
        events_dir = state / agent / "events"
        path = _write_event(
            events_dir,
            "fid-" + agent,
            {
                "ts": iso,
                "agent": agent,
                "event": "firing_complete",
                "subtype": "error_rate_limit",
                "engine": "claude",
            },
        )
        os.utime(path, (now, now))
    finding = fd.check_engine_auth_streak(now=now)
    assert finding.severity == "green"


def test_engine_auth_streak_window_excludes_old_files(tmp_path, monkeypatch):
    """Files outside the time window must not contribute."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    fd = _load_doctor()
    state = fd.STATE_ROOT
    now = time.time()
    old = now - (3 * 3600)
    iso_old = "2026-05-23T10:00:00.000000Z"
    for agent in ("lucius", "drake", "bane"):
        events_dir = state / agent / "events"
        path = _write_event(
            events_dir,
            "fid-" + agent,
            {
                "ts": iso_old,
                "agent": agent,
                "event": "firing_complete",
                "subtype": "error_authentication",
                "engine": "claude",
            },
        )
        os.utime(path, (old, old))
    finding = fd.check_engine_auth_streak(now=now)
    assert finding.severity == "green"
