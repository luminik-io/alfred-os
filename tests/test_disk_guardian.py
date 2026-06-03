"""Tests for the disk guardian: pressure probe + preflight back-off.

Covers the three load-bearing behaviours of the ENOSPC guard:

* :func:`disk_pressure_status` threshold logic (GB floor, % floor, the
  ``low`` early-warning band), with ``shutil.disk_usage`` monkeypatched
  so the test never depends on the host's real free space.
* :func:`preflight` SKIPS cleanly (raises ``PreflightFailed`` → the
  runner's ``sys.exit(0)``) under critical disk, and never crashes.
* The emergency-cleanup trigger fires exactly once and is loop-guarded.

The non-Alfred-path / dirty-worktree safety properties of the emergency
sweep live in ``test_agent_cleanup.py`` alongside the existing sweep
coverage.
"""

from __future__ import annotations

import os
import sys
from collections import namedtuple
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    # A clean slate: no operator-tuned thresholds leak in from the host.
    monkeypatch.delenv("ALFRED_MIN_FREE_DISK_GB", raising=False)
    monkeypatch.delenv("ALFRED_MIN_FREE_DISK_PCT", raising=False)
    monkeypatch.delenv("ALFRED_DISK_EMERGENCY_IN_PROGRESS", raising=False)
    for mod in list(sys.modules):
        if mod.startswith("agent_runner"):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    yield


_Usage = namedtuple("_Usage", ["total", "free", "used"])
_GB = 1024**3


def _fake_usage(*, free_gb: float, total_gb: float = 100.0):
    """Build a fake ``shutil.disk_usage`` return for ``free_gb`` free."""
    total = int(total_gb * _GB)
    free = int(free_gb * _GB)
    return _Usage(total=total, free=free, used=total - free)


# ---------------------------------------------------------------------------
# disk_pressure_status thresholds
# ---------------------------------------------------------------------------


def test_disk_status_healthy_when_plenty_free(monkeypatch):
    import agent_runner.disk as disk

    monkeypatch.setattr(disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=50.0))
    status = disk.disk_pressure_status()
    assert status["critical"] is False
    assert status["low"] is False
    assert status["free_gb"] == pytest.approx(50.0, abs=0.1)
    assert status["free_pct"] == pytest.approx(50.0, abs=0.1)


def test_disk_status_critical_below_gb_floor(monkeypatch):
    import agent_runner.disk as disk

    # 2 GB free of a huge disk: under the 3 GB floor but well over 5%.
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=2.0, total_gb=10000.0)
    )
    status = disk.disk_pressure_status()
    assert status["critical"] is True
    assert status["low"] is False  # critical takes precedence over low


def test_disk_status_low_pct_on_large_disk_is_advisory_not_critical(monkeypatch):
    import agent_runner.disk as disk

    # 10 GB free of a 500 GB disk: 2% free is well under the 5% floor, but
    # 10 GB is far above the 3 GB GB floor. The percent floor is advisory only,
    # so this must NOT be critical (letting a low percent on a big-but-busy disk
    # force a back-off is what could wedge the whole fleet). It is "low" as an
    # early warning via the percent band, but a firing may still run.
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=10.0, total_gb=500.0)
    )
    status = disk.disk_pressure_status()
    assert status["critical"] is False
    assert status["low"] is True
    assert status["free_gb"] == pytest.approx(10.0, abs=0.1)


def test_disk_status_low_band_between_floor_and_1_5x(monkeypatch):
    import agent_runner.disk as disk

    # 4 GB free of a 50 GB disk: over the 3 GB floor but under 1.5x (4.5 GB),
    # while 8% free clears both the 5% floor and the 7.5% low-pct band →
    # low via the GB band only, not critical.
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=4.0, total_gb=50.0)
    )
    status = disk.disk_pressure_status()
    assert status["critical"] is False
    assert status["low"] is True


def test_disk_status_respects_env_threshold_override(monkeypatch):
    import agent_runner.disk as disk

    monkeypatch.setenv("ALFRED_MIN_FREE_DISK_GB", "10")
    monkeypatch.setenv("ALFRED_MIN_FREE_DISK_PCT", "0")  # disable pct floor
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=8.0, total_gb=10000.0)
    )
    status = disk.disk_pressure_status()
    # 8 GB free is under the operator's 10 GB floor → critical.
    assert status["critical"] is True


def test_disk_status_fails_open_on_oserror(monkeypatch):
    import agent_runner.disk as disk

    def _boom(_p):
        raise OSError("stat failed")

    monkeypatch.setattr(disk.shutil, "disk_usage", _boom)
    status = disk.disk_pressure_status()
    # Never wedge the fleet into a permanent skip on a transient stat error.
    assert status["critical"] is False
    assert status["low"] is False


def test_disk_status_bad_env_value_falls_back_to_default(monkeypatch):
    import agent_runner.disk as disk

    monkeypatch.setenv("ALFRED_MIN_FREE_DISK_GB", "not-a-number")
    monkeypatch.setattr(disk.shutil, "disk_usage", lambda _p: _fake_usage(free_gb=50.0))
    status = disk.disk_pressure_status()
    assert status["critical"] is False


