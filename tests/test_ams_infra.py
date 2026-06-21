"""Static checks for Alfred's local Redis Agent Memory Server launcher."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AMS_LAUNCH = ROOT / "bin" / "ams-launch.sh"
INSTALL_SH = ROOT / "install.sh"
DEPLOY_SH = ROOT / "deploy.sh"


def test_ams_launcher_exists_and_is_executable() -> None:
    assert AMS_LAUNCH.is_file()
    assert os.access(AMS_LAUNCH, os.X_OK)


def test_ams_launcher_uses_shared_config_and_loopback_server() -> None:
    text = AMS_LAUNCH.read_text()

    assert "from memory.ams_server import ams_server_env" in text
    assert "from memory.ams_server import AmsServerConfig" in text
    assert "api --host" in text
    assert "--port" in text


def test_ams_launcher_requires_redis_stack_for_vector_search() -> None:
    text = AMS_LAUNCH.read_text()

    assert "redis-stack-server" in text
    assert "redis_has_redisearch" in text
    assert "wait_for_redis_ping" in text
    assert "MODULE LIST" in text
    assert "FT._LIST" in text


def test_ams_launcher_starts_ollama_and_falls_back_to_uvx() -> None:
    text = AMS_LAUNCH.read_text()

    assert "ollama serve" in text
    assert "wait_for_ollama" in text
    assert "/api/tags" in text
    assert "ollama did not answer" in text
    assert "agent-memory token add" in text
    assert '--token "$ALFRED_AMS_TOKEN"' in text
    assert "command -v agent-memory" in text
    assert "agent_memory_runs agent-memory" in text
    assert "uvx --python 3.12" in text
    assert "agent-memory-server.git" in text


def test_deploy_starts_ams_as_host_service() -> None:
    text = DEPLOY_SH.read_text()

    assert "install_ams_service_linux" in text
    assert "install_ams_service_launchd" in text
    assert "alfred-ams.service" in text
    assert "io.luminik.alfred.ams.plist" in text
    assert "ams-launch.sh" in text
    assert "enable --now alfred-ams.service" in text
    assert "launchctl bootstrap" in text


def test_installer_provisions_ams_dependencies() -> None:
    text = INSTALL_SH.read_text()

    assert "redis-stack/redis-stack" in text
    assert "redis-stack-server" in text
    assert "ollama" in text
    assert "uv tool install --python 3.12" in text
    assert "agent-memory-server.git" in text
    assert "mxbai-embed-large llama3.2" in text
    assert 'ollama pull "$ollama_model"' in text
    assert "Deploy also starts the local Redis Agent Memory Server" in text
    apt_line = next(line for line in text.splitlines() if "local apt_pkgs=" in line)
    assert "redis-server" not in apt_line
    assert "redis-tools" in text
