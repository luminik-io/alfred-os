"""Transcript reader for stream-JSON firing logs.

Standalone module — no dependency on ``agent_runner`` so it can be imported
on hosts that haven't deployed the full runtime.

Path layout (under ``$ALFRED_HOME/state``, default ``~/.alfred/state``):

    transcripts/<codename>/<YYYY-MM>/<firing_id>.jsonl
    codex/<codename>/<YYYY-MM>/<firing_id>.stdout.txt

Every consumer passes an explicit ``state_dir`` so tests can inject a tmp
path; nothing in this module reads ``$ALFRED_HOME`` directly.

The stream-JSON shape is the one emitted by ``claude -p --output-format
stream-json``: a sequence of newline-separated JSON objects with
``type ∈ {"system", "user", "assistant", "result"}``. We summarise it by
counting tool-use blocks, recording file paths and Bash commands, and
extracting the trailing ``result`` event.

Pure stdlib. Operator runs this with the system Python.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# State directory resolution
# --------------------------------------------------------------------------


def default_state_dir() -> Path:
    """Resolve the operator's state directory.

    Priority order:
      1. ``ALFRED_STATE_DIR`` (explicit override).
      2. ``ALFRED_HOME``/state.
      3. ``~/.alfred/state``.

    The returned path is not required to exist; callers that need a
    populated tree should check ``exists()`` themselves.
    """
    import os

    explicit = os.environ.get("ALFRED_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    alfred_home = os.environ.get("ALFRED_HOME")
    if alfred_home:
        return Path(alfred_home).expanduser() / "state"
    return Path.home() / ".alfred" / "state"


def transcripts_root(state_dir: Path) -> Path:
    return state_dir / "transcripts"


def codex_root(state_dir: Path) -> Path:
    return state_dir / "codex"


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class FiringResult:
    """The trailing ``result`` event from a stream-JSON transcript."""

    subtype: str | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    session_id: str | None = None
    stop_reason: str | None = None


@dataclass
class TranscriptSummary:
    """Aggregate view of one firing transcript."""

    path: str
    tool_calls_total: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)
    bash_commands: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    skills_invoked: list[str] = field(default_factory=list)
    result: FiringResult | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.result is None:
            data["result"] = None
        return data


@dataclass
class FiringRef:
    """Pointer to a firing transcript on disk plus its codename."""

    codename: str
    firing_id: str
    path: Path
    mtime: float

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(self.mtime, tz=UTC)


# --------------------------------------------------------------------------
# Listing transcripts
# --------------------------------------------------------------------------


def list_firings(state_dir: Path, codename: str) -> list[FiringRef]:
    """Return all firing transcripts for ``codename``, newest first."""
    root = transcripts_root(state_dir) / codename
    if not root.is_dir():
        return []
    out: list[FiringRef] = []
    for month_dir in root.iterdir():
        if not month_dir.is_dir():
            continue
        for path in month_dir.glob("*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                logger.debug("skipping unreadable transcript %s", path)
                continue
            out.append(
                FiringRef(
                    codename=codename,
                    firing_id=path.stem,
                    path=path,
                    mtime=mtime,
                )
            )
    out.sort(key=lambda r: r.mtime, reverse=True)
    return out


def find_firing(state_dir: Path, codename: str, firing_id: str) -> FiringRef | None:
    """Return the firing record matching ``firing_id`` or ``None``."""
    for ref in list_firings(state_dir, codename):
        if ref.firing_id == firing_id:
            return ref
    return None


def list_codenames(state_dir: Path) -> list[str]:
    """Return codenames that have at least one transcript directory."""
    root = transcripts_root(state_dir)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


# --------------------------------------------------------------------------
# Summarisation
# --------------------------------------------------------------------------


def transcript_summary(path: Path) -> TranscriptSummary:
    """Summarise a stream-JSON transcript file.

    Returns an empty summary if the file is missing or unreadable. JSON
    decode errors on individual lines are skipped silently — the
    stream-JSON format guarantees well-formed lines but interrupted
    writes can produce torn tails.
    """
    summary = TranscriptSummary(path=str(path))
    if not path.exists():
        return summary
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("could not read transcript %s", path)
        return summary

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        if obj.get("type") == "result" or ("subtype" in obj and "num_turns" in obj):
            summary.result = FiringResult(
                subtype=obj.get("subtype"),
                num_turns=obj.get("num_turns"),
                total_cost_usd=obj.get("total_cost_usd"),
                session_id=obj.get("session_id"),
                stop_reason=obj.get("stop_reason"),
            )
            continue

        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            _record_tool_use(summary, block)

    return summary


def _record_tool_use(summary: TranscriptSummary, block: dict[str, Any]) -> None:
    """Update ``summary`` in place with one tool_use block."""
    name = block.get("name") or ""
    if not name:
        return
    summary.tool_calls_total += 1
    summary.tool_calls_by_name[name] = summary.tool_calls_by_name.get(name, 0) + 1
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return

    if name == "Bash":
        cmd = (inp.get("command") or "")[:200]
        if cmd:
            summary.bash_commands.append(cmd)
    elif name == "Read":
        file_path = inp.get("file_path") or ""
        if file_path:
            summary.files_read.append(file_path)
    elif name == "Edit":
        file_path = inp.get("file_path") or ""
        if file_path:
            summary.files_edited.append(file_path)
    elif name == "Write":
        file_path = inp.get("file_path") or ""
        if file_path:
            summary.files_written.append(file_path)
    elif name == "Skill":
        skill = inp.get("skill") or ""
        if skill:
            summary.skills_invoked.append(skill)


# --------------------------------------------------------------------------
# Codex artifact helpers
# --------------------------------------------------------------------------


_CODEX_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|usage.?limit|quota|\b429\b|too.?many.?requests",
    re.IGNORECASE,
)


def extract_codex_tokens(text: str) -> int:
    """Parse the ``tokens used\\nN`` summary block from a Codex stdout dump.

    Returns 0 when no token summary is present. Codex prints the summary
    only when the run completed cleanly; rate-limited or aborted runs
    have no token line, which is reflected as zero.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if line == "tokens used" and index + 1 < len(lines):
            raw = lines[index + 1].replace(",", "")
            if raw.isdigit():
                return int(raw)
    return 0


