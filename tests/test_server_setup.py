"""Tests for first-run setup status helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import server.setup as setup_mod  # noqa: E402


@pytest.fixture(autouse=True)
def restore_repo_env_keys() -> None:
    """Undo live process mirrors written by repo-selection saves."""

    keys = (
        setup_mod.QUEUE_REPOS_ENV,
        setup_mod.SHIPPED_REPOS_ENV,
        setup_mod.BRIDGE_REPOS_ENV,
    )
    saved = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_install_inventory_reports_existing_config_without_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "alfred"
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "\n".join(
            [
                "ALFRED_QUEUE_REPOS=acme/api",
                "ALFRED_SHIPPED_REPOS=acme/api",
                "ALFRED_BRIDGE_REPOS=acme/api",
                "SLACK_BOT_TOKEN=xoxb-super-secret",
                "ALFRED_AMS_PORT=9099",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    token = home / "state" / "server-token"
    token.parent.mkdir(parents=True)
    token.write_text("local-token-secret\n", encoding="utf-8")

    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tSingle-repo engineer\n",
        encoding="utf-8",
    )

    inventory = setup_mod.install_inventory(repos=["acme/api"])
    payload = json.dumps(inventory)

    assert inventory["initialized"] is True
    assert inventory["env_present"] is True
    assert inventory["server_token_present"] is True
    assert inventory["agents_conf_present"] is True
    assert inventory["scheduled_runs"] == 1
    assert inventory["selected_repos_env_present"] is True
    assert inventory["slack_configured"] is True
    assert inventory["memory_configured"] is True
    assert "xoxb-super-secret" not in payload
    assert "local-token-secret" not in payload

    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["agents"]["ok"] is True
    assert by_key["repos"]["ok"] is True
    assert by_key["slack"]["ok"] is True
    assert by_key["token"]["ok"] is True


def test_install_inventory_reports_unmanaged_alfred_launchd_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "old.alfred.batman.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>old.alfred.batman</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agent_launch}</string>
    <string>batman.py</string>
  </array>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    (launch_agents / "com.example.keep.plist").write_text("<plist />", encoding="utf-8")
    managed = runtime / "launchd" / "managed-labels.txt"
    managed.parent.mkdir(parents=True)
    managed.write_text("alfred.lucius\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "old.alfred.batman\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "old.alfred.batman":
            return [str(runtime / "bin" / "agent-launch"), "batman.py"]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["old.alfred.batman"]
    assert inventory["unmanaged_scheduler_count"] == 1
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is False
    assert "old.alfred.batman" in by_key["scheduler_unmanaged"]["detail"]
    assert by_key["scheduler_unmanaged"]["path"] == str(launch_agents)


def test_install_inventory_ignores_unloaded_alfred_launchd_plists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "old.alfred.batman.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>old.alfred.batman</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agent_launch}</string>
    <string>batman.py</string>
  </array>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.keep\n")

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is True


def test_install_inventory_blocks_when_launchd_list_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(setup_mod.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["launchd probe unavailable"]
    assert inventory["unmanaged_scheduler_count"] == 1
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is False
    assert "Could not query launchd" in by_key["scheduler_unmanaged"]["detail"]


def test_install_inventory_detects_program_only_launchd_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "old.alfred.batman.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>old.alfred.batman</string>
  <key>Program</key>
  <string>{agent_launch}</string>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "old.alfred.batman\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "old.alfred.batman":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["old.alfred.batman"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_detects_loaded_job_masked_by_stale_local_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.example.worker.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/true</string>
  </array>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.worker":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_prefers_active_job_over_stale_local_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.example.worker.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agent_launch}</string>
  </array>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.worker":
            return ["/usr/bin/true"]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0


def test_install_inventory_blocks_loaded_plist_when_active_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.example.worker.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agent_launch}</string>
  </array>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.worker (unreadable)"]
    assert inventory["unmanaged_scheduler_count"] == 1
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is False
    assert "Could not verify 1 loaded launchd label" in by_key["scheduler_unmanaged"]["detail"]


def test_install_inventory_ignores_unreadable_non_alfred_loaded_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.example.worker.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/true</string>
  </array>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0


def test_install_inventory_retries_loaded_plist_when_active_read_transiently_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.example.worker.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.example.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/true</string>
  </array>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")
    calls: list[str] = []

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str] | None:
        calls.append(label)
        if len(calls) == 1:
            return None
        return [str(runtime / "bin" / "agent-launch")]

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert calls == ["com.example.worker", "com.example.worker"]
    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_blocks_unreadable_alfred_family_loaded_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "old.alfred.batman\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["old.alfred.batman (unreadable)"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_ignores_unreadable_generic_engineering_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.eng.worker\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0


def test_install_inventory_blocks_unreadable_inferred_legacy_fleet_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv(
        "ALFRED_SETUP_LAUNCHD_LIST_FIXTURE",
        "\n".join(["team.eng.batman", "team.eng.lucius", "team.eng.worker"]),
    )
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == [
        "team.eng.batman (unreadable)",
        "team.eng.lucius (unreadable)",
        "team.eng.worker (unreadable)",
    ]
    assert inventory["unmanaged_scheduler_count"] == 3


def test_install_inventory_blocks_unreadable_configured_legacy_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LEGACY_LAUNCHD_LABEL_PREFIXES", "team.eng")
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "team.eng.worker\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["team.eng.worker (unreadable)"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_blocks_unreadable_default_legacy_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    label = ".".join(("luminik", "eng", "worker"))
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", f"{label}\n")
    monkeypatch.setattr(setup_mod, "_launchctl_program_args", lambda *_args: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == [f"{label} (unreadable)"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_treats_agents_conf_labels_as_managed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    conf = runtime / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "com.example.worker\tworker.py\tinterval:1200\tyes\t\tCurrent worker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.worker":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["agents_conf_present"] is True
    assert inventory["scheduled_runs"] == 1
    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0


def test_install_inventory_does_not_trust_stale_managed_labels_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    conf = runtime / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\tCurrent worker\n",
        encoding="utf-8",
    )
    (runtime / "launchd" / "managed-labels.txt").write_text(
        "com.acme.alfred.worker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.acme.alfred.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.acme.alfred.worker":
            return [str(tmp_path / "old-alfred" / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.acme.alfred.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_detects_loaded_alfred_job_without_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.acme.alfred.batman\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.acme.alfred.batman":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.acme.alfred.batman"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_detects_arbitrary_no_plist_label_running_from_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv(
        "ALFRED_SETUP_LAUNCHD_LIST_FIXTURE",
        "com.example.worker\ncom.example.keep\n",
    )
    probed: list[str] = []

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        probed.append(label)
        if label == "com.example.worker":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1
    assert set(probed) == {"com.example.keep", "com.example.worker"}


def test_install_inventory_detects_no_plist_job_from_old_alfred_install_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    old_runtime = tmp_path / "old-alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv(
        "ALFRED_SETUP_LAUNCHD_LIST_FIXTURE",
        "\n".join(["team.eng.batman", "team.eng.lucius", "team.eng.worker"]),
    )

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "team.eng.worker":
            return [str(old_runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["team.eng.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_detects_single_old_alfred_install_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    old_runtime = tmp_path / "internal-alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "team.eng.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "team.eng.worker":
            return [str(old_runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["team.eng.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_ignores_external_agent_launch_without_alfred_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.drake.equation\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.drake.equation":
            return ["/opt/vendor/bin/agent-launch"]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0


def test_install_inventory_detects_wrapper_job_running_from_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.worker\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.worker":
            return ["/usr/bin/python3", str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.worker"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_install_inventory_detects_no_plist_job_without_launch_agents_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "alfred"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "com.example.nightwing\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "com.example.nightwing":
            return [str(runtime / "bin" / "agent-launch")]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == ["com.example.nightwing"]
    assert inventory["unmanaged_scheduler_count"] == 1


def test_launchctl_program_args_parses_arguments_array(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "alfred" / "bin" / "agent-launch"

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=f"""
gui/501/com.example.batman = {{
\tstate = not running

\targuments = {{
\t\t{launcher}
\t\tbatman.py
\t}}
}}
""",
        )

    monkeypatch.setattr(setup_mod.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(setup_mod.os, "getuid", lambda: 501)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod._launchctl_program_args("com.example.batman", {}) == [
        str(launcher),
        "batman.py",
    ]


def test_launchctl_program_args_prefers_arguments_array_over_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "alfred" / "bin" / "agent-launch"

    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=f"""
