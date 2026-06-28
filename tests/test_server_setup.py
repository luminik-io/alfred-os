"""Tests for first-run setup status helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import server.setup as setup_mod  # noqa: E402


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


def test_install_inventory_does_not_mix_launcher_config_into_active_default_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_home = tmp_path / ".alfred"
    launcher_home = tmp_path / "launcher-runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "missing-workspace"))

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

    assert setup_mod.selected_repos() == ["acme/api"]
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
    env_path.write_text("ALFRED_SHIPPED_REPOS=Acme/API\n", encoding="utf-8")

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
    by_key = {item["key"]: item for item in status["install"]["items"]}
    assert by_key["repos"]["ok"] is True
    assert status["ready"] is True


def test_persist_selected_repos_writes_launcher_home_and_rc_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)

    rc = tmp_path / ".alfredrc"
    rc.write_text(
        f"export ALFRED_HOME={home}\nexport ALFRED_QUEUE_REPOS=old/repo\n",
        encoding="utf-8",
    )
    home.mkdir(parents=True)

    result = setup_mod.persist_selected_repos(["Acme/Web"])

    env_path = home / ".env"
    assert result["env_path"] == str(env_path)
    env_text = env_path.read_text(encoding="utf-8")
    assert "ALFRED_QUEUE_REPOS=acme/web" in env_text
    assert "ALFRED_SHIPPED_REPOS=acme/web" in env_text
    assert "ALFRED_BRIDGE_REPOS=acme/web" in env_text

    rc_text = rc.read_text(encoding="utf-8")
    assert "export ALFRED_QUEUE_REPOS=acme/web" in rc_text
    assert "export ALFRED_SHIPPED_REPOS=acme/web" in rc_text
    assert "export ALFRED_BRIDGE_REPOS=acme/web" in rc_text
    assert setup_mod.selected_repos() == ["acme/web"]


def test_persist_selected_repos_writes_rc_override_for_stale_launch_env(
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

    setup_mod.persist_selected_repos(["Acme/Web"])

    rc_text = (tmp_path / ".alfredrc").read_text(encoding="utf-8")
    assert "export ALFRED_QUEUE_REPOS=acme/web" in rc_text
    assert "export ALFRED_SHIPPED_REPOS=acme/web" in rc_text
    assert "export ALFRED_BRIDGE_REPOS=acme/web" in rc_text

    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "old/repo")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "old/repo")

    launcher_env = setup_mod._setup_launcher_env()
    assert launcher_env["ALFRED_QUEUE_REPOS"] == "acme/web"
    assert launcher_env["ALFRED_SHIPPED_REPOS"] == "acme/web"
    assert launcher_env["ALFRED_BRIDGE_REPOS"] == "acme/web"
