"""Tests for lib/transcripts.py."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import transcripts  # noqa: E402

# --------------------------------------------------------------------------
# Fixtures: a tiny but representative stream-JSON corpus
# --------------------------------------------------------------------------


def _write_firing(
    state_dir: Path,
    codename: str,
    firing_id: str,
    events: list[dict],
    month: str | None = None,
) -> Path:
    month = month or datetime.now(UTC).strftime("%Y-%m")
    out = state_dir / "transcripts" / codename / month / f"{firing_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return out


def _full_firing_events() -> list[dict]:
    return [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Reading the file"},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": "/repo/foo.py"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "ok"}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "ls -la /repo"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {
                            "file_path": "/repo/foo.py",
                            "old_string": "x",
                            "new_string": "y",
                        },
                    },
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "input": {"skill": "review"},
                    },
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 2,
            "total_cost_usd": 0.12,
            "session_id": "abc",
            "stop_reason": "end_turn",
        },
    ]


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path


# --------------------------------------------------------------------------
# transcript_summary
# --------------------------------------------------------------------------


def test_transcript_summary_full_shape(state_dir: Path) -> None:
    path = _write_firing(state_dir, "lucius", "L001", _full_firing_events())
    s = transcripts.transcript_summary(path)
    assert s.tool_calls_total == 4
    assert s.tool_calls_by_name == {"Read": 1, "Bash": 1, "Edit": 1, "Skill": 1}
    assert s.bash_commands == ["ls -la /repo"]
    assert s.files_read == ["/repo/foo.py"]
    assert s.files_edited == ["/repo/foo.py"]
    assert s.skills_invoked == ["review"]
    assert s.result is not None
    assert s.result.subtype == "success"
    assert s.result.num_turns == 2
    assert s.result.total_cost_usd == 0.12


def test_transcript_summary_missing_file(state_dir: Path) -> None:
    s = transcripts.transcript_summary(state_dir / "does-not-exist.jsonl")
    assert s.tool_calls_total == 0
    assert s.result is None


def test_transcript_summary_skips_invalid_json(state_dir: Path) -> None:
    path = state_dir / "transcripts" / "drake" / "2026-05" / "D001.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "result", "subtype": "ok", "num_turns": 1})
        + "\n"
        + "not valid json\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}}]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    s = transcripts.transcript_summary(path)
    # The valid lines still parse - torn tail doesn't sink the whole file.
    assert s.tool_calls_total == 1
    assert s.result is not None


def test_transcript_summary_handles_empty_lines(state_dir: Path) -> None:
    path = state_dir / "transcripts" / "drake" / "2026-05" / "D002.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n\n" + json.dumps({"type": "system", "subtype": "init"}) + "\n\n",
        encoding="utf-8",
    )
    s = transcripts.transcript_summary(path)
    assert s.tool_calls_total == 0


# --------------------------------------------------------------------------
# list_firings / find_firing / list_codenames
# --------------------------------------------------------------------------


def test_list_firings_sorted_newest_first(state_dir: Path) -> None:
    a = _write_firing(state_dir, "lucius", "old", [{"type": "system"}], month="2026-01")
    b = _write_firing(state_dir, "lucius", "new", [{"type": "system"}], month="2026-05")
    import os
    import time

    # Force ordering by setting mtimes explicitly.
    now = time.time()
    os.utime(a, (now - 10_000, now - 10_000))
    os.utime(b, (now, now))

    firings = transcripts.list_firings(state_dir, "lucius")
    assert [f.firing_id for f in firings] == ["new", "old"]


def test_list_firings_empty_returns_empty_list(state_dir: Path) -> None:
    assert transcripts.list_firings(state_dir, "nobody") == []


def test_find_firing_match_and_miss(state_dir: Path) -> None:
    _write_firing(state_dir, "drake", "D001", [{"type": "system"}])
    hit = transcripts.find_firing(state_dir, "drake", "D001")
    assert hit is not None
    assert hit.path.exists()
    assert transcripts.find_firing(state_dir, "drake", "nope") is None


def test_list_codenames(state_dir: Path) -> None:
    _write_firing(state_dir, "lucius", "L1", [{"type": "system"}])
    _write_firing(state_dir, "drake", "D1", [{"type": "system"}])
    assert transcripts.list_codenames(state_dir) == ["drake", "lucius"]


# --------------------------------------------------------------------------
# default_state_dir resolution
# --------------------------------------------------------------------------


def test_default_state_dir_honours_alfred_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ALFRED_STATE_DIR", str(tmp_path / "explicit"))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    assert transcripts.default_state_dir() == tmp_path / "explicit"


def test_default_state_dir_falls_back_to_alfred_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ALFRED_STATE_DIR", raising=False)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "h"))
    assert transcripts.default_state_dir() == tmp_path / "h" / "state"


def test_default_state_dir_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_STATE_DIR", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    assert transcripts.default_state_dir() == Path.home() / ".alfred" / "state"


# --------------------------------------------------------------------------
# Codex helpers
# --------------------------------------------------------------------------


def test_extract_codex_tokens() -> None:
    text = "some preamble\ntokens used\n12,345\nmore output\n"
    assert transcripts.extract_codex_tokens(text) == 12345


def test_extract_codex_tokens_missing_returns_zero() -> None:
    assert transcripts.extract_codex_tokens("nothing here") == 0


def test_extract_codex_session_id() -> None:
    assert transcripts.extract_codex_session_id("session id: 01HXYZ\n") == "01HXYZ"
    assert transcripts.extract_codex_session_id("session id:\n") is None


def test_codex_rate_limit_signal() -> None:
    assert transcripts.codex_run_hit_rate_limit("HTTP 429 too many requests")
    assert transcripts.codex_run_hit_rate_limit("rate-limit hit")
    assert not transcripts.codex_run_hit_rate_limit("all good")


# --------------------------------------------------------------------------
# Render helpers
# --------------------------------------------------------------------------


def test_render_firing_jsonl(state_dir: Path) -> None:
    path = _write_firing(state_dir, "lucius", "L1", _full_firing_events())
    lines = transcripts.render_firing_jsonl(path)
    joined = "\n".join(lines)
    assert "[system] init" in joined
    assert "[tool_use Read]" in joined
    assert "[tool_use Bash] $ ls -la /repo" in joined
    assert "[tool_use Skill] /review" in joined
    assert "[result] subtype=success turns=2" in joined