gui/501/com.example.batman = {{
\tstate = not running
\tprogram = /usr/bin/python3

\targuments = {{
\t\t0 => /usr/bin/python3
\t\t1 => {launcher}
\t\t2 => batman.py
\t}}
}}
""",
        )

    monkeypatch.setattr(setup_mod.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(setup_mod.os, "getuid", lambda: 501)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod._launchctl_program_args("com.example.batman", {}) == [
        "/usr/bin/python3",
        str(launcher),
        "batman.py",
    ]


def test_launchctl_program_args_returns_none_for_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout="""
gui/501/com.example.batman = {
\tstate = running
}
""",
        )

    monkeypatch.setattr(setup_mod.os, "uname", lambda: SimpleNamespace(sysname="Darwin"))
    monkeypatch.setattr(setup_mod.os, "getuid", lambda: 501)
    monkeypatch.setattr(setup_mod.subprocess, "run", fake_run)

    assert setup_mod._launchctl_program_args("com.example.batman", {}) is None


def test_install_inventory_treats_unmanaged_jobs_as_initialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "missing-alfred"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "old.alfred.batman.plist").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>old.alfred.batman</string>
  <key>ProgramArguments</key>
  <array>
    <string>{agent_launch}</string>
    <string>batman.py</string>
  </array>
</dict>
</plist>
""".format(agent_launch=runtime / "bin" / "agent-launch"),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setenv("ALFRED_SETUP_LAUNCHD_LIST_FIXTURE", "old.alfred.batman\n")

    def fake_program_args(label: str, _env: dict[str, str]) -> list[str]:
        if label == "old.alfred.batman":
            return [str(runtime / "bin" / "agent-launch"), "batman.py"]
        return []

    monkeypatch.setattr(setup_mod, "_launchctl_program_args", fake_program_args)

    inventory = setup_mod.install_inventory()

    assert inventory["initialized"] is True
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is False


