"""Transcript and codex artifact path resolution + small parsers.

This module owns the on-disk transcript surface:

* :func:`transcript_path` — canonical path for a Claude streaming JSONL
  transcript.
* :func:`codex_artifact_paths` — paths for ``last-message`` / ``stdout``
  / ``stderr`` from a non-interactive Codex run.
* :func:`_extract_codex_session_id` and :func:`_extract_codex_tokens` —
  tiny line-scanning helpers used by the codex invoker to recover
  session ID and tokens-used from the human-readable output.

What this module does NOT own:

* Writing or reading the Claude streaming JSONL itself (currently a
  TODO — see ``process.claude_invoke_streaming``).
* Parsing the transcript bodies into events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .paths import CODEX_TRANSCRIPTS_ROOT, TRANSCRIPTS_ROOT


def transcript_path(agent: str, firing_id: str) -> Path:
    """Resolve the transcript file path for an ``(agent, firing_id)`` pair.

    Convention:
    ``${ALFRED_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl``.
    Currently no transcripts are written (see
    ``process.claude_invoke_streaming``), but the path resolver ships
    now so consumer agents and downstream log viewers don't need to
    change when streaming lands.
    """
    month = datetime.now(UTC).strftime("%Y-%m")
    return TRANSCRIPTS_ROOT / agent / month / f"{firing_id}.jsonl"


def codex_artifact_paths(agent: str, firing_id: str) -> dict[str, Path]:
    """Canonical artifact paths for a non-interactive Codex run.

    Returns a mapping with keys ``last_message`` / ``stdout`` /
    ``stderr``. The containing directory is created (idempotent) so the
    invoker can simply ``.write_text(...)`` against the returned paths.
    """
    month = datetime.now(UTC).strftime("%Y-%m")
    directory = CODEX_TRANSCRIPTS_ROOT / agent / month
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "last_message": directory / f"{firing_id}.last.md",
        "stdout": directory / f"{firing_id}.stdout.txt",
        "stderr": directory / f"{firing_id}.stderr.txt",
    }


def _extract_codex_session_id(text: str) -> str | None:
    """Find ``session id:`` in Codex output; return the value or ``None``."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("session id:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def _extract_codex_tokens(text: str) -> int:
    """Read the ``tokens used`` integer from Codex output. ``0`` if absent."""
    lines = [line.strip() for line in (text or "").splitlines()]
    for idx, line in enumerate(lines):
        if line == "tokens used" and idx + 1 < len(lines):
            raw = lines[idx + 1].replace(",", "")
            if raw.isdigit():
                return int(raw)
    return 0
