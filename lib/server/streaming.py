"""Incremental streaming helpers for ``alfred serve``.

Two read-time streaming features sit here so the route handlers in
``views.py`` stay thin and the shared file takes only small, additive edits:

1. Live log tail (#41). The runtime tees each firing's transcript to JSONL at
   ``state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl`` as it runs. This
   module tails that file from a byte offset, yielding only the bytes that have
   appeared since the caller last read, plus a ``done`` signal once the firing's
   ``events`` log records completion. Two transports share the same offset
   reader: a Server-Sent-Events generator (``tail_transcript_sse``) for the
   ``EventSource`` path and a single offset-poll snapshot
   (``tail_transcript_chunk``) for the JSON fallback.

2. Compose converse token stream (#36). ``run_turn`` already tees the
   interrogator's assistant text to a transcript via ``claude_invoke_streaming``.
   ``stream_converse_turn`` runs that turn on a worker thread while tailing the
   transcript for assistant ``text`` deltas, emitting them as ``token`` SSE
   events, then a final ``result`` event carrying the reconciled turn payload.

Everything here is pure stdlib plus an injected ``state_root``/runner so the
serve tests can drive it against a temp directory and a fake firing without a
live model. Nothing blocks the event loop: file tailing runs in a thread via
``run_in_threadpool``, and the converse turn runs on its own worker thread
while the async generator polls the transcript.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool


def _env_float(name: str, default: float) -> float:
    """Read a float from env, falling back to ``default`` on absence/garbage."""
    try:
        raw = os.environ.get(name)
        return float(raw) if raw is not None and raw.strip() else default
    except (TypeError, ValueError):
        return default

# How long the SSE tail waits between file-size checks while a firing is still
# running. Small enough to feel live, large enough that an idle stream is cheap.
TAIL_POLL_SECONDS = 1.0
# Hard ceiling on how long a single tail stream stays open even if the firing
# never records completion (a crashed runner, a torn events log). The client
# falls back to its 60s poll after this, so the stream is never a silent hang.
TAIL_MAX_SECONDS = 15 * 60
# Bytes read per tail iteration. Transcript lines are normally small. This only
# bounds a single read of a pathological giant line.
TAIL_READ_CHUNK = 256 * 1024


def find_transcript(state_root: Path, firing_id: str) -> Path | None:
    """Locate the transcript JSONL for ``firing_id`` under any codename.

    Mirrors ``transcripts.list_firings`` layout
    (``transcripts/<codename>/<YYYY-MM>/<firing_id>.jsonl``) but searches by
    firing id without needing the codename, which the client does not carry
    into the tail URL. Returns ``None`` when no transcript exists yet.
    """
    if not _safe_id(firing_id):
        return None
    root = state_root / "transcripts"
    if not root.is_dir():
        return None
    target = f"{firing_id}.jsonl"
    for codename_dir in root.iterdir():
        if not codename_dir.is_dir():
            continue
        for month_dir in codename_dir.iterdir():
            if not month_dir.is_dir():
                continue
            candidate = month_dir / target
            if candidate.is_file():
                return candidate
    return None


def firing_is_done(state_root: Path, firing_id: str) -> bool:
    """Best effort: has the firing recorded completion in its events log?

    The events JSONL (``<codename>/events/<firing_id>.jsonl``) gets a terminal
    ``firing_complete`` / ``firing_failed`` record when the run ends. We treat
    either as done so the tail can close cleanly instead of polling forever.
    A missing events file means "still unknown", which keeps the tail open
    until the wall-clock ceiling.
    """
    if not _safe_id(firing_id):
        return True
    for codename_dir in _iter_codename_dirs(state_root):
        events_path = codename_dir / "events" / f"{firing_id}.jsonl"
        if not events_path.is_file():
            continue
        try:
            text = events_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for raw in reversed(text.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = obj.get("event") if isinstance(obj, dict) else None
            if event in {"firing_complete", "firing_failed", "firing_aborted"}:
                return True
        return False
    return False


def _iter_codename_dirs(state_root: Path) -> Iterable[Path]:
    if not state_root.is_dir():
        return []
    reserved = {"transcripts", "codex", "fleet", "engines", "_paused", "firings"}
    out: list[Path] = []
    for child in state_root.iterdir():
        if child.is_dir() and child.name not in reserved:
            out.append(child)
    return out


def _safe_id(firing_id: str) -> bool:
    """Reject path traversal in a caller-supplied firing id."""
    if not firing_id:
        return False
    if "/" in firing_id or "\\" in firing_id or firing_id.startswith("."):
        return False
    return ".." not in firing_id


def tail_transcript_chunk(
    state_root: Path,
    firing_id: str,
    *,
    offset: int = 0,
) -> dict[str, Any]:
    """Read everything appended to the transcript since ``offset``.

    The JSON-poll fallback returns ``{found, offset, lines, done}``:

    * ``found``: whether a transcript exists yet.
    * ``offset``: the new byte offset to send on the next poll.
    * ``lines``: the complete JSONL lines that appeared since ``offset``.
    * ``done``: whether the firing has finished.
    """
    path = find_transcript(state_root, firing_id)
    done = firing_is_done(state_root, firing_id)
    if path is None:
        return {"found": False, "offset": offset, "lines": [], "done": done}
    text, new_offset = _read_from_offset(path, offset)
    lines = [line for line in text.splitlines() if line.strip()]
    return {
        "found": True,
        "offset": new_offset,
        "lines": lines,
        "done": done,
    }


def _read_from_offset(path: Path, offset: int) -> tuple[str, int]:
    """Return text appended after ``offset`` and the new offset.

    Only whole lines are returned. If the file does not end on a newline, the
    trailing partial line is excluded and the offset is left just before it.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "", offset
    if size <= offset:
        return "", offset
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(min(size - offset, TAIL_READ_CHUNK))
    except OSError:
        return "", offset
    if not raw:
        return "", offset
    last_newline = raw.rfind(b"\n")
    if last_newline == -1:
        return "", offset
    whole = raw[: last_newline + 1]
    new_offset = offset + len(whole)
    return whole.decode("utf-8", errors="replace"), new_offset