def test_install_inventory_skips_launchd_scan_without_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "alfred"
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    monkeypatch.setattr(setup_mod, "_safe_home", lambda _env: None)

    inventory = setup_mod.install_inventory()

    assert inventory["unmanaged_scheduler_jobs"] == []
    assert inventory["unmanaged_scheduler_count"] == 0
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["scheduler_unmanaged"]["ok"] is True
    assert by_key["scheduler_unmanaged"]["path"] is None


def test_install_inventory_uses_active_serve_home_for_agents_conf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "active-runtime"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    (tmp_path / ".alfredrc").write_text(f"ALFRED_HOME={launcher_home}\n", encoding="utf-8")
    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("ALFRED_QUEUE_REPOS=acme/api\n", encoding="utf-8")

    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tSingle-repo engineer\n",
        encoding="utf-8",
    )
    launcher_conf = launcher_home / "launchd" / "agents.conf"
    launcher_conf.parent.mkdir(parents=True)
    launcher_conf.write_text(
        "alfred.bane\tbane.py\tinterval:1200\tyes\t\topus\tLauncher-only engineer\n",
        encoding="utf-8",
    )

    inventory = setup_mod.install_inventory(repos=["acme/api"])

    assert inventory["alfred_home"] == str(home)
    assert inventory["agents_conf_path"] == str(conf)
    assert inventory["agents_conf_present"] is True
    assert inventory["scheduled_runs"] == 1