def extract_codex_session_id(text: str) -> str | None:
    """Pull the ``session id:`` line out of a Codex stdout dump."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("session id:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def codex_run_hit_rate_limit(text: str) -> bool:
    """Return True when the Codex stdout body contains rate-limit signals."""
    return bool(_CODEX_RATE_LIMIT_RE.search(text or ""))


# --------------------------------------------------------------------------
# Pretty-printing helpers used by the CLI layer
# --------------------------------------------------------------------------


def render_firing_jsonl(path: Path) -> list[str]:
    """Decode a stream-JSON transcript into a list of human-readable lines.

    Returns the raw lines a caller would print one-per-line. The
    rendering is intentionally compact — full payloads are clipped so
    operators can scan a firing without scrolling pages of tool output.
    """
    lines: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("could not read transcript %s: %s", path, exc)
        return lines

    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        rendered = _render_event(obj)
        if rendered:
            lines.append(rendered)
    return lines


def _render_event(obj: dict[str, Any]) -> str | None:
    t = obj.get("type") or obj.get("event_type") or "?"
    if t == "system":
        return f"[system] {obj.get('subtype') or ''}".rstrip()
    if t == "user":
        return _render_user(obj)
    if t == "assistant":
        return _render_assistant(obj)
    if t == "result" or ("subtype" in obj and "num_turns" in obj):
        cost = obj.get("total_cost_usd") or 0
        try:
            cost_str = f"${float(cost):.4f}"
        except (TypeError, ValueError):
            cost_str = "$?"
        return (
            f"[result] subtype={obj.get('subtype')} turns={obj.get('num_turns')} "
            f"cost={cost_str} stop_reason={obj.get('stop_reason')}"
        )
    return None


def _render_user(obj: dict[str, Any]) -> str | None:
    content = (obj.get("message") or {}).get("content") or ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body = block.get("content") or ""
                if isinstance(body, list):
                    body = " ".join(
                        (b.get("text", "") if isinstance(b, dict) else str(b)) for b in body
                    )
                snippet = (str(body) or "").replace("\n", " ")[:120]
                parts.append(f"[tool_result] {snippet}")
        return "\n".join(parts) if parts else None
    snippet = str(content).replace("\n", " ")[:120]
    if not snippet.strip():
        return None
    return f"[user] {snippet}"


def _render_assistant(obj: dict[str, Any]) -> str | None:
    content = (obj.get("message") or {}).get("content") or []
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            snippet = (block.get("text") or "").replace("\n", " ")[:160]
            if snippet.strip():
                parts.append(f"[assistant] {snippet}")
        elif bt == "tool_use":
            parts.append(_render_tool_use_event(block))
    return "\n".join(parts) if parts else None


def _render_tool_use_event(block: dict[str, Any]) -> str:
    name = block.get("name") or "?"
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        return f"[tool_use {name}] (no input)"
    if name == "Bash":
        return f"[tool_use Bash] $ {(inp.get('command') or '')[:160]}"
    if name in {"Read", "Edit", "Write"}:
        return f"[tool_use {name}] {inp.get('file_path') or ''}"
    if name == "Skill":
        return f"[tool_use Skill] /{inp.get('skill') or '?'}"
    return f"[tool_use {name}] {json.dumps(inp)[:140]}"
