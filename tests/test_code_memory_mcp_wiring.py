#!/usr/bin/env python3
"""codebase-memory-mcp is attached to firings alongside the read-only memory MCP.

These tests pin the merged ``--mcp-config`` (one ``mcpServers`` map carrying
both servers), the allowlist augmentation, and the env opt-out, without invoking
Claude or requiring the external binary to be installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

import agent_runner  # noqa: E402
from agent_runner import process as _proc  # noqa: E402

_OK = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"stop_reason":"end_turn","num_turns":1,"total_cost_usd":0,"result":""}'
)


def _capture(monkeypatch) -> list[str]:
    monkeypatch.delenv("ALFRED_DRY_RUN", raising=False)
    # Pin both launcher resolvers so wiring assertions do not depend on the
    # physical files being present in a sparse checkout.
    monkeypatch.setattr(_proc, "_memory_mcp_script", lambda: Path("/repo/bin/alfred-mcp.py"))
    monkeypatch.setattr(_proc, "_code_memory_launcher", lambda: Path("/repo/bin/code-memory-mcp"))
    captured: dict = {}

    def fake_run(cmd, *, cwd=None, timeout=60, capture=True, env=None, **kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout=_OK, stderr="")

    with mock.patch.dict(agent_runner.claude_invoke.__globals__, {"run": fake_run}):
        agent_runner.claude_invoke(
            prompt="hi", workdir=Path("/tmp"), allowed_tools="Read,Bash", max_turns=None, timeout=10
        )
    return captured["cmd"]


def _mcp_config(cmd: list[str]) -> dict | None:
    if "--mcp-config" not in cmd:
        return None
    return json.loads(cmd[cmd.index("--mcp-config") + 1])


def _allowed(cmd: list[str]) -> str:
    return cmd[cmd.index("--allowedTools") + 1]


def test_both_servers_share_one_mcp_config(monkeypatch) -> None:
    monkeypatch.delenv("ALFRED_MEMORY_MCP", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    cmd = _capture(monkeypatch)
    # Exactly one --mcp-config flag carrying both servers.
    assert cmd.count("--mcp-config") == 1
    cfg = _mcp_config(cmd)
    assert cfg is not None
    servers = cfg["mcpServers"]
    assert "alfred_memory" in servers
    assert "code_memory" in servers
    code = servers["code_memory"]
    assert code["command"].endswith("code-memory-mcp")
    assert code["args"] == ["serve"]


def test_code_memory_tools_in_allowlist(monkeypatch) -> None:
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    cmd = _capture(monkeypatch)
    allowed = _allowed(cmd)
    for name in _proc._code_memory_tool_names():
        assert name in allowed, f"{name} missing from allowlist: {allowed}"
    assert "Read" in allowed and "Bash" in allowed  # originals preserved


def test_code_memory_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_CODE_MEMORY_MCP", "0")
    monkeypatch.delenv("ALFRED_MEMORY_MCP", raising=False)
    cmd = _capture(monkeypatch)
    cfg = _mcp_config(cmd)
    # Memory MCP still attached; code-memory must be absent.
    assert cfg is not None
    assert "code_memory" not in cfg["mcpServers"]
    assert "alfred_memory" in cfg["mcpServers"]
    allowed = _allowed(cmd)
    for name in _proc._code_memory_tool_names():
        assert name not in allowed


def test_both_disabled_drops_mcp_config(monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_MEMORY_MCP", "0")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_MCP", "0")
    cmd = _capture(monkeypatch)
    assert "--mcp-config" not in cmd
    assert _allowed(cmd) == "Read,Bash"


def test_code_memory_tool_names_use_server_prefix() -> None:
    names = _proc._code_memory_tool_names()
    assert "mcp__code_memory__search_code" in names
    assert all(n.startswith("mcp__code_memory__") for n in names)


if __name__ == "__main__":
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
