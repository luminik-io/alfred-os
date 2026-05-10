"""Tests for the runner-level fleet gate in lib/agent_runner.py.

Covers: file-missing default behaviour, file-present default fallback,
comment handling, atomic writes, idempotence, round-trip through
enable_agent / disable_agent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def test_is_agent_enabled_returns_default_when_file_missing():
    import agent_runner as ar

    assert not ar.FLEET_ENABLED_FILE.exists()
    # Default-enabled (opt-out) for stable agents.
    assert ar.is_agent_enabled("lucius") is True
    assert ar.is_agent_enabled("lucius", default=True) is True
    # Default-disabled (opt-in) for new/in-burn-in agents.
    assert ar.is_agent_enabled("batman", default=False) is False


def test_is_agent_enabled_respects_default_when_file_present():
    import agent_runner as ar

    ar.FLEET_ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ar.FLEET_ENABLED_FILE.write_text("batman\nlucius\n")
    # Listed: enabled regardless of default.
    assert ar.is_agent_enabled("batman", default=False) is True
    assert ar.is_agent_enabled("lucius", default=False) is True
    # Not listed: the caller's default still decides. This lets the same file
    # gate opt-in runners while normal launchd-scheduled agents remain visible
    # and runnable unless explicitly paused/unloaded.
    assert ar.is_agent_enabled("nightwing", default=True) is True
    assert ar.is_agent_enabled("nightwing", default=False) is False


def test_read_enabled_codenames_skips_blank_and_comments():
    import agent_runner as ar

    ar.FLEET_ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ar.FLEET_ENABLED_FILE.write_text(
        "# This file managed by alfred CLI\n"
        "\n"
        "batman\n"
        "  # indented comment\n"
        "lucius # MVP burn-in\n"
        "\n"
    )
    out = ar.list_enabled_agents()
    assert out == ["batman", "lucius"]


def test_enable_agent_round_trip():
    import agent_runner as ar

    out = ar.enable_agent("batman")
    assert "batman" in out
    assert ar.FLEET_ENABLED_FILE.exists()
    assert ar.is_agent_enabled("batman") is True
    assert ar.is_agent_enabled("nightwing", default=True) is True  # default-enabled


def test_enable_agent_idempotent():
    import agent_runner as ar

    ar.enable_agent("batman")
    out = ar.enable_agent("batman")
    # Single occurrence even when called twice.
    assert out.count("batman") == 1


def test_disable_agent_idempotent_when_not_present():
    import agent_runner as ar

    # Disabling a never-enabled agent must not raise and must not change
    # state — idempotent contract per the helper docstring.
    ar.enable_agent("lucius")
    out = ar.disable_agent("never-listed")
    assert out == ["lucius"]


def test_disable_agent_round_trip():
    import agent_runner as ar

    ar.enable_agent("batman")
    ar.enable_agent("lucius")
    out = ar.disable_agent("batman")
    assert out == ["lucius"]
    assert ar.is_agent_enabled("batman", default=False) is False
    assert ar.is_agent_enabled("lucius") is True


def test_enable_agent_rejects_empty_codename():
    import agent_runner as ar

    with pytest.raises(ValueError):
        ar.enable_agent("")
    with pytest.raises(ValueError):
        ar.enable_agent("   ")


def test_atomic_write_leaves_no_tmp_orphan(tmp_path):
    import agent_runner as ar

    ar.enable_agent("batman")
    # The atomic write must not leave a *.tmp file behind.
    parent = ar.FLEET_ENABLED_FILE.parent
    leftover = list(parent.glob("*.tmp"))
    assert leftover == [], f"unexpected tmp orphans: {leftover}"


def test_write_dedupes_silently():
    import agent_runner as ar

    ar.enable_agent("batman")
    # Manually inject duplicates into the file to mimic a hand-edit, then
    # any subsequent enable/disable should normalize the state.
    ar.FLEET_ENABLED_FILE.write_text("batman\nbatman\nlucius\n")
    out = ar.enable_agent("nightwing")
    # Sorted, deduped.
    assert out == ["batman", "lucius", "nightwing"]


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------

CLI = Path(__file__).resolve().parent.parent / "bin" / "alfred"


def _run_cli(*argv: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        env=full_env,
    )


def test_cli_enable_then_enabled_agents_round_trip(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("enable", "batman", env_extra=env)
    assert res.returncode == 0, res.stderr
    assert "enabled batman" in res.stdout

    res = _run_cli("enabled-agents", env_extra=env)
    assert res.returncode == 0
    assert "batman" in res.stdout


def test_cli_disable_idempotent(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    # Disabling something that was never enabled is fine.
    res = _run_cli("disable", "never-existed", env_extra=env)
    assert res.returncode == 0


def test_cli_enabled_agents_announces_missing_file(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("enabled-agents", env_extra=env)
    assert res.returncode == 0
    assert "missing" in res.stdout.lower()


def test_cli_engine_set_supports_batman(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("engine", "set", "batman", "codex", env_extra=env)
    assert res.returncode == 0, res.stderr
    assert "batman engine set to codex" in res.stdout
    assert (tmp_path / "hermes" / "state" / "engines" / "batman").read_text().strip() == "codex"

    status = _run_cli("engine", "status", "batman", env_extra=env)
    assert status.returncode == 0, status.stderr
    assert "batman engine: codex" in status.stdout


def test_cli_engine_status_lists_known_agents(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("engine", "status", env_extra=env)
    assert res.returncode == 0, res.stderr
    for agent in ("bane", "batman", "drake", "lucius", "nightwing", "rasalghul", "robin"):
        assert agent in res.stdout


def test_cli_review_engine_alias_is_not_exposed(tmp_path):
    env = {
        "HERMES_HOME": str(tmp_path / "hermes"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    help_res = _run_cli("--help", env_extra=env)
    assert help_res.returncode == 0, help_res.stderr
    assert "review-engine" not in help_res.stdout

    res = _run_cli("review-engine", "status", env_extra=env)
    assert res.returncode == 2
    assert "invalid choice" in res.stderr


def test_cli_agents_does_not_disable_default_agents_when_gate_file_exists(tmp_path):
    hermes = tmp_path / "hermes"
    launchd = hermes / "launchd"
    launchd.mkdir(parents=True)
    (launchd / "agents.conf").write_text(
        "my.fleet.batman\tbatman.py\tinterval:5400\tno\t\tBundle coordinator\n"
        "my.fleet.lucius\tlucius.py\tinterval:1200\tyes\t\tFeature dev\n"
    )
    gate = hermes / "state" / "fleet"
    gate.mkdir(parents=True)
    (gate / "enabled.txt").write_text("batman\n")
    env = {
        "HERMES_HOME": str(hermes),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }

    res = _run_cli("agents", env_extra=env)
    assert res.returncode == 0, res.stderr
    lines = {
        line.split()[0]: line
        for line in res.stdout.splitlines()
        if line.startswith(("batman", "lucius"))
    }
    assert "yes" in lines["batman"].split()
    assert "yes" in lines["lucius"].split()
