from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
ALFRED = ROOT / "bin" / "alfred"
CUSTOM_AGENT = ROOT / "bin" / "custom-agent.py"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from custom_agents import CustomAgentError, CustomAgentStore, canonical_schedule  # noqa: E402


def test_custom_agent_store_validates_and_round_trips(tmp_path: Path) -> None:
    store = CustomAgentStore.from_state_root(tmp_path / "state")

    agent = store.upsert(
        {
            "codename": "security-scout",
            "display_name": "Security Scout",
            "role_title": "Security reviewer",
            "purpose": "Review risky changes before PR handoff.",
            "prompt": "Review the configured repositories for risky code changes and summarize actions.",
            "engine": "codex",
            "schedule": "weekly@mon:09:05",
            "repos": ["acme/api", "acme/web", "acme/api"],
        }
    )

    assert agent.codename == "security-scout"
    assert agent.engine == "codex"
    assert agent.schedule == "cron:1:9:05"
    assert agent.repos == ("acme/api", "acme/web")
    assert store.get("security-scout") == agent
    assert store.conf_rows() == [
        "alfred.security-scout\tcustom-agent.py\tcron:1:9:05\tno\t"
        "alfred.security-scout\tSecurity reviewer"
    ]
    payload = json.loads((tmp_path / "state" / "custom-agents" / "custom-agents.json").read_text())
    assert payload["version"] == 1
    assert payload["agents"][0]["display_name"] == "Security Scout"


def test_custom_agent_store_rejects_builtin_and_bad_schedule(tmp_path: Path) -> None:
    store = CustomAgentStore.from_state_root(tmp_path / "state")
    base = {
        "display_name": "Builder",
        "role_title": "Builder",
        "purpose": "Builds things.",
        "prompt": "Inspect the repository and summarize the next concrete engineering action.",
    }

    with pytest.raises(CustomAgentError):
        store.upsert({"codename": "lucius", **base})
    with pytest.raises(CustomAgentError):
        store.upsert({"codename": "new-builder", "schedule": "tomorrow", **base})


@pytest.mark.parametrize(
    "codename",
    [
        "connector-sync",
        "fleet-ingest",
        "fleet-github-poll",
        "custom-agent",
        "alfred-slack-thread-sync",
        "slack-thread-sync",
    ],
)
def test_custom_agent_store_rejects_shipped_runner_codenames(
    tmp_path: Path,
    codename: str,
) -> None:
    store = CustomAgentStore.from_state_root(tmp_path / "state")

    with pytest.raises(CustomAgentError, match="built-in runtime codename"):
        store.upsert(
            {
                "codename": codename,
                "display_name": "Builder",
                "role_title": "Builder",
                "purpose": "Builds things.",
                "prompt": "Inspect the repository and summarize the next concrete engineering action.",
            }
        )


def test_custom_agent_store_rejects_scheduler_config_codename_suffix(tmp_path: Path) -> None:
    home = tmp_path / "alfred-home"
    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "my.fleet.marshall\tlucius.py\tinterval:600\tno\tmy.fleet.marshall\tOps lead\n",
        encoding="utf-8",
    )
    store = CustomAgentStore.from_state_root(home / "state")

    with pytest.raises(CustomAgentError, match="scheduler config"):
        store.upsert(
            {
                "codename": "marshall",
                "display_name": "Marshall",
                "role_title": "Operations lead",
                "purpose": "Coordinate operational follow-up.",
                "prompt": "Review operational follow-up needs and summarize concrete next actions.",
                "schedule": "10m",
            }
        )


def test_custom_agent_store_rejects_disabled_scheduler_config_codename_suffix(
    tmp_path: Path,
) -> None:
    home = tmp_path / "alfred-home"
    conf = home / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "#my.fleet.marshall\tlucius.py\tinterval:600\tno\tmy.fleet.marshall\tOps lead\n",
        encoding="utf-8",
    )
    store = CustomAgentStore.from_state_root(home / "state")

    with pytest.raises(CustomAgentError, match="scheduler config"):
        store.upsert(
            {
                "codename": "marshall",
                "display_name": "Marshall",
                "role_title": "Operations lead",
                "purpose": "Coordinate operational follow-up.",
                "prompt": "Review operational follow-up needs and summarize concrete next actions.",
                "schedule": "10m",
            }
        )


@pytest.mark.parametrize("repo", ["../outside", "./repo", "acme/..", "acme/."])
def test_custom_agent_store_rejects_dot_segment_repo_slugs(
    tmp_path: Path,
    repo: str,
) -> None:
    store = CustomAgentStore.from_state_root(tmp_path / "state")

    with pytest.raises(CustomAgentError):
        store.upsert(
            {
                "codename": "repo-scout",
                "display_name": "Repo Scout",
                "role_title": "Repository scout",
                "purpose": "Checks a configured repository.",
                "prompt": "Inspect the configured repository and summarize the next action.",
                "repos": [repo],
            }
        )


def test_custom_agent_store_load_drops_existing_reserved_names(tmp_path: Path) -> None:
    state = tmp_path / "state"
    manifest = state / "custom-agents" / "custom-agents.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "lucius",
                        "display_name": "Legacy Lucius",
                        "role_title": "Legacy custom role",
                        "purpose": "Existing custom role before a reserved-name update.",
                        "prompt": "Summarize the current repository state for the operator.",
                        "engine": "codex",
                        "schedule": "interval:3600",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = CustomAgentStore.from_state_root(state)

    assert store.load() == []
    with pytest.raises(CustomAgentError, match="built-in runtime codename"):
        store.conf_rows(strict=True)