def test_install_inventory_does_not_reuse_checkout_agents_conf_for_runtime_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "active-runtime"
    repo = tmp_path / "alfred-checkout"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_REPO", str(repo))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    conf = repo / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tSingle-repo engineer\n",
        encoding="utf-8",
    )

    inventory = setup_mod.install_inventory()

    assert inventory["agents_conf_path"] is None
    assert inventory["agents_conf_present"] is False
    assert inventory["scheduled_runs"] == 0


def test_install_inventory_prefers_runtime_home_agents_conf_over_repo_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "active-runtime"
    repo = tmp_path / "alfred-checkout"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_REPO", str(repo))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    home_conf = home / "launchd" / "agents.conf"
    home_conf.parent.mkdir(parents=True)
    home_conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tRuntime engineer\n",
        encoding="utf-8",
    )
    repo_conf = repo / "launchd" / "agents.conf"
    repo_conf.parent.mkdir(parents=True)
    repo_conf.write_text(
        "alfred.bane\tbane.py\tinterval:1200\tyes\t\topus\tCheckout engineer\n",
        encoding="utf-8",
    )

    inventory = setup_mod.install_inventory()

    assert inventory["agents_conf_path"] == str(home_conf)
    assert inventory["agents_conf_present"] is True
    assert inventory["scheduled_runs"] == 1


def test_install_inventory_ignores_explicit_alfredrc_without_process_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    custom_rc = tmp_path / "custom.alfredrc"
    home.mkdir()
    runtime.mkdir()
    custom_rc.write_text(
        f"ALFRED_HOME={runtime}\nALFRED_SHIPPED_REPOS=acme/api\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFREDRC", str(custom_rc))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    status = setup_mod.bootstrap_status()
    inventory = status["install"]
    active_home = home / ".alfred"

    assert status["repos"]["selected"] == []
    assert inventory["alfred_home"] == str(active_home)
    assert inventory["env_path"] == str(active_home / ".env")
    assert inventory["env_present"] is False
    assert inventory["selected_repos_env_present"] is False
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["env"]["path"] == str(active_home / ".env")
    assert by_key["env"]["ok"] is False
    assert by_key["repos"]["path"] is None


