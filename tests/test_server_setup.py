"""Tests for first-run setup status helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
        setup_mod.GH_ORG_ENV,
        setup_mod.QUEUE_REPOS_ENV,
        setup_mod.SHIPPED_REPOS_ENV,
        setup_mod.BRIDGE_REPOS_ENV,
        *setup_mod.RUNTIME_REPO_SCOPE_ENV_KEYS,
    )
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def repo_save_keys(*prefix: str) -> list[str]:
    return [
        setup_mod.GH_ORG_ENV,
        *prefix,
        setup_mod.SHIPPED_REPOS_ENV,
        setup_mod.BRIDGE_REPOS_ENV,
        *setup_mod.RUNTIME_REPO_SCOPE_ENV_KEYS,
    ]


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


def test_install_inventory_reports_custom_runtime_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_agents import CustomAgentStore

    home = tmp_path / "active-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

    CustomAgentStore.from_state_root(home / "state").upsert(
        {
            "codename": "release-captain",
            "display_name": "Release Captain",
            "role_title": "Release coordinator",
            "purpose": "Checks release readiness before handoff.",
            "prompt": "Review release readiness and summarize blockers for the operator.",
            "engine": "codex",
            "schedule": "30m",
            "repos": ["acme/api"],
        }
    )

    inventory = setup_mod.install_inventory()
    custom = inventory["custom_agents"]
    by_key = {item["key"]: item for item in inventory["items"]}

    assert custom["count"] == 1
    assert custom["enabled_count"] == 1
    assert custom["agents"][0]["codename"] == "release-captain"
    assert by_key["custom-agents"]["ok"] is True
    assert "1 custom runtime agent" in by_key["custom-agents"]["detail"]


def test_install_inventory_uses_prompt_free_custom_agent_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_agents import CustomAgentStore

    seen: dict[str, bool] = {}

    class Store:
        def snapshot(self, *, include_prompt: bool = True) -> dict[str, object]:
            seen["include_prompt"] = include_prompt
            return {
                "path": str(tmp_path / "custom-agents.json"),
                "count": 0,
                "enabled_count": 0,
                "disabled_count": 0,
                "agents": [],
            }

    monkeypatch.setattr(CustomAgentStore, "from_state_root", lambda _state: Store())

    payload = setup_mod._install_custom_agents(tmp_path)

    assert seen["include_prompt"] is False
    assert payload["agents"] == []


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
    assert result["keys"] == repo_save_keys()
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "GH_ORG=acme" in env_text
    assert "ALFRED_QUEUE_REPOS=" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert "ALFRED_LUCIUS_REPOS=Web" in env_text
    assert "ALFRED_DRAKE_REPOS=Web" in env_text
    assert "ALFRED_RASALGHUL_REPOS=Web" in env_text
    assert "BATMAN_ROLLOUT_ORDER=Web" in env_text
    assert "ALFRED_CODE_MEMORY_REPOS=Web" in env_text
    assert os.environ["GH_ORG"] == "acme"
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "Web"
    assert os.environ["BATMAN_ROLLOUT_ORDER"] == "Web"


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
    assert result["keys"] == repo_save_keys(setup_mod.QUEUE_REPOS_ENV)
    env_text = env_path.read_text(encoding="utf-8")
    assert "GH_ORG=acme" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert "ALFRED_LUCIUS_REPOS=Web" in env_text
    assert "ALFRED_DRAKE_REPOS=Web" in env_text
    assert "ALFRED_BANE_REPOS=Web" in env_text
    assert "ALFRED_RASALGHUL_REPOS=Web" in env_text
    assert "ALFRED_NIGHTWING_REPOS=Web" in env_text
    assert "ALFRED_ROBIN_REPOS=Web" in env_text
    assert "ALFRED_AUTOMERGE_REPOS=Web" in env_text
    assert "ALFRED_CLAIM_SWEEP_REPOS=Web" in env_text
    assert "ALFRED_CODE_MAP_REPOS=Web" in env_text
    assert "ALFRED_CODE_MEMORY_REPOS=Web" in env_text
    assert "ALFRED_MORNING_BRIEF_REPOS=Web" in env_text
    assert "ALFRED_SHIPPED_SUMMARY_DAILY_REPOS=Web" in env_text
    assert "ALFRED_SHIPPED_SUMMARY_WEEKLY_REPOS=Web" in env_text
    assert "BATMAN_ROLLOUT_ORDER=Web" in env_text


def test_persist_selected_repos_preserves_existing_runtime_agent_scopes(
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
                "GH_ORG=acme",
                "ALFRED_AUTOMERGE_REPOS=api",
                "ALFRED_LUCIUS_REPOS=api",
                "ALFRED_CODE_MEMORY_REPOS=api",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "GH_ORG=acme" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text
    assert "ALFRED_AUTOMERGE_REPOS=api" in env_text
    assert "ALFRED_LUCIUS_REPOS=api" in env_text
    assert "ALFRED_CODE_MEMORY_REPOS=api" in env_text
    assert "ALFRED_DRAKE_REPOS=Web" in env_text
    assert os.environ["ALFRED_AUTOMERGE_REPOS"] == "api"
    assert os.environ["ALFRED_DRAKE_REPOS"] == "Web"


def test_persist_selected_repos_preserves_explicit_empty_runtime_agent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_AUTOMERGE_REPOS", raising=False)
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "\n".join(
            [
                "GH_ORG=acme",
                "ALFRED_AUTOMERGE_REPOS=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_AUTOMERGE_REPOS=\n" in env_text
    assert "ALFRED_AUTOMERGE_REPOS=Web" not in env_text
    assert "ALFRED_LUCIUS_REPOS=Web" in env_text
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_AUTOMERGE_REPOS" not in os.environ
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "Web"


def test_persist_selected_repos_rejects_owner_change_with_existing_runtime_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "GH_ORG=legacy\nALFRED_AUTOMERGE_REPOS=api\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="existing GH_ORG"):
        setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "GH_ORG=legacy" in env_text
    assert "ALFRED_AUTOMERGE_REPOS=api" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" not in env_text


def test_persist_selected_repos_rejects_owner_change_with_existing_gh_org_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    home.mkdir(parents=True)
    (home / ".env").write_text("GH_ORG=legacy\n", encoding="utf-8")

    with pytest.raises(ValueError, match="existing GH_ORG"):
        setup_mod.persist_selected_repos(["Acme/Web"], queue_repos=["Acme/Web"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "GH_ORG=legacy" in env_text
    assert "ALFRED_LUCIUS_REPOS=web" not in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" not in env_text


def test_persist_selected_repos_rejects_mixed_owners_before_writing_runtime_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    for key in setup_mod.RUNTIME_REPO_SCOPE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    home.mkdir(parents=True)

    with pytest.raises(ValueError, match="single owner"):
        setup_mod.persist_selected_repos(["Acme/Web", "Other/API"], queue_repos=["Acme/Web"])

    assert not (home / ".env").exists()
    assert "GH_ORG" not in os.environ
    assert "ALFRED_LUCIUS_REPOS" not in os.environ


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


def test_persist_selected_repos_refreshes_previous_ui_runtime_agent_scopes(
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
    assert "ALFRED_SHIPPED_REPOS=acme/api" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/api" in env_text
    assert "ALFRED_LUCIUS_REPOS=API" in env_text
    assert "ALFRED_DRAKE_REPOS=API" in env_text
    assert "BATMAN_ROLLOUT_ORDER=API" in env_text
    assert "ALFRED_CODE_MEMORY_REPOS=API" in env_text
    assert "ALFRED_LUCIUS_REPOS=Web" not in env_text
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "API"
    assert os.environ["BATMAN_ROLLOUT_ORDER"] == "API"
    assert os.environ["ALFRED_CODE_MEMORY_REPOS"] == "API"


def test_persist_selected_repos_preserves_runtime_repo_name_casing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    home.mkdir(parents=True)

    setup_mod.persist_selected_repos(["Acme/MyService"], queue_repos=["Acme/MyService"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_SHIPPED_REPOS=acme/myservice" in env_text
    assert "ALFRED_LUCIUS_REPOS=MyService" in env_text
    assert "ALFRED_AUTOMERGE_REPOS=MyService" in env_text
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "MyService"


def test_persist_selected_repos_keeps_existing_runtime_casing_on_same_board_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ALFRED_HOME", str(home))
    home.mkdir(parents=True)
    (home / ".env").write_text(
        "GH_ORG=acme\nALFRED_SHIPPED_REPOS=acme/myservice\nALFRED_LUCIUS_REPOS=MyService\n",
        encoding="utf-8",
    )

    setup_mod.persist_selected_repos(["acme/myservice"], queue_repos=["acme/myservice"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_LUCIUS_REPOS=MyService" in env_text
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "MyService"


def test_persist_selected_repos_refreshes_previous_ui_runtime_scopes_after_ordered_multi_save(
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

    setup_mod.persist_selected_repos(
        ["Acme/Web", "Acme/API"],
        queue_repos=["Acme/Web", "Acme/API"],
    )
    setup_mod.persist_selected_repos(["Acme/Docs"], queue_repos=["Acme/Docs"])

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/api,acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/docs" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/docs" in env_text
    assert "ALFRED_LUCIUS_REPOS=Docs" in env_text
    assert "BATMAN_ROLLOUT_ORDER=Docs" in env_text
    assert "ALFRED_CODE_MEMORY_REPOS=Docs" in env_text
    assert "ALFRED_LUCIUS_REPOS=Web,API" not in env_text
    assert os.environ["ALFRED_LUCIUS_REPOS"] == "Docs"
    assert os.environ["BATMAN_ROLLOUT_ORDER"] == "Docs"


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
