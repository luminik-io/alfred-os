"""Tests for the runner-level fleet gate in lib/agent_runner.py.

Covers: file-missing default behaviour, file-present default fallback,
comment handling, atomic writes, idempotence, round-trip through
enable_agent / disable_agent.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
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
    # state, idempotent contract per the helper docstring.
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


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("alfred_cli", str(CLI))
    spec = importlib.util.spec_from_loader("alfred_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_cli"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


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
        "ALFRED_HOME": str(tmp_path / "alfred"),
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
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    # Disabling something that was never enabled is fine.
    res = _run_cli("disable", "never-existed", env_extra=env)
    assert res.returncode == 0


def test_cli_enabled_agents_announces_missing_file(tmp_path):
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("enabled-agents", env_extra=env)
    assert res.returncode == 0
    assert "missing" in res.stdout.lower()


def test_cli_engine_set_supports_batman(tmp_path):
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("engine", "set", "batman", "codex", env_extra=env)
    assert res.returncode == 0, res.stderr
    assert "batman engine set to codex" in res.stdout
    assert (tmp_path / "alfred" / "state" / "engines" / "batman").read_text().strip() == "codex"

    status = _run_cli("engine", "status", "batman", env_extra=env)
    assert status.returncode == 0, status.stderr
    assert "batman engine: codex" in status.stdout


def test_cli_engine_status_lists_known_agents(tmp_path):
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    res = _run_cli("engine", "status", env_extra=env)
    assert res.returncode == 0, res.stderr
    for agent in ("bane", "batman", "drake", "lucius", "nightwing", "rasalghul", "robin"):
        assert agent in res.stdout
    assert "Codex fallback only on capability gaps" in res.stdout
    assert "auth/limit/budget" not in res.stdout


def test_cli_codex_status_reports_binary_and_engines(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo codex-test; exit 0; fi\n')
    codex.chmod(0o755)
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
    }

    res = _run_cli("codex", "status", env_extra=env)

    assert res.returncode == 0, res.stderr
    assert "codex version: codex-test" in res.stdout
    assert "engine lucius:" in res.stdout
    assert "Probe with: alfred codex probe" in res.stdout


def test_cli_codex_status_fails_when_binary_missing(tmp_path):
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "PATH": str(tmp_path / "empty-bin"),
    }

    res = _run_cli("codex", "status", env_extra=env)

    assert res.returncode == 1
    assert "codex: not found" in res.stderr


def test_claude_routing_reads_systemd_environment(monkeypatch, tmp_path):
    cli = _load_cli_module()
    monkeypatch.setattr(cli.scheduler, "SCHEDULER", "systemd")
    target = tmp_path / "claude-secondary"

    def fake_run(cmd, **_kwargs):
        assert cmd[:3] == ["systemctl", "--user", "show-environment"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"PATH=/usr/bin\nCLAUDE_CONFIG_DIR={target}\n",
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._current_claude_dir() == str(target)


def test_claude_routing_decodes_systemd_escaped_environment(monkeypatch, tmp_path):
    cli = _load_cli_module()
    monkeypatch.setattr(cli.scheduler, "SCHEDULER", "systemd")
    target = tmp_path / "home with spaces" / ".claude-secondary"

    def fake_run(cmd, **_kwargs):
        assert cmd[:3] == ["systemctl", "--user", "show-environment"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"PATH=/usr/bin\nCLAUDE_CONFIG_DIR=$'{target}'\n",
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._current_claude_dir() == str(target)


def test_claude_primary_sets_systemd_environment(monkeypatch, tmp_path):
    cli = _load_cli_module()
    monkeypatch.setattr(cli.scheduler, "SCHEDULER", "systemd")
    home = tmp_path / "home"
    primary = home / ".claude"
    primary.mkdir(parents=True)
    cli.PRIMARY_CLAUDE_DIR = primary
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli._set_claude_dir(primary, "primary") == 0
    assert ["systemctl", "--user", "set-environment", f"CLAUDE_CONFIG_DIR={primary}"] in calls


def test_cli_auth_status_propagates_codex_status_failure(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n")
    launchctl.chmod(0o755)
    systemctl = fake_bin / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "PATH": str(fake_bin),
    }

    res = _run_cli("auth", "status", env_extra=env)

    assert res.returncode == 1
    assert "Current routing for scheduled agents" in res.stdout
    assert "codex: not found" in res.stderr


def test_cli_status_reports_local_snapshot(tmp_path):
    alfred = tmp_path / "alfred"
    launchd = alfred / "launchd"
    launchd.mkdir(parents=True)
    (launchd / "agents.conf").write_text(
        "my.fleet.batman\tbatman.py\tinterval:5400\tno\t\tBundle coordinator\n"
    )
    wait_dir = alfred / "state" / "batman" / "approval-waits"
    wait_dir.mkdir(parents=True)
    (wait_dir / "firing.json").write_text(
        '{"firing_id":"abc","pid":0,"created_at":"2026-05-12T10:00:00Z","issues":[{"number":504}]}'
    )
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    events_dir = alfred / "state" / "batman" / "events"
    events_dir.mkdir(parents=True)
    (events_dir / f"{day.replace('-', '')}-101500-abcd.jsonl").write_text(
        f'{{"ts":"{day}T10:15:00Z","agent":"batman","firing_id":"abc","event":"firing_started"}}\n'
        f'{{"ts":"{day}T10:15:05Z","agent":"batman","firing_id":"abc","event":"firing_complete","outcome":"silent_no_work"}}\n'
    )
    env = {
        "ALFRED_HOME": str(alfred),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }

    res = _run_cli("status", env_extra=env)

    assert res.returncode == 0, res.stderr
    assert "alfred-status @" in res.stdout
    assert "approval wait dead #504" in res.stdout
    batman_row = next(line for line in res.stdout.splitlines() if line.startswith("batman"))
    assert " 1     0   0" in batman_row


def test_cli_engine_set_accepts_configured_runtime_codename(tmp_path):
    alfred = tmp_path / "alfred"
    launchd = alfred / "launchd"
    launchd.mkdir(parents=True)
    (launchd / "agents.conf").write_text(
        "my.fleet.marshall\tlucius.py\tinterval:1200\tyes\t\tCustom feature engineer\n"
    )
    env = {
        "ALFRED_HOME": str(alfred),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }

    res = _run_cli("engine", "set", "marshall", "codex", env_extra=env)
    assert res.returncode == 0, res.stderr
    assert "marshall engine set to codex" in res.stdout
    assert (alfred / "state" / "engines" / "marshall").read_text().strip() == "codex"

    status = _run_cli("engine", "status", "marshall", env_extra=env)
    assert status.returncode == 0, status.stderr
    assert "marshall engine: codex" in status.stdout


def test_cli_engine_set_rasalghul_uses_canonical_engine_state_only(tmp_path):
    alfred = tmp_path / "alfred"
    env = {
        "ALFRED_HOME": str(alfred),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }

    res = _run_cli("engine", "set", "rasalghul", "codex", env_extra=env)

    assert res.returncode == 0, res.stderr
    assert (alfred / "state" / "engines" / "rasalghul").read_text().strip() == "codex"
    assert not (alfred / "state" / "review-engine").exists()


def test_cli_review_engine_alias_is_not_exposed(tmp_path):
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
    }
    help_res = _run_cli("--help", env_extra=env)
    assert help_res.returncode == 0, help_res.stderr
    assert "review-engine" not in help_res.stdout

    res = _run_cli("review-engine", "status", env_extra=env)
    assert res.returncode == 2
    assert "invalid choice" in res.stderr


def test_cli_agents_does_not_disable_default_agents_when_gate_file_exists(tmp_path):
    alfred = tmp_path / "alfred"
    launchd = alfred / "launchd"
    launchd.mkdir(parents=True)
    (launchd / "agents.conf").write_text(
        "my.fleet.batman\tbatman.py\tinterval:5400\tno\t\tBundle coordinator\n"
        "my.fleet.lucius\tlucius.py\tinterval:1200\tyes\t\tFeature dev\n"
    )
    gate = alfred / "state" / "fleet"
    gate.mkdir(parents=True)
    (gate / "enabled.txt").write_text("batman\n")
    env = {
        "ALFRED_HOME": str(alfred),
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