def test_install_inventory_does_not_mix_launcher_config_into_active_process_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_home = tmp_path / ".alfred"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(active_home))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))
    for key in (
        "ALFRED_QUEUE_REPOS",
        "ALFRED_SHIPPED_REPOS",
        "ALFRED_BRIDGE_REPOS",
        "SLACK_WEBHOOK_URL",
        "SLACK_WEBHOOK_SECRET_ID",
        "SLACK_BOT_TOKEN",
        "ALFRED_SLACK_BOT_TOKEN_SECRET_ID",
        "SLACK_APP_TOKEN",
        "ALFRED_SLACK_APP_TOKEN",
        "ALFRED_REDIS_MEMORY_URL",
        "ALFRED_REDIS_MEMORY_NAMESPACE",
        "ALFRED_AMS_HOST",
        "ALFRED_AMS_PORT",
        "ALFRED_AMS_REDIS_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    (tmp_path / ".alfredrc").write_text(f"ALFRED_HOME={launcher_home}\n", encoding="utf-8")
    env_path = launcher_home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "\n".join(
            [
                "ALFRED_SHIPPED_REPOS=acme/api",
                "SLACK_BOT_TOKEN=xoxb-launcher-only",
                "ALFRED_AMS_PORT=9099",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    launcher_conf = launcher_home / "launchd" / "agents.conf"
    launcher_conf.parent.mkdir(parents=True)
    launcher_conf.write_text(
        "alfred.lucius\tlucius.py\tinterval:1200\tyes\t\topus\tLauncher-only install\n",
        encoding="utf-8",
    )

    inventory = setup_mod.install_inventory()

    assert inventory["alfred_home"] == str(active_home)
    assert inventory["agents_conf_path"] is None
    assert inventory["agents_conf_present"] is False
    assert inventory["scheduled_runs"] == 0
    assert inventory["selected_repos_env_present"] is False
    assert inventory["slack_configured"] is False
    assert inventory["memory_configured"] is False
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["repos"]["ok"] is False
    assert by_key["slack"]["ok"] is False
    assert by_key["memory"]["path"] is None


def test_install_inventory_ignores_default_alfredrc_runtime_without_process_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    (home / ".alfredrc").write_text(
        f"ALFRED_HOME={runtime}\nALFRED_QUEUE_REPOS=acme/api\nALFRED_SHIPPED_REPOS=acme/api\n",
        encoding="utf-8",
    )

    status = setup_mod.bootstrap_status()
    inventory = status["install"]
    active_home = home / ".alfred"

    assert status["repos"]["selected"] == []
    assert status["queue"]["ready"] is False
    assert inventory["alfred_home"] == str(active_home)
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["env"]["path"] == str(active_home / ".env")
    assert by_key["repos"]["path"] is None


def test_bootstrap_status_does_not_treat_queue_only_scope_as_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("ALFRED_QUEUE_REPOS=Acme/API\n", encoding="utf-8")

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert setup_mod.selected_repos() == []
    assert status["repos"]["selected"] == []
    assert status["repos"]["count"] == 0
    assert status["install"]["selected_repos_env_present"] is True
    by_key = {item["key"]: item for item in status["install"]["items"]}
    assert by_key["repos"]["ok"] is False
    assert "Queue-only repo scope found" in by_key["repos"]["detail"]
    assert status["ready"] is False


def test_bootstrap_status_uses_active_serve_home_for_board_repo_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "ALFRED_QUEUE_REPOS=Acme/API\nALFRED_SHIPPED_REPOS=Acme/API\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert status["repos"]["selected"] == ["acme/api"]
    assert status["repos"]["count"] == 1
    assert status["queue"]["ready"] is True
    by_key = {item["key"]: item for item in status["install"]["items"]}
    assert by_key["repos"]["ok"] is True
    assert status["ready"] is True


def test_bootstrap_status_strips_queue_inline_comments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "ALFRED_QUEUE_REPOS=org/allowed # org/board disabled\nALFRED_SHIPPED_REPOS=org/board\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert status["repos"]["selected"] == ["org/board"]
    assert status["queue"]["ready"] is True
    assert status["queue"]["covers_selected"] is False
    assert status["queue"]["missing_selected"] == ["org/board"]
    assert status["ready"] is False


def test_bootstrap_status_rejects_split_queue_and_board_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "ALFRED_QUEUE_REPOS=Legacy/Repo\nALFRED_SHIPPED_REPOS=Acme/API\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert status["repos"]["selected"] == ["acme/api"]
    assert status["queue"]["ready"] is True
    assert status["queue"]["covers_selected"] is False
    assert status["queue"]["missing_selected"] == ["acme/api"]
    assert status["ready"] is False


def test_bootstrap_status_requires_enabled_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "ALFRED_QUEUE_REPOS=\nALFRED_SHIPPED_REPOS=Acme/API\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert status["repos"]["selected"] == ["acme/api"]
    assert status["queue"]["ready"] is False
    assert status["ready"] is False


def test_bootstrap_status_preserves_empty_process_queue_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "")
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    env_path = home / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "ALFRED_QUEUE_REPOS=Acme/API\nALFRED_SHIPPED_REPOS=Acme/API\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octo", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})

    status = setup_mod.bootstrap_status()

    assert status["repos"]["selected"] == ["acme/api"]
    assert status["queue"]["ready"] is False
    assert status["queue"]["count"] == 0
    assert status["queue"]["missing_selected"] == ["acme/api"]
    assert status["ready"] is False


