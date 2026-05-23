"""Tests for bin/alfred-logs.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))


def _load_alfred_logs():
    spec = importlib.util.spec_from_file_location(
        "alfred_logs_cli", str(REPO_ROOT / "bin" / "alfred-logs.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _events_with_tools(num_reads: int = 1, num_bash: int = 1, num_edits: int = 0,
                      skill: str | None = None) -> list[dict]:
    content = []
    for _ in range(num_reads):
        content.append({"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}})
    for _ in range(num_bash):
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": "ls /a"}})
    for _ in range(num_edits):
        content.append({"type": "tool_use", "name": "Edit", "input": {"file_path": "/a"}})
    if skill:
        content.append({"type": "tool_use", "name": "Skill", "input": {"skill": skill}})
    return [
        {"type": "assistant", "message": {"content": content}},
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 4,
            "total_cost_usd": 0.21,
            "stop_reason": "end_turn",
        },
    ]


def _write_firing(state_dir: Path, codename: str, firing_id: str, events: list[dict]) -> Path:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    path = state_dir / "transcripts" / codename / month / f"{firing_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Summary mode
# --------------------------------------------------------------------------


def test_logs_summary_smoke(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "F001", _events_with_tools(skill="review"))
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alfred-logs lucius" in out
    assert "F001" in out
    assert "success" in out
    assert "Readx1" in out or "Read" in out


def test_logs_summary_json(tmp_path: Path, capsys):
    _write_firing(tmp_path, "drake", "D001", _events_with_tools())
    cli = _load_alfred_logs()
    rc = cli.main(["drake", "--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["codename"] == "drake"
    assert payload["firings"][0]["firing_id"] == "D001"
    assert payload["firings"][0]["tool_calls_total"] == 2


def test_logs_summary_no_transcripts(tmp_path: Path, capsys):
    # Put a different codename so list_codenames is non-empty and the
    # validation gate doesn't trip.
    _write_firing(tmp_path, "drake", "D1", _events_with_tools())
    # Drake exists, lucius doesn't — but we want the "no transcripts" path,
    # so create the directory empty under transcripts/.
    (tmp_path / "transcripts" / "lucius").mkdir(parents=True, exist_ok=True)
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path)])
    assert rc == 0
    assert "no transcripts" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --firing-id
# --------------------------------------------------------------------------


def test_logs_firing_pretty_dump(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "F010", _events_with_tools(num_reads=2, skill="qa"))
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path), "--firing-id", "F010"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[tool_use Read]" in out
    assert "[tool_use Bash]" in out
    assert "[tool_use Skill] /qa" in out
    assert "[result] subtype=success" in out


def test_logs_firing_tool_calls(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "F020", _events_with_tools(num_reads=3, num_bash=2, num_edits=1))
    cli = _load_alfred_logs()
    rc = cli.main([
        "lucius", "--state-dir", str(tmp_path),
        "--firing-id", "F020", "--show-tool-calls",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total tool calls: 6" in out
    assert "Read" in out and "x3" in out


def test_logs_firing_not_found_exit_1(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "real", _events_with_tools())
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path), "--firing-id", "ghost"])
    assert rc == 1
    assert "no transcript" in capsys.readouterr().err


def test_logs_firing_json_passthrough(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "F030", _events_with_tools())
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path), "--firing-id", "F030", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    # Each line must be valid JSON (raw passthrough).
    for line in [line for line in out.splitlines() if line.strip()]:
        json.loads(line)


# --------------------------------------------------------------------------
# --show-tool-calls aggregate
# --------------------------------------------------------------------------


def test_logs_show_tool_calls_aggregate(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "A1", _events_with_tools(num_reads=2, num_bash=1, skill="review"))
    _write_firing(tmp_path, "lucius", "A2", _events_with_tools(num_reads=1, num_bash=3))
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path), "--show-tool-calls"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alfred-logs lucius --show-tool-calls" in out
    # 2 firings x Bash counts = 4
    assert "Bash" in out
    assert "/review" in out


def test_logs_show_tool_calls_json(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "A1", _events_with_tools(num_reads=2))
    cli = _load_alfred_logs()
    rc = cli.main([
        "lucius", "--state-dir", str(tmp_path), "--show-tool-calls", "--json"
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["codename"] == "lucius"
    assert payload["firings"] == 1
    assert payload["tools"]["Read"] == 2


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_logs_unknown_codename_exit_1(tmp_path: Path, capsys):
    _write_firing(tmp_path, "lucius", "F1", _events_with_tools())
    cli = _load_alfred_logs()
    rc = cli.main(["ghost", "--state-dir", str(tmp_path)])
    assert rc == 1
    assert "ghost" in capsys.readouterr().err


def test_logs_missing_state_dir_exit_2(tmp_path: Path, capsys):
    cli = _load_alfred_logs()
    rc = cli.main(["lucius", "--state-dir", str(tmp_path / "missing")])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err