# ---------------------------------------------------------------------------
# preflight back-off (the key fix): SKIP clean, never crash
# ---------------------------------------------------------------------------


def test_preflight_skips_clean_under_critical_disk(monkeypatch):
    import agent_runner as ar

    # Force a persistently-critical reading (even after emergency cleanup).
    monkeypatch.setattr(
        ar,
        "disk_pressure_status",
        lambda *a, **k: {
            "free_gb": 0.5,
            "free_pct": 1.0,
            "critical": True,
            "low": False,
        },
    )
    # Emergency cleanup is a no-op stub so the test doesn't shell out.
    calls: list[str] = []
    monkeypatch.setattr(ar, "_run_emergency_cleanup", lambda agent: calls.append(agent))
    monkeypatch.setattr(ar, "slack_post", lambda *a, **k: True)

    spec = ar.PreflightSpec(agent="test")  # agent=="test" suppresses Slack
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    # Emergency cleanup fired exactly once before deciding to skip.
    assert calls == ["test"]


def test_preflight_exit_zero_pattern_under_critical_disk(monkeypatch):
    """The runner pattern (catch PreflightFailed, sys.exit(0)) must hold:
    a low disk behaves identically to any other preflight miss and never
    crashes the process with a non-zero exit."""
    import agent_runner as ar

    monkeypatch.setattr(
        ar,
        "disk_pressure_status",
        lambda *a, **k: {
            "free_gb": 0.5,
            "free_pct": 1.0,
            "critical": True,
            "low": False,
        },
    )
    monkeypatch.setattr(ar, "_run_emergency_cleanup", lambda agent: None)
    monkeypatch.setattr(ar, "slack_post", lambda *a, **k: True)

    exit_code = None
    try:
        ar.preflight(ar.PreflightSpec(agent="test"))
    except ar.PreflightFailed:
        exit_code = 0  # exactly what the runners do
    assert exit_code == 0


def test_preflight_proceeds_when_emergency_cleanup_recovers(monkeypatch):
    import agent_runner as ar

    # First probe critical, second (post-cleanup) probe healthy.
    readings = iter(
        [
            {"free_gb": 0.5, "free_pct": 1.0, "critical": True, "low": False},
            {"free_gb": 20.0, "free_pct": 40.0, "critical": False, "low": False},
        ]
    )
    monkeypatch.setattr(ar, "disk_pressure_status", lambda *a, **k: next(readings))
    monkeypatch.setattr(ar, "_run_emergency_cleanup", lambda agent: None)

    # No other preflight checks declared → returns without raising.
    ar.preflight(ar.PreflightSpec(agent="test"))


def test_preflight_skips_disk_check_when_disabled(monkeypatch):
    """The cleanup agent sets check_disk=False so it runs DESPITE low disk."""
    import agent_runner as ar

    def _should_not_run(*a, **k):
        raise AssertionError("disk_pressure_status must not be probed when check_disk=False")

    monkeypatch.setattr(ar, "disk_pressure_status", _should_not_run)
    ar.preflight(ar.PreflightSpec(agent="test", check_disk=False))


def test_preflight_disk_gate_loop_guarded(monkeypatch):
    """Inside an emergency-cleanup pass the gate must not re-probe."""
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DISK_EMERGENCY_IN_PROGRESS", "1")

    def _should_not_run(*a, **k):
        raise AssertionError("disk probe must be skipped inside emergency pass")

    monkeypatch.setattr(ar, "disk_pressure_status", _should_not_run)
    # No raise: gate returns early because the loop guard is set.
    ar.preflight(ar.PreflightSpec(agent="test"))


def test_preflight_spec_floors_inherited_by_default(monkeypatch):
    """Every agent inherits the guard: a default spec has the floors at
    None (meaning env/built-in defaults), not disabled."""
    import agent_runner as ar

    spec = ar.PreflightSpec(agent="anyagent")
    assert spec.min_free_disk_gb is None
    assert spec.min_free_disk_pct is None
    assert spec.check_disk is True


def test_preflight_per_spec_floor_override_does_not_leak(monkeypatch):
    """A spec-level floor override is applied during the probe and then
    restored, never leaking into the rest of the process env."""
    import agent_runner as ar

    seen: dict[str, str | None] = {}

    def _probe(*a, **k):
        seen["gb"] = os.environ.get("ALFRED_MIN_FREE_DISK_GB")
        return {"free_gb": 99.0, "free_pct": 99.0, "critical": False, "low": False}

    monkeypatch.setattr(ar, "disk_pressure_status", _probe)
    monkeypatch.delenv("ALFRED_MIN_FREE_DISK_GB", raising=False)

    ar.preflight(ar.PreflightSpec(agent="test", min_free_disk_gb=42.0))
    assert seen["gb"] == "42.0"  # applied during the probe
    assert os.environ.get("ALFRED_MIN_FREE_DISK_GB") is None  # restored after