def test_persist_selected_repos_writes_active_home_without_importing_stale_launcher_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)

    rc = tmp_path / ".alfredrc"
    rc.write_text(
        f"export ALFRED_HOME={launcher_home}\nexport ALFRED_QUEUE_REPOS=old/repo\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)

    result = setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_path = home / ".env"
    assert result["env_path"] == str(env_path)
    env_text = env_path.read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_QUEUE_REPOS=old/repo" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text

    rc_text = rc.read_text(encoding="utf-8")
    assert "export ALFRED_QUEUE_REPOS=old/repo" in rc_text
    assert "export ALFRED_QUEUE_REPOS=acme/web" not in rc_text
    assert "export ALFRED_SHIPPED_REPOS=acme/web" not in rc_text
    assert "export ALFRED_BRIDGE_REPOS=acme/web" not in rc_text
    assert setup_mod.setup_board_repos() == ["acme/web"]


def test_persist_selected_repos_board_only_save_does_not_create_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)

    result = setup_mod.persist_selected_repos(["Acme/Web"])

    assert not (tmp_path / ".alfredrc").exists()
    assert result["keys"] == [
        "ALFRED_SHIPPED_REPOS",
        "ALFRED_BRIDGE_REPOS",
    ]
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text


def test_persist_selected_repos_does_not_sync_to_rc_that_omits_custom_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)
    rc = tmp_path / ".alfredrc"
    rc.write_text("export ALFRED_SHIPPED_REPOS=old/repo\n", encoding="utf-8")

    setup_mod.persist_selected_repos(["Acme/Web"])

    rc_text = rc.read_text(encoding="utf-8")
    assert "export ALFRED_SHIPPED_REPOS=old/repo" in rc_text
    assert "export ALFRED_SHIPPED_REPOS=acme/web" not in rc_text
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text


def test_selected_repos_skips_stale_launcher_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)

    (tmp_path / ".alfredrc").write_text(
        f"export ALFRED_HOME={launcher_home}\nexport ALFRED_QUEUE_REPOS=old/repo\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)

    assert setup_mod.selected_repos() == []


def test_selected_repos_skips_matching_launcher_queue_only_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)

    (tmp_path / ".alfredrc").write_text(
        f"export ALFRED_HOME={home}\nexport ALFRED_QUEUE_REPOS=old/repo\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)

    assert setup_mod.selected_repos() == []


def test_selected_repos_honors_empty_runtime_board_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    (tmp_path / ".alfredrc").write_text(
        f"export ALFRED_HOME={home}\n"
        "export ALFRED_SHIPPED_REPOS=old/repo\n"
        "export ALFRED_BRIDGE_REPOS=old/repo\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "ALFRED_SHIPPED_REPOS=\nALFRED_BRIDGE_REPOS=\n",
        encoding="utf-8",
    )

    assert setup_mod.selected_repos() == []
    inventory = setup_mod.install_inventory()
    assert inventory["selected_repos_env_present"] is True
    by_key = {item["key"]: item for item in inventory["items"]}
    assert by_key["repos"]["ok"] is False
    assert "No repositories selected yet" in by_key["repos"]["detail"]


def test_persist_selected_repos_seeds_queue_for_new_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)

    result = setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_path = home / ".env"
    assert result["env_path"] == str(env_path)
    assert result["keys"] == [
        "ALFRED_QUEUE_REPOS",
        "ALFRED_SHIPPED_REPOS",
        "ALFRED_BRIDGE_REPOS",
    ]
    env_text = env_path.read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text


def test_persist_selected_repos_preserves_exported_queue_scope_without_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "\n".join(
            [
                "export ALFRED_QUEUE_REPOS=old/repo",
                "export ALFRED_SHIPPED_REPOS=old/repo",
                "export ALFRED_BRIDGE_REPOS=old/repo",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=old/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert "export ALFRED_QUEUE_REPOS" not in env_text
    assert "export ALFRED_SHIPPED_REPOS" not in env_text
    assert "export ALFRED_BRIDGE_REPOS" not in env_text


def test_persist_selected_repos_replaces_queue_scope_when_explicitly_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "old/repo")
    home.mkdir(parents=True)

    setup_mod.persist_selected_repos(
        ["Acme/Web"],
        queue_repos=["Acme/Web"],
        replace_queue_repos=True,
    )

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_QUEUE_REPOS=old/repo" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert os.environ["ALFRED_QUEUE_REPOS"] == "acme/web"


def test_persist_selected_repos_preserves_existing_queue_scope_on_guided_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "old/repo")
    home.mkdir(parents=True)

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    assert not (tmp_path / ".alfredrc").exists()
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=old/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text

    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)

    assert setup_mod.setup_board_repos() == ["acme/web"]
    assert setup_mod.selected_repos() == ["acme/web"]


