#!/usr/bin/env python3
"""The read-only memory MCP server is attached to every claude firing by default.

This lets agents recall prior lessons as a tool (the model decides when) instead
of memory being a passive store. These tests pin the ``--mcp-config`` +
``--allowedTools`` augmentation and the env opt-out, without invoking Claude.
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


def test_memory_mcp_attached_by_default(monkeypatch):
    monkeypatch.delenv("ALFRED_MEMORY_MCP", raising=False)
    cmd = _capture(monkeypatch)
    cfg = _mcp_config(cmd)
    assert cfg is not None, f"--mcp-config must be present by default; got {cmd}"
    server = cfg["mcpServers"]["alfred_memory"]
    assert server["args"][0].endswith("alfred-mcp.py")
    assert server["args"][-1] == "serve"
    allowed = _allowed(cmd)
    for name in _proc._memory_tool_names():
        assert name in allowed, f"{name} missing from allowlist: {allowed}"
    assert "Read" in allowed and "Bash" in allowed  # originals preserved


def test_memory_mcp_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ALFRED_MEMORY_MCP", "0")
    cmd = _capture(monkeypatch)
    assert "--mcp-config" not in cmd
    assert _allowed(cmd) == "Read,Bash"  # untouched


def test_memory_tool_names_use_server_prefix():
    names = _proc._memory_tool_names()
    assert "mcp__alfred_memory__alfred_memory_recall" in names
    assert all(n.startswith("mcp__alfred_memory__") for n in names)


if __name__ == "__main__":
    import pytest as _pytest

    sys.exit(_pytest.main([__file__, "-v"]))
