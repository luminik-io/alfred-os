"""Tests for ``bin/fleet-doctor.py`` — read-only health checks.

We import the script as a module so the helper functions are
unit-testable without shelling out. The import path is set up the same
way the runner does at startup.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCTOR = REPO / "bin" / "fleet-doctor.py"


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
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
    assert "missing" in result.message.lower() or "default-enabled" in result.message


def test_check_enabled_agents_lists_codenames():
    import agent_runner as ar

    ar.enable_agent("batman")
    ar.enable_agent("lucius")
    fd = _load_doctor()
    result = fd.check_enabled_agents()
    assert result.severity == "green"
    assert "batman" in result.message
    assert "lucius" in result.message


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
    """HERMES_DOCTOR=1 should print the OK sentinel and exit 0 without
    running the real checks (so a fresh-install doctor.sh probe does
    not mutate state or hit the network)."""
    env = {
        **os.environ,
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "HERMES_DOCTOR": "1",
    }
    res = subprocess.run(
        [sys.executable, str(DOCTOR)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "[FLEET-DOCTOR-OK]" in res.stdout