def test_persist_selected_repos_preserves_previous_ui_save_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])
    setup_mod.persist_selected_repos(["Acme/API"], queue_repos=["Acme/API"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/api" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/api" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/api" in env_text
    assert os.environ["ALFRED_QUEUE_REPOS"] == "acme/web"
    assert os.environ["ALFRED_SHIPPED_REPOS"] == "acme/api"
    assert os.environ["ALFRED_BRIDGE_REPOS"] == "acme/api"


def test_persist_selected_repos_preserves_process_queue_only_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "old/repo")
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)

    setup_mod.persist_selected_repos(["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=old/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert os.environ["ALFRED_QUEUE_REPOS"] == "old/repo"
    assert os.environ["ALFRED_SHIPPED_REPOS"] == "acme/web"
    assert os.environ["ALFRED_BRIDGE_REPOS"] == "acme/web"


def test_persist_selected_repos_preserves_process_queue_that_matches_persisted_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "old/repo")
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "ALFRED_SHIPPED_REPOS=old/repo\nALFRED_BRIDGE_REPOS=old/repo\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=old/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert os.environ["ALFRED_QUEUE_REPOS"] == "old/repo"
    assert os.environ["ALFRED_SHIPPED_REPOS"] == "acme/web"
    assert os.environ["ALFRED_BRIDGE_REPOS"] == "acme/web"


def test_persist_selected_repos_preserves_process_queue_that_matches_live_board(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "live/repo")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "live/repo")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "live/repo")
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "ALFRED_SHIPPED_REPOS=stale/repo\nALFRED_BRIDGE_REPOS=stale/repo\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=live/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert os.environ["ALFRED_QUEUE_REPOS"] == "live/repo"
    assert os.environ["ALFRED_SHIPPED_REPOS"] == "acme/web"
    assert os.environ["ALFRED_BRIDGE_REPOS"] == "acme/web"


def test_persist_selected_repos_preserves_active_narrow_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "\n".join(
            [
                "ALFRED_QUEUE_REPOS=old/repo",
                "ALFRED_SHIPPED_REPOS=old/repo,current/repo",
                "ALFRED_BRIDGE_REPOS=old/repo,current/repo",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=old/repo" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text


def test_persist_selected_repos_preserves_empty_active_queue_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "\n".join(
            [
                "ALFRED_QUEUE_REPOS=",
                "ALFRED_SHIPPED_REPOS=",
                "ALFRED_BRIDGE_REPOS=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=\n" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text


def test_persist_selected_repos_ignores_stale_rc_queue_scope_when_runtime_has_board_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    (tmp_path / ".alfredrc").write_text(
        f"export ALFRED_HOME={launcher_home}\nexport ALFRED_QUEUE_REPOS=prod/safe\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "\n".join(
            [
                "ALFRED_SHIPPED_REPOS=prod/api,prod/web",
                "ALFRED_BRIDGE_REPOS=prod/api,prod/web",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Prod/API", "Prod/Mobile"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=" not in env_text
    assert "ALFRED_QUEUE_REPOS=prod/safe" not in env_text
    assert "ALFRED_SHIPPED_REPOS=prod/api,prod/mobile" in env_text
    assert "ALFRED_BRIDGE_REPOS=prod/api,prod/mobile" in env_text

    rc_text = (tmp_path / ".alfredrc").read_text(encoding="utf-8")
    assert "export ALFRED_QUEUE_REPOS=prod/safe" in rc_text
    assert "export ALFRED_SHIPPED_REPOS=prod/api,prod/mobile" not in rc_text
