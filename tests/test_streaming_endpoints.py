"""Tests for the real-time streaming serve helpers."""

# ruff: noqa: E402,I001

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from fastapi.testclient import TestClient

from server import FilesystemReader, create_app
from server import streaming


def _transcript_for(state: Path, codename: str, firing_id: str) -> Path:
    path = state / "transcripts" / codename / "2026-06" / f"{firing_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _events_for(state: Path, codename: str, firing_id: str) -> Path:
    path = state / codename / "events" / f"{firing_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _assistant_line(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if name is not None:
            events.append((name, data if data is not None else {}))
    return events


def test_chunk_reads_only_whole_lines_and_advances_offset(tmp_path: Path) -> None:
    state = tmp_path / "state"
    transcript = _transcript_for(state, "lucius", "fire-1")
    transcript.write_text("alpha\nbeta\n", encoding="utf-8")

    first = streaming.tail_transcript_chunk(state, "fire-1", offset=0)
    assert first["found"] is True
    assert first["lines"] == ["alpha", "beta"]
    assert first["offset"] == len("alpha\nbeta\n")

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("gam")
    torn = streaming.tail_transcript_chunk(state, "fire-1", offset=first["offset"])
    assert torn["lines"] == []
    assert torn["offset"] == first["offset"]

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("ma\n")
    rest = streaming.tail_transcript_chunk(state, "fire-1", offset=torn["offset"])
    assert rest["lines"] == ["gamma"]


def test_chunk_reports_not_found_before_transcript_exists(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    chunk = streaming.tail_transcript_chunk(state, "never-fired", offset=0)
    assert chunk["found"] is False
    assert chunk["lines"] == []


def test_firing_is_done_reads_terminal_events_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    events = _events_for(state, "lucius", "fire-2")
    events.write_text(
        json.dumps({"ts": "t", "event": "firing_started"}) + "\n",
        encoding="utf-8",
    )
    assert streaming.firing_is_done(state, "fire-2") is False
    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": "t", "event": "firing_complete"}) + "\n")
    assert streaming.firing_is_done(state, "fire-2") is True


def test_find_transcript_rejects_path_traversal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "transcripts").mkdir(parents=True)
    assert streaming.find_transcript(state, "../../etc/passwd") is None
    assert streaming.find_transcript(state, "..") is None


def test_assistant_text_fragments_in_order(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "system", "subtype": "init"})
        + "\n"
        + _assistant_line("Reading ")
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Read", "input": {}}],
                },
            }
        )
        + "\n"
        + _assistant_line("the code.")
        + "\n",
        encoding="utf-8",
    )
    assert streaming.assistant_text_fragments(transcript) == ["Reading ", "the code."]


def test_tail_offset_poll_fallback_returns_json_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    transcript = _transcript_for(state, "lucius", "poll-1")
    transcript.write_text("one\ntwo\n", encoding="utf-8")
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    resp = client.get("/api/firings/poll-1/tail", params={"poll": 1, "offset": 0})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["found"] is True
    assert payload["lines"] == ["one", "two"]
    assert payload["done"] is False

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("three\n")
    resp2 = client.get("/api/firings/poll-1/tail", params={"poll": 1, "offset": payload["offset"]})
    assert resp2.json()["lines"] == ["three"]


def test_tail_poll_missing_transcript_degrades_not_errors(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    client = TestClient(create_app(FilesystemReader(state_root=state)))
    resp = client.get("/api/firings/ghost/tail", params={"poll": 1})
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_tail_sse_streams_appends_then_done(tmp_path: Path) -> None:
    state = tmp_path / "state"
    transcript = _transcript_for(state, "lucius", "sse-1")
    transcript.write_text("first\n", encoding="utf-8")
    events = _events_for(state, "lucius", "sse-1")
    events.write_text(json.dumps({"event": "firing_started"}) + "\n", encoding="utf-8")

    def _grow() -> None:
        time.sleep(0.2)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write("second\n")
        time.sleep(0.2)
        with events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "firing_complete"}) + "\n")

    grower = threading.Thread(target=_grow)
    grower.start()
    try:
        client = TestClient(create_app(FilesystemReader(state_root=state)))
        with client.stream("GET", "/api/firings/sse-1/tail") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = "".join(resp.iter_text())
    finally:
        grower.join()

    events_seen = _parse_sse(body)
    names = [name for name, _ in events_seen]
    assert names[0] == "open"
    assert "done" in names
    appended: list[str] = []
    for name, data in events_seen:
        if name == "append":
            appended.extend(data["lines"])
    assert "first" in appended
    assert "second" in appended


def test_stream_converse_turn_primitive_orders_tokens_before_result(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    def run_turn() -> dict[str, str]:
        with transcript.open("w", encoding="utf-8") as handle:
            handle.write(_assistant_line("hello ") + "\n")
            handle.flush()
            time.sleep(0.1)
            handle.write(_assistant_line("world") + "\n")
            handle.flush()
            time.sleep(0.1)
        return {"reply": "hello world"}

    async def _collect() -> str:
        frames = []
        async for frame in streaming.stream_converse_turn(
            run_turn=run_turn,
            extract_tokens=streaming.assistant_text_fragments,
            transcript_path=transcript,
            reconcile=lambda turn: {"reply": turn["reply"]},
            poll_seconds=0.02,
        ):
            frames.append(frame.decode("utf-8"))
        return "".join(frames)

    events = _parse_sse(asyncio.run(_collect()))
    names = [name for name, _ in events]
    assert names[0] == "open"
    assert names[-1] == "result"
    tokens = [data["text"] for name, data in events if name == "token"]
    assert "".join(tokens) == "hello world"


def test_stream_converse_turn_emits_heartbeat_while_idle(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    def run_turn() -> dict[str, str]:
        # The model is "thinking": block with NO tokens written, long enough for
        # at least one heartbeat to fire.
        time.sleep(0.25)
        return {"reply": ""}

    async def _collect() -> str:
        frames = []
        async for frame in streaming.stream_converse_turn(
            run_turn=run_turn,
            extract_tokens=streaming.assistant_text_fragments,
            transcript_path=transcript,
            reconcile=lambda turn: {"reply": turn["reply"]},
            poll_seconds=0.02,
            heartbeat_seconds=0.05,
        ):
            frames.append(frame.decode("utf-8"))
        return "".join(frames)

    raw = asyncio.run(_collect())
    # An idle stream emitted at least one keep-alive comment to hold the socket...
    assert ": keep-alive" in raw
    # ...and the comment is invisible to the SSE event parser (no phantom event).
    events = _parse_sse(raw)
    names = [name for name, _ in events]
    assert names[0] == "open"
    assert names[-1] == "result"
    assert "keep-alive" not in names


def test_stream_converse_turn_heartbeat_disabled_with_zero(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    def run_turn() -> dict[str, str]:
        time.sleep(0.25)
        return {"reply": ""}

    async def _collect() -> str:
        frames = []
        async for frame in streaming.stream_converse_turn(
            run_turn=run_turn,
            extract_tokens=streaming.assistant_text_fragments,
            transcript_path=transcript,
            reconcile=lambda turn: {"reply": turn["reply"]},
            poll_seconds=0.02,
            heartbeat_seconds=0,
        ):
            frames.append(frame.decode("utf-8"))
        return "".join(frames)

    # heartbeat_seconds=0 disables keep-alives entirely (the env knob must be
    # able to turn the feature off, not be floored to a 1s spam).
    assert ": keep-alive" not in asyncio.run(_collect())
