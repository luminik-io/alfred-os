"""Tests for the read-only Alfred memory MCP bridge."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_mcp_memory_tools_are_read_only(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli", repo / "bin" / "alfred-mcp.py")

    from fleet_brain import FleetBrain

    db = tmp_path / "brain.db"
    brain = FleetBrain(db_path=db)
    brain.reflect(codename="lucius", repo="org/api", body="Use fixture factory.")
    brain.propose_memory(codename="lucius", repo="org/api", body="candidate")
    brain.record_file_touch(repo="org/api", path="src/api.py", codename="lucius")
    brain.record_failure(codename="huntress", repo="org/web", subtype="error_timeout", summary="")

    recalled = mod.call_tool(
        "alfred_memory_recall",
        {"codename": "lucius", "repo": "org/api"},
        db_path=str(db),
    )
    assert recalled[0]["body"] == "Use fixture factory."

    candidates = mod.call_tool(
        "alfred_memory_candidates",
        {"codename": "lucius"},
        db_path=str(db),
    )
    assert candidates[0]["body_preview"] == "candidate"
    assert "body" not in candidates[0]
    assert "evidence" not in candidates[0]

    failures = mod.call_tool("alfred_failure_patterns", {"repo": "org/web"}, db_path=str(db))
    assert failures["by_subtype"] == {"error_timeout": 1}
    assert "result_text" not in json.dumps(failures)


def test_mcp_memory_tools_require_scope(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli_scope", repo / "bin" / "alfred-mcp.py")

    from fleet_brain import FleetBrain

    db = tmp_path / "brain.db"
    FleetBrain(db_path=db)
    response = mod.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "alfred_memory_candidates", "arguments": {}},
        },
        db_path=str(db),
    )
    assert response["error"]["code"] == -32000
    assert "codename or repo scope" in response["error"]["message"]


def test_mcp_json_rpc_tools_call(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli_rpc", repo / "bin" / "alfred-mcp.py")
    db = tmp_path / "brain.db"

    response = mod.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        db_path=str(db),
    )
    assert response["result"]["tools"]

    response = mod.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "alfred_memory_doctor", "arguments": {}},
        },
        db_path=str(db),
    )
    assert response["result"]["isError"] is False
    assert "brain database does not exist" in response["result"]["content"][0]["text"]


def test_mcp_graph_tools_are_listed(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli_graph_list", repo / "bin" / "alfred-mcp.py")
    names = {tool["name"] for tool in mod.TOOLS}
    assert {"alfred_who_owns", "alfred_recent_changes_near", "alfred_prs_touching"} <= names
    for tool in mod.TOOLS:
        if tool["name"] in {
            "alfred_who_owns",
            "alfred_recent_changes_near",
            "alfred_prs_touching",
        }:
            assert tool["inputSchema"]["required"] == ["repo", "path"]


def test_mcp_graph_tools_answer_ownership_and_change_locality(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli_graph", repo / "bin" / "alfred-mcp.py")

    from fleet_brain import FleetBrain

    db = tmp_path / "brain.db"
    brain = FleetBrain(db_path=db)
    brain.ingest_codeowners(repo="org/api", content="*  @org/everyone\n/src/  @org/api-team\n")
    brain.record_file_touch(
        repo="org/api",
        path="src/app.py",
        codename="lucius",
        pr_url="https://example.test/org/api/pull/7",
    )
    brain.record_file_touch(repo="org/api", path="src/util.py", codename="drake")

    owners = mod.call_tool(
        "alfred_who_owns",
        {"repo": "org/api", "path": "src/app.py"},
        db_path=str(db),
    )
    assert owners == {"repo": "org/api", "path": "src/app.py", "owners": ["@org/api-team"]}

    near = mod.call_tool(
        "alfred_recent_changes_near",
        {"repo": "org/api", "path": "src/app.py"},
        db_path=str(db),
    )
    assert {row["path"] for row in near} == {"src/app.py", "src/util.py"}

    prs = mod.call_tool(
        "alfred_prs_touching",
        {"repo": "org/api", "path": "src/app.py"},
        db_path=str(db),
    )
    assert [row["pr"] for row in prs] == ["https://example.test/org/api/pull/7"]


def test_mcp_graph_tools_require_repo_and_path(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    mod = _load("alfred_mcp_cli_graph_scope", repo / "bin" / "alfred-mcp.py")
    db = tmp_path / "brain.db"

    response = mod.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "alfred_who_owns", "arguments": {"repo": "org/api"}},
        },
        db_path=str(db),
    )
    assert response["error"]["code"] == -32000
    assert "repo and a path" in response["error"]["message"]
