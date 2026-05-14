"""Tests for lib/scheduler.py and the alfred CLI pause/resume/run verbs.

scheduler.py wraps the host's per-user scheduler (launchd on macOS, systemd
--user on Linux). These tests exercise the OS-detection branching and the
pure-logic paths that do not require a live scheduler, plus the alfred CLI
subcommands that sit on top of it.

The CLI subprocess tests shell out to ``bin/alfred`` with a fake scheduler
binary on PATH so they run identically on macOS and Linux CI.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import scheduler  # noqa: E402


def test_scheduler_detects_a_known_value():
    # On any CI host this module loads, SCHEDULER is one of the three known
    # values, never an arbitrary string.
    assert scheduler.SCHEDULER in ("launchd", "systemd", "none")


def test_supported_matches_scheduler_value():
    assert scheduler.supported() == (scheduler.SCHEDULER in ("launchd", "systemd"))


def test_unit_file_extension_matches_scheduler():
    if scheduler.SCHEDULER == "launchd":
        assert scheduler.UNIT_EXT == "plist"
        path = scheduler.unit_file("my.fleet.lucius")
        assert path is not None and path.name == "my.fleet.lucius.plist"
    elif scheduler.SCHEDULER == "systemd":
        assert scheduler.UNIT_EXT == "timer"
        path = scheduler.unit_file("my.fleet.lucius")
        assert path is not None and path.name == "my.fleet.lucius.timer"
    else:
        assert scheduler.unit_file("my.fleet.lucius") is None


def test_unit_dir_honors_env_override(monkeypatch, tmp_path):
    # Both override env vars are read at import time; reloading the module
    # with the env set proves the override path is wired up.
    monkeypatch.setenv("ALFRED_LAUNCH_DIR", str(tmp_path / "launchagents"))
    monkeypatch.setenv("ALFRED_SYSTEMD_USER_DIR", str(tmp_path / "systemd-user"))
    reloaded = importlib.reload(scheduler)
    try:
        if reloaded.SCHEDULER == "launchd":
            assert tmp_path / "launchagents" == reloaded.UNIT_DIR
        elif reloaded.SCHEDULER == "systemd":
            assert tmp_path / "systemd-user" == reloaded.UNIT_DIR
    finally:
        # Restore the module to the real environment for other tests.
        monkeypatch.undo()
        importlib.reload(scheduler)


def _run_alfred(args, *, home, alfred_home, extra_path=None):
    env = {**os.environ, "HOME": str(home), "ALFRED_HOME": str(alfred_home)}
    if extra_path:
        env["PATH"] = f"{extra_path}{os.pathsep}{os.environ['PATH']}"
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "alfred"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


def _seed_conf(alfred_home: Path, conf_text: str) -> None:
    conf = alfred_home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(conf_text)


def test_alfred_pause_writes_marker(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    _seed_conf(alfred_home, "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tFeature engineer\n")

    res = _run_alfred(["pause", "lucius"], home=home, alfred_home=alfred_home)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "paused lucius" in res.stdout
    marker = alfred_home / "state" / "_paused" / "lucius"
    assert marker.exists()
    # The marker carries an ISO timestamp so deploy.sh / status can show it.
    assert marker.read_text().strip().endswith("Z")


def test_alfred_pause_unknown_agent_fails(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    _seed_conf(alfred_home, "my.fleet.lucius\tlucius.py\tinterval:600\tno\n")

    res = _run_alfred(["pause", "ghost"], home=home, alfred_home=alfred_home)
    assert res.returncode == 1
    assert "unknown agent 'ghost'" in res.stderr


def test_alfred_run_all_is_refused(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    _seed_conf(alfred_home, "my.fleet.lucius\tlucius.py\tinterval:600\tno\n")

    res = _run_alfred(["run", "all"], home=home, alfred_home=alfred_home)
    assert res.returncode == 2
    assert "intentionally not supported" in res.stderr


def test_alfred_run_honors_pause_marker(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    _seed_conf(alfred_home, "my.fleet.lucius\tlucius.py\tinterval:600\tno\n")
    pause_dir = alfred_home / "state" / "_paused"
    pause_dir.mkdir(parents=True)
    (pause_dir / "lucius").write_text("2026-01-01T00:00:00Z\n")

    res = _run_alfred(["run", "lucius"], home=home, alfred_home=alfred_home)
    # Paused and no --force: refused before any scheduler call.
    assert res.returncode == 1
    assert "paused" in res.stderr


def test_alfred_agents_shows_loaded_column(tmp_path):
    home = tmp_path / "home"
    alfred_home = tmp_path / "alfred"
    home.mkdir()
    alfred_home.mkdir()
    _seed_conf(
        alfred_home,
        "my.fleet.lucius\tlucius.py\tinterval:600\tno\t\tFeature engineer\n"
        "#my.fleet.batman\tbatman.py\tinterval:5400\tyes\t\tBig features\n",
    )
    res = _run_alfred(["agents"], home=home, alfred_home=alfred_home)
    assert res.returncode == 0, res.stdout + res.stderr
    # The header now carries a real scheduler-load column distinct from the
    # configured/on-off column.
    assert "configured" in res.stdout
    assert "loaded" in res.stdout
    # The commented-out batman row renders as configured=off.
    assert "batman" in res.stdout
