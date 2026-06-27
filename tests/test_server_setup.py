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
