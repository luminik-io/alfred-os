"""Rolling conversation condenser for long Ask/Slack chats and long autonomous runs.

Long conversations and long autonomous runs blow past the model's context budget.
Naively truncating the head loses the original task; naively truncating the tail
loses the live working state. This module condenses the MIDDLE instead: it keeps
the opening turns (the system framing plus the person's original task/intent)
verbatim, keeps the most recent ``keep_last`` turns intact, and replaces the run
of turns in between with one compact, model-written summary block.

Design (OpenHands-style, adapted to Alfred's turn model):

* ``keep_first`` opening turns are preserved verbatim. The default of 1 keeps the
  original task/intent; raising it preserves an explicit system turn plus the task.
* ``keep_last`` recent turns are preserved verbatim so the live working state is
  never summarized away.
* The middle run is summarized by an injected ``summarize`` callable (a cheap
  model) into a single ``system``-role summary turn. The summarizer is injected,
  never imported, so this module has zero model dependency and unit tests stay
  deterministic.
* PROACTIVE: :func:`condense` runs when a conversation crosses a configurable
  turn-count or character-budget threshold. Below the threshold the conversation
  is returned untouched, so short chats behave exactly as before.
* REACTIVE: :func:`condense_on_overflow` is the condense-and-retry helper for a
  context-overflow error. It forces a condensation pass (ignoring the proactive
  threshold) so a caller that just hit an overflow can retry with a smaller
  prompt instead of failing the turn.
* AUDITABLE: every condensation returns a :class:`CondensationRecord` carrying the
  list of summarized turn indices, the summary text, and counts. The record is a
  first-class value the caller can persist (see :func:`persist_record`) so the
  summary is auditable and durable facts could later be promoted to memory.

The module never touches the memory subsystem; it only emits a record that a
caller MAY route to memory. Condensing a transcript loses no durable fact that
was not already in the live tail, because the summary is appended, never silently
dropped.

Public surface:

* :class:`Turn` -- the minimal structural Protocol a message must satisfy
  (``role`` + ``content``), so Compose ``ConverseMessage`` and Slack turns both
  fit without conversion.
* :class:`CondenserConfig` -- the env-driven, overridable thresholds.
* :class:`CondensationRecord` -- the auditable summary record.
* :class:`CondensationResult` -- the condensed message list plus the optional
  record.
* :func:`condense` -- the proactive entrypoint.
* :func:`condense_on_overflow` -- the reactive entrypoint.
* :func:`looks_like_context_overflow` -- the overflow-error classifier.
* :func:`persist_record` -- write a record to disk as auditable JSON.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "CondensationRecord",
    "CondensationResult",
    "CondenserConfig",
    "SUMMARY_ROLE",
    "Turn",
    "condense",
    "condense_on_overflow",
    "looks_like_context_overflow",
    "persist_record",
]

# The role stamped on the synthesized summary turn. ``system`` so a downstream
# prompt builder renders it as framing context, not as another user/assistant
# message. Callers that constrain roles to user/assistant (the Compose transcript
# does) should treat the summary as a system block separate from the turn list;
# see ``apply`` for how to keep it out of an untrusted-user boundary.
SUMMARY_ROLE = "system"

# Env knobs (config-driven; conservative defaults keep short chats untouched).
ENV_ENABLED = "ALFRED_CONDENSER_ENABLED"
ENV_KEEP_FIRST = "ALFRED_CONDENSER_KEEP_FIRST"
ENV_KEEP_LAST = "ALFRED_CONDENSER_KEEP_LAST"
ENV_TRIGGER_TURNS = "ALFRED_CONDENSER_TRIGGER_TURNS"
ENV_TRIGGER_CHARS = "ALFRED_CONDENSER_TRIGGER_CHARS"
ENV_MAX_SUMMARY_CHARS = "ALFRED_CONDENSER_MAX_SUMMARY_CHARS"

# Conservative defaults. ``trigger_turns`` of 40 means a normal multi-turn chat
# never condenses; only genuinely long conversations do. ``trigger_chars`` of
# 48000 is a coarse second trigger so a few very long turns can also fire it.
DEFAULT_ENABLED = True
DEFAULT_KEEP_FIRST = 1
DEFAULT_KEEP_LAST = 6
DEFAULT_TRIGGER_TURNS = 40
DEFAULT_TRIGGER_CHARS = 48_000
DEFAULT_MAX_SUMMARY_CHARS = 6_000


@runtime_checkable
class Turn(Protocol):
    """The minimal shape a message must have to be condensed.

    ``role`` is ``user`` / ``assistant`` / ``system``; ``content`` is the text.
    Compose's ``ConverseMessage`` and a Slack turn both satisfy this without any
    adapter, so the condenser is reused across surfaces.
    """

    @property
    def role(self) -> str: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True)
class _PlainTurn:
    """A concrete, immutable :class:`Turn` for the synthesized summary block.

    Used internally so the condenser can return a homogeneous list even when the
    caller's turn type is a frozen dataclass we cannot construct generically.
    """

    role: str
    content: str


@dataclass(frozen=True)
class CondenserConfig:
    """Thresholds for the rolling condenser. All knobs are env-overridable.

    ``keep_first`` + ``keep_last`` are always preserved verbatim; the run between
    them is the only candidate for summarization. ``trigger_turns`` /
    ``trigger_chars`` are the proactive thresholds (either one fires).
    """

    enabled: bool = DEFAULT_ENABLED
    keep_first: int = DEFAULT_KEEP_FIRST
    keep_last: int = DEFAULT_KEEP_LAST
    trigger_turns: int = DEFAULT_TRIGGER_TURNS
    trigger_chars: int = DEFAULT_TRIGGER_CHARS
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CondenserConfig:
        """Build a config from process env, clamped so a typo can never break it.

        Missing or unparseable values fall back to the conservative defaults.
        ``keep_first`` and ``keep_last`` are floored at sane minimums so the
        opening task and a live tail are always preserved.
        """
        source = os.environ if env is None else env
        return cls(
            enabled=_truthy(source.get(ENV_ENABLED), default=DEFAULT_ENABLED),
            keep_first=_clamp_int(source.get(ENV_KEEP_FIRST), DEFAULT_KEEP_FIRST, minimum=1),
            keep_last=_clamp_int(source.get(ENV_KEEP_LAST), DEFAULT_KEEP_LAST, minimum=1),
            trigger_turns=_clamp_int(
                source.get(ENV_TRIGGER_TURNS), DEFAULT_TRIGGER_TURNS, minimum=4
            ),
            trigger_chars=_clamp_int(
                source.get(ENV_TRIGGER_CHARS), DEFAULT_TRIGGER_CHARS, minimum=1_000
            ),
            max_summary_chars=_clamp_int(
                source.get(ENV_MAX_SUMMARY_CHARS), DEFAULT_MAX_SUMMARY_CHARS, minimum=500
            ),
        )


@dataclass(frozen=True)
class CondensationRecord:
    """An auditable record of one condensation pass.

    Carries the summary text plus the 0-based indices of the turns it replaced
    (relative to the input list), so the result is auditable and durable facts
    could later be promoted to memory by a caller that owns the memory write.
    """

    summary: str
    summarized_indices: tuple[int, ...]
    kept_first: int
    kept_last: int
    original_turn_count: int
    condensed_turn_count: int
    reason: str  # "proactive" | "reactive_overflow"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def to_dict(self) -> dict[str, object]:
        """Render as plain JSON-serializable data for persistence / audit."""
        return {
            "summary": self.summary,
            "summarized_indices": list(self.summarized_indices),
            "kept_first": self.kept_first,
            "kept_last": self.kept_last,
            "original_turn_count": self.original_turn_count,
            "condensed_turn_count": self.condensed_turn_count,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class CondensationResult:
    """The outcome of a condensation attempt.

    ``messages`` is the (possibly unchanged) turn list to send to the model.
    ``record`` is ``None`` when nothing was condensed (short conversation, or
    summarizer declined); otherwise it is the auditable summary record.
    ``summary_turn`` is the synthesized summary block when one was produced, kept
    separate so a caller can route it outside an untrusted-user boundary.
    """

    messages: list[Turn]
    record: CondensationRecord | None = None
    summary_turn: Turn | None = None

    @property
    def condensed(self) -> bool:
        """True when this pass actually summarized a middle run."""
        return self.record is not None


# A summarizer takes the run of middle turns and returns a compact summary string
# (or an empty string to decline, in which case the conversation is left intact).
# Injected by the caller so this module never imports a model engine.
class Summarizer(Protocol):
    def __call__(self, turns: Sequence[Turn]) -> str: ...


def _total_chars(messages: Sequence[Turn]) -> int:
    return sum(len(turn.content or "") for turn in messages)


def _should_condense(messages: Sequence[Turn], config: CondenserConfig) -> bool:
    """Proactive trigger: too many turns, or too many characters."""
    if not config.enabled:
        return False
    if len(messages) > config.trigger_turns:
        return True
    return _total_chars(messages) > config.trigger_chars


def _middle_bounds(message_count: int, config: CondenserConfig) -> tuple[int, int]:
    """Return the ``[start, end)`` slice of the summarizable middle run.

    Empty (``start >= end``) when keeping first + last already covers everything,
    so a conversation with no real middle is never needlessly summarized.
    """
    start = config.keep_first
    end = message_count - config.keep_last
    return start, end


def _render_summary_turn(summary: str, config: CondenserConfig) -> _PlainTurn:
    text = (summary or "").strip()[: config.max_summary_chars]
    body = (
        "Earlier in this conversation, the following was established "
        "(condensed summary of older turns, preserved for continuity):\n\n"
        f"{text}"
    )
    return _PlainTurn(role=SUMMARY_ROLE, content=body)


def _do_condense(
    messages: Sequence[Turn],
    *,
    summarize: Summarizer,
    config: CondenserConfig,
    reason: str,
) -> CondensationResult:
    """Run one condensation pass unconditionally (the proactive gate is upstream).

    Keeps ``keep_first`` head turns and ``keep_last`` tail turns verbatim,
    summarizes the middle into one block, and returns the rebuilt list plus an
    audit record. If there is no real middle to summarize, or the summarizer
    declines (empty string), the original list is returned untouched.
    """
    msgs = list(messages)
    start, end = _middle_bounds(len(msgs), config)
    if start >= end:
        # Nothing between the kept head and the kept tail; leave as-is.
        return CondensationResult(messages=msgs)

    middle = msgs[start:end]
    summary = summarize(middle)
    if not (summary or "").strip():
        # Summarizer declined; do not drop turns we cannot summarize.
        return CondensationResult(messages=msgs)

    summary_turn = _render_summary_turn(summary, config)
    condensed: list[Turn] = [*msgs[:start], summary_turn, *msgs[end:]]
    record = CondensationRecord(
        summary=summary_turn.content,
        summarized_indices=tuple(range(start, end)),
        kept_first=start,
        kept_last=config.keep_last,
        original_turn_count=len(msgs),
        condensed_turn_count=len(condensed),
        reason=reason,
    )
    return CondensationResult(messages=condensed, record=record, summary_turn=summary_turn)


def condense(
    messages: Sequence[Turn],
    *,
    summarize: Summarizer,
    config: CondenserConfig | None = None,
) -> CondensationResult:
    """Proactively condense ``messages`` when they cross the configured threshold.

    Below the threshold (or when disabled) the conversation is returned untouched
    so short chats behave exactly as before. Above it, the middle run is replaced
    with one summary block and an audit record is attached.
    """
    config = config or CondenserConfig.from_env()
    if not _should_condense(messages, config):
        return CondensationResult(messages=list(messages))
    return _do_condense(messages, summarize=summarize, config=config, reason="proactive")


def condense_on_overflow(
    messages: Sequence[Turn],
    *,
    summarize: Summarizer,
    config: CondenserConfig | None = None,
) -> CondensationResult:
    """Reactively condense after a context-overflow error (condense-and-retry).

    Forces a condensation pass regardless of the proactive threshold, so a caller
    that just hit an overflow can retry with a smaller prompt instead of failing.
    Honors ``keep_first`` / ``keep_last`` so the original task and the live tail
    survive the forced pass. Returns the original list untouched only when there
    is no summarizable middle (the prompt is already minimal) or the summarizer
    declines, in which case the caller should surface the overflow honestly.
    """
    config = config or CondenserConfig.from_env()
    return _do_condense(messages, summarize=summarize, config=config, reason="reactive_overflow")


# --------------------------------------------------------------------------
# Context-overflow classifier (reactive trigger)
# --------------------------------------------------------------------------
#
# Providers phrase a context-window overflow several ways. We match the common
# shapes tight enough to avoid false positives on ordinary engineering prose
# (a PR that merely mentions "context window" must not trip this).
_OVERFLOW_RE = re.compile(
    r"\bcontext[_ ]?(?:length|window)[^\n]{0,60}?(?:exceed|too\s+long|too\s+large|maximum|limit)"
    r"|\b(?:exceed|exceeded)[^\n]{0,40}?context[_ ]?(?:length|window)"
    r"|\bprompt\s+is\s+too\s+long\b"
    r"|\binput\s+(?:is\s+)?too\s+long\b"
    r"|\bmaximum\s+context\s+length\b"
    r"|\btoo\s+many\s+(?:input\s+)?tokens\b"
    r'|"type"\s*:\s*"invalid_request_error"[^\n]{0,200}?(?:context|tokens|too\s+long)',
    re.IGNORECASE,
)


def looks_like_context_overflow(text: str | None) -> bool:
    """True when ``text`` looks like a provider context-window overflow error.

    Used by callers to decide whether to take the reactive condense-and-retry
    path. Deliberately strict so ordinary prose mentioning "context window" does
    not trigger a needless condensation.
    """
    if not text:
        return False
    return bool(_OVERFLOW_RE.search(text))


# --------------------------------------------------------------------------
# Persistence (auditable, memory-promotable record)
# --------------------------------------------------------------------------


def persist_record(record: CondensationRecord, *, record_dir: Path, slug: str = "") -> Path:
    """Write a condensation record to disk as auditable JSON.

    Records bucket under ``record_dir`` so an operator (or a later memory-promote
    pass) can audit exactly which turns were summarized and what the summary said.
    Returns the path written. The caller owns ``record_dir`` (e.g. the serve state
    root), so this module stays free of any path policy of its own.
    """
    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    safe_slug = _slugify(slug)
    name = f"condense-{stamp}{('-' + safe_slug) if safe_slug else ''}.json"
    path = record_dir / name
    path.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def with_summary_in_role(result: CondensationResult, *, as_role: str) -> CondensationResult:
    """Re-stamp the summary turn's role (e.g. to ``user``) for a constrained surface.

    Compose wraps user/assistant turns in an untrusted boundary and coerces any
    other role to ``user``. A caller that must keep the summary inside that turn
    list can re-stamp the summary block's role so it survives that coercion as a
    clearly-labelled summary, rather than being silently relabeled. Returns the
    result unchanged when nothing was condensed.
    """
    if result.record is None or result.summary_turn is None:
        return result
    restamped = _PlainTurn(role=as_role, content=result.summary_turn.content)
    new_messages: list[Turn] = []
    replaced = False
    for turn in result.messages:
        if not replaced and turn is result.summary_turn:
            new_messages.append(restamped)
            replaced = True
        else:
            new_messages.append(turn)
    return replace(result, messages=new_messages, summary_turn=restamped)


# --------------------------------------------------------------------------
# Small env / string helpers (mirrors agent_runner.config conventions without
# importing the runtime, so the condenser stays dependency-light and reusable).
# --------------------------------------------------------------------------


def _truthy(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clamp_int(raw: str | None, default: int, *, minimum: int, maximum: int | None = None) -> int:
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError:
            value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug[:48]
