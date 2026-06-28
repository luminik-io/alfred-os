"""Prompt context budgeting before engine invocation.

Alfred already gives agents durable memory and read-only code graph tools. This
module keeps the final prompt sent to Claude or Codex inside a local character
budget so huge issue bodies, logs, or generated context cannot waste a firing on
an avoidable provider-side context failure.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_FALSEY = {"0", "false", "no", "off", ""}
_DEFAULT_MAX_CHARS = 220_000
_DEFAULT_HEAD_CHARS = 130_000
_DEFAULT_TAIL_CHARS = 70_000
_MIN_MAX_CHARS = 4_096
_MIN_EDGE_CHARS = 512


@dataclass(frozen=True)
class ContextGovernance:
    """Metadata for one prompt context-governor pass."""

    applied: bool
    original_chars: int
    final_chars: int
    omitted_chars: int
    max_chars: int
    head_chars: int
    tail_chars: int
    reason: str

    def as_raw(self) -> dict[str, int | str | bool]:
        return {
            "applied": self.applied,
            "original_chars": self.original_chars,
            "final_chars": self.final_chars,
            "omitted_chars": self.omitted_chars,
            "max_chars": self.max_chars,
            "head_chars": self.head_chars,
            "tail_chars": self.tail_chars,
            "reason": self.reason,
        }


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip().replace("_", ""))
    except ValueError:
        return default


def context_governor_enabled(env: Mapping[str, str] | None = None) -> bool:
    resolved = env or os.environ
    raw = resolved.get("ALFRED_CONTEXT_GOVERNOR")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def _config(env: Mapping[str, str]) -> tuple[int, int, int]:
    max_chars = max(
        _MIN_MAX_CHARS,
        _env_int(env, "ALFRED_CONTEXT_MAX_CHARS", _DEFAULT_MAX_CHARS),
    )
    head_chars = max(
        _MIN_EDGE_CHARS,
        _env_int(env, "ALFRED_CONTEXT_HEAD_CHARS", _DEFAULT_HEAD_CHARS),
    )
    tail_chars = max(
        _MIN_EDGE_CHARS,
        _env_int(env, "ALFRED_CONTEXT_TAIL_CHARS", _DEFAULT_TAIL_CHARS),
    )
    return max_chars, head_chars, tail_chars


def _marker(omitted_chars: int, max_chars: int) -> str:
    return (
        "\n\n[ALFRED_CONTEXT_GOVERNOR "
        f"omitted_chars={omitted_chars} max_chars={max_chars}]\n"
        "Middle context omitted to keep this firing inside the local prompt budget. "
        "Trust the repository, tests, memory, and code-memory MCP tools over any "
        "missing middle text.\n"
        "[/ALFRED_CONTEXT_GOVERNOR]\n\n"
    )


def govern_prompt_context(
    prompt: str,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ContextGovernance]:
    """Return ``prompt`` compacted to the configured character budget.

    The governor is intentionally simple and deterministic: keep the beginning
    where system/task instructions usually live, keep the tail where fresh issue
    or run context usually lands, and replace the middle with an explicit marker.
    It does not summarize or invent facts. Agents must use tools to recover any
    omitted detail.
    """

    resolved_env = env or os.environ
    original_chars = len(prompt)
    max_chars, requested_head, requested_tail = _config(resolved_env)
    if not context_governor_enabled(resolved_env):
        return prompt, ContextGovernance(
            applied=False,
            original_chars=original_chars,
            final_chars=original_chars,
            omitted_chars=0,
            max_chars=max_chars,
            head_chars=original_chars,
            tail_chars=0,
            reason="disabled",
        )
    if original_chars <= max_chars:
        return prompt, ContextGovernance(
            applied=False,
            original_chars=original_chars,
            final_chars=original_chars,
            omitted_chars=0,
            max_chars=max_chars,
            head_chars=original_chars,
            tail_chars=0,
            reason="within_budget",
        )

    head_chars = requested_head
    tail_chars = requested_tail
    placeholder = _marker(0, max_chars)
    available = max(_MIN_EDGE_CHARS * 2, max_chars - len(placeholder))
    if head_chars + tail_chars > available:
        head_ratio = requested_head / max(1, requested_head + requested_tail)
        head_chars = max(_MIN_EDGE_CHARS, int(available * head_ratio))
        tail_chars = max(_MIN_EDGE_CHARS, available - head_chars)
    if head_chars + tail_chars > available:
        overflow = head_chars + tail_chars - available
        tail_drop = min(tail_chars - _MIN_EDGE_CHARS, overflow)
        tail_chars -= tail_drop
        overflow -= tail_drop
        if overflow > 0:
            head_chars = max(_MIN_EDGE_CHARS, head_chars - overflow)

    omitted_chars = max(0, original_chars - head_chars - tail_chars)
    marker = _marker(omitted_chars, max_chars)
    while head_chars + len(marker) + tail_chars > max_chars:
        if tail_chars > _MIN_EDGE_CHARS:
            tail_chars -= 1
        elif head_chars > _MIN_EDGE_CHARS:
            head_chars -= 1
        else:
            break
        omitted_chars = max(0, original_chars - head_chars - tail_chars)
        marker = _marker(omitted_chars, max_chars)

    compacted = prompt[:head_chars] + marker + prompt[original_chars - tail_chars :]
    return compacted, ContextGovernance(
        applied=True,
        original_chars=original_chars,
        final_chars=len(compacted),
        omitted_chars=omitted_chars,
        max_chars=max_chars,
        head_chars=head_chars,
        tail_chars=tail_chars,
        reason="over_budget",
    )