def _sse(event: str, data: Any) -> bytes:
    """Frame one Server-Sent Event. ``data`` is JSON-encoded on one line."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode()


# Default keep-alive cadence. A silent SSE connection (the model is thinking, or
# a firing is running with no new transcript lines) can be reaped by an idle
# proxy/load-balancer mid-turn; an SSE comment every HEARTBEAT_SECONDS keeps the
# socket warm. Env-overridable per the config-driven-tunables rule.
HEARTBEAT_SECONDS = max(1.0, _env_float("ALFRED_SSE_HEARTBEAT_SECONDS", 15.0))


def _sse_comment() -> bytes:
    """A Server-Sent-Events comment line (keep-alive).

    Comment lines start with ``:`` and carry no ``data:`` field, so a spec
    compliant client (including the desktop ``readSseStream`` parser, which only
    acts on ``event``/``data`` lines) ignores them. They exist solely to push
    bytes through idle proxies so the connection is not reaped mid-turn.
    """
    return b": keep-alive\n\n"


async def tail_transcript_sse(
    state_root: Path,
    firing_id: str,
    *,
    start_offset: int = 0,
    poll_seconds: float = TAIL_POLL_SECONDS,
    max_seconds: float = TAIL_MAX_SECONDS,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[bytes]:
    """Yield SSE frames tailing a firing transcript until it completes.

    Emits:

    * ``open`` once, with the firing id and whether a transcript exists.
    * ``append`` whenever new whole lines appear, carrying ``{lines, offset}``.
    * ``done`` once the firing records completion or the ceiling is hit.

    File I/O runs in the threadpool so the event loop is never blocked.
    """
    offset = max(0, int(start_offset))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_seconds

    found = await run_in_threadpool(lambda: find_transcript(state_root, firing_id) is not None)
    yield _sse("open", {"firing_id": firing_id, "found": found})
    last_frame = loop.time()

    while True:
        chunk = await run_in_threadpool(tail_transcript_chunk, state_root, firing_id, offset=offset)
        offset = int(chunk["offset"])
        if chunk["lines"]:
            yield _sse("append", {"lines": chunk["lines"], "offset": offset})
            last_frame = loop.time()
        elif heartbeat_seconds > 0 and loop.time() - last_frame >= heartbeat_seconds:
            # A running firing with no new transcript lines: keep the socket warm.
            yield _sse_comment()
            last_frame = loop.time()
        if chunk["done"]:
            final = await run_in_threadpool(
                tail_transcript_chunk, state_root, firing_id, offset=offset
            )
            offset = int(final["offset"])
            if final["lines"]:
                yield _sse("append", {"lines": final["lines"], "offset": offset})
            yield _sse("done", {"offset": offset, "reason": "complete"})
            return
        if loop.time() >= deadline:
            yield _sse("done", {"offset": offset, "reason": "timeout"})
            return
        await asyncio.sleep(poll_seconds)


async def stream_converse_turn(
    *,
    run_turn: Callable[[], Any],
    extract_tokens: Callable[[Path], list[str]],
    transcript_path: Path,
    reconcile: Callable[[Any], dict[str, Any]],
    poll_seconds: float = 0.15,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[bytes]:
    """Stream a Compose converse turn token by token, then reconcile.

    ``run_turn`` is the blocking call that runs one interrogator turn. It tees
    assistant text to ``transcript_path`` via ``claude_invoke_streaming`` and
    runs on a worker thread so the event loop stays free. While it runs, this
    helper tails ``transcript_path`` with ``extract_tokens`` and emits each
    newly seen assistant text fragment as a ``token`` SSE event. When the turn
    returns, it emits a single ``result`` event with ``reconcile(turn)``, or an
    ``error`` event when the engine returned nothing usable.
    """
    loop = asyncio.get_event_loop()
    result_box: dict[str, Any] = {}
    done_event = threading.Event()

    def _worker() -> None:
        try:
            result_box["turn"] = run_turn()
        except Exception as exc:
            result_box["error"] = str(exc) or exc.__class__.__name__
        finally:
            done_event.set()

    worker = threading.Thread(target=_worker, name="compose-converse-stream", daemon=True)
    worker.start()

    yield _sse("open", {})

    emitted = 0
    last_frame = loop.time()
    while not done_event.is_set():
        tokens = await run_in_threadpool(_safe_extract, extract_tokens, transcript_path)
        if len(tokens) > emitted:
            for fragment in tokens[emitted:]:
                yield _sse("token", {"text": fragment})
            emitted = len(tokens)
            last_frame = loop.time()
        elif heartbeat_seconds > 0 and loop.time() - last_frame >= heartbeat_seconds:
            # The model is thinking with no new tokens: emit a keep-alive comment
            # so an idle proxy does not reap the connection before the result.
            yield _sse_comment()
            last_frame = loop.time()
        await asyncio.sleep(poll_seconds)

    tokens = await run_in_threadpool(_safe_extract, extract_tokens, transcript_path)
    if len(tokens) > emitted:
        for fragment in tokens[emitted:]:
            yield _sse("token", {"text": fragment})

    await loop.run_in_executor(None, worker.join, 1.0)

    if "error" in result_box:
        yield _sse("error", {"detail": result_box["error"]})
        return
    turn = result_box.get("turn")
    if turn is None:
        yield _sse("error", {"detail": "live_session_unavailable"})
        return
    yield _sse("result", reconcile(turn))


def _safe_extract(extract_tokens: Callable[[Path], list[str]], transcript_path: Path) -> list[str]:
    """Never let a transcript read error abort the token stream."""
    try:
        return extract_tokens(transcript_path)
    except Exception:
        return []


def assistant_text_fragments(transcript_path: Path) -> list[str]:
    """Pull assistant ``text`` blocks in order from a stream-json transcript.

    Each ``claude_invoke_streaming`` transcript line is one stream-json event.
    This collects the ``text`` payload of every assistant ``text`` content block
    in file order. Torn or non-JSON lines are skipped.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    fragments: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            value = block.get("text")
            if isinstance(value, str) and value:
                fragments.append(value)
    return fragments