def test_custom_agent_store_upsert_preserves_malformed_manifest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    manifest = state / "custom-agents" / "custom-agents.json"
    manifest.parent.mkdir(parents=True)
    original = '{"version": 1, "agents": ['
    manifest.write_text(original, encoding="utf-8")
    store = CustomAgentStore.from_state_root(state)

    assert store.load() == []
    with pytest.raises(CustomAgentError, match="not valid JSON"):
        store.upsert(
            {
                "codename": "release-captain",
                "display_name": "Release Captain",
                "role_title": "Release coordinator",
                "purpose": "Checks release readiness.",
                "prompt": "Review release readiness and summarize blockers before the operator ships.",
            }
        )

    assert manifest.read_text(encoding="utf-8") == original


def test_custom_agent_store_delete_preserves_malformed_manifest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    manifest = state / "custom-agents" / "custom-agents.json"
    manifest.parent.mkdir(parents=True)
    original = '{"version": 1, "agents": ['
    manifest.write_text(original, encoding="utf-8")
    store = CustomAgentStore.from_state_root(state)

    with pytest.raises(CustomAgentError, match="not valid JSON"):
        store.delete("release-captain")

    assert manifest.read_text(encoding="utf-8") == original


def test_custom_agent_store_strict_rows_reject_invalid_enabled_value(tmp_path: Path) -> None:
    state = tmp_path / "state"
    manifest = state / "custom-agents" / "custom-agents.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers before the operator ships.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": "false",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = CustomAgentStore.from_state_root(state)

    assert store.load()[0].enabled is True
    with pytest.raises(CustomAgentError, match="invalid agent"):
        store.conf_rows(strict=True)


def test_custom_agent_schedule_shortcuts_match_scheduler_grammar() -> None:
    assert canonical_schedule("10m") == "interval:600"
    assert canonical_schedule("every 2h") == "interval:7200"
    assert canonical_schedule("daily@07:30") == "cron:7:30"


@pytest.mark.parametrize("value", ["1s", "59s", "interval:1", "interval:59"])
def test_custom_agent_schedule_rejects_sub_minute_intervals(value: str) -> None:
    with pytest.raises(CustomAgentError):
        canonical_schedule(value)


def test_alfred_agent_cli_add_list_remove(tmp_path: Path) -> None:
    home = tmp_path / "alfred-home"
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(home)
    env["WORKSPACE_ROOT"] = str(tmp_path / "workspace")

    add = subprocess.run(
        [
            sys.executable,
            str(ALFRED),
            "agent",
            "add",
            "release-captain",
            "--display-name",
            "Release Captain",
            "--role-title",
            "Release coordinator",
            "--prompt",
            "Review release readiness for the configured repositories and list blockers.",
            "--engine",
            "hybrid",
            "--schedule",
            "30m",
            "--repo",
            "acme/api",
            "--json",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert add.returncode == 0, add.stderr
    assert json.loads(add.stdout)["agent"]["schedule"] == "interval:1800"

    listed = subprocess.run(
        [sys.executable, str(ALFRED), "agent", "list", "--json"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["agents"][0]["codename"] == "release-captain"

    removed = subprocess.run(
        [sys.executable, str(ALFRED), "agent", "remove", "release-captain", "--json"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr
    assert json.loads(removed.stdout)["removed"] is True


def test_custom_agent_doctor_mode_exits_before_engine_invocation(tmp_path: Path) -> None:
    home = tmp_path / "alfred-home"
    workspace = tmp_path / "workspace"
    fakebin = tmp_path / "fakebin"
    marker = tmp_path / "codex-invoked"
    store = home / "state" / "custom-agents"
    store.mkdir(parents=True)
    workspace.mkdir()
    fakebin.mkdir()
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (fakebin / "codex").write_text(
        '#!/usr/bin/env sh\nprintf "codex invoked\\n" >> "$CODEX_MARKER"\nexit 42\n',
        encoding="utf-8",
    )
    (fakebin / "git").write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    (fakebin / "codex").chmod(0o755)
    (fakebin / "git").chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(CUSTOM_AGENT)],
        env={
            **os.environ,
            "AGENT_CODENAME": "release-captain",
            "ALFRED_DOCTOR": "1",
            "ALFRED_HOME": str(home),
            "WORKSPACE_ROOT": str(workspace),
            "CODEX_MARKER": str(marker),
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "[RELEASE-CAPTAIN-DOCTOR-OK]" in result.stdout
    assert not marker.exists()


def test_custom_agent_pause_marker_exits_before_preflight(tmp_path: Path) -> None:
    home = tmp_path / "alfred-home"
    workspace = tmp_path / "workspace"
    fakebin = tmp_path / "fakebin"
    store = home / "state" / "custom-agents"
    pause_dir = home / "state" / "_paused"
    store.mkdir(parents=True)
    pause_dir.mkdir(parents=True)
    workspace.mkdir()
    fakebin.mkdir()
    (pause_dir / "release-captain").write_text("operator paused\n", encoding="utf-8")
    (store / "custom-agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "agents": [
                    {
                        "codename": "release-captain",
                        "display_name": "Release Captain",
                        "role_title": "Release coordinator",
                        "purpose": "Checks release readiness.",
                        "prompt": "Review release readiness and summarize blockers.",
                        "engine": "codex",
                        "schedule": "interval:1800",
                        "repos": [],
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(CUSTOM_AGENT)],
        env={
            **os.environ,
            "AGENT_CODENAME": "release-captain",
            "ALFRED_HOME": str(home),
            "WORKSPACE_ROOT": str(workspace),
            "PATH": str(fakebin),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "[RELEASE-CAPTAIN-PAUSED]" in result.stdout
    assert "PREFLIGHT" not in result.stdout + result.stderr
