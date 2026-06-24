"""Best-effort runtime wiring for Alfred's local memory layer."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .result import ClaudeResult

_LOG = logging.getLogger(__name__)

BEGIN_MARKER = "ALFRED_MEMORY_REFLECTIONS_JSON"
END_MARKER = "END_ALFRED_MEMORY_REFLECTIONS_JSON"
_MEMORY_BLOCK_RE = re.compile(
    rf"(?:^|\n){BEGIN_MARKER}\s*(.*?)\s*{END_MARKER}(?:\n|$)",
    re.DOTALL,
)
_VALID_SEVERITIES = {"info", "warning", "blocker"}
_REFLECTION_MODES = {"direct", "candidate", "off"}


@dataclass(frozen=True)
class MemoryReflection:
    """One durable lesson parsed from an engine response."""

    body: str
    tags: tuple[str, ...] = ()
    severity: str = "info"


def load_runtime_memory(env: Mapping[str, str] | None = None):
    """Return the configured memory provider, or ``None`` on any failure."""
    try:
        from memory.config import load_provider

        return load_provider(env=env)
    except Exception:
        _LOG.exception("memory runtime: provider load failed")
        return None


_DEFAULT_RECALL_THRESHOLD = 0.0


def _recall_relevance_threshold(env: Mapping[str, str] | None = None) -> float:
    """Minimum AMS similarity a recalled lesson needs to be injected.

    Config-driven via ``ALFRED_MEMORY_RECALL_THRESHOLD`` (a similarity in
    ``[0, 1]``, higher is stricter). Default ``0.0`` preserves the historical
    "inject everything recall returned" behavior; raise it to suppress weakly
    related lessons. Lessons whose provider reports no score are never dropped
    by the threshold (the gate cannot judge them).
    """
    raw = (env or os.environ).get("ALFRED_MEMORY_RECALL_THRESHOLD")
    if raw is None or not str(raw).strip():
        return _DEFAULT_RECALL_THRESHOLD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RECALL_THRESHOLD
    return max(0.0, min(1.0, value))


def _normalized_body(body: str) -> str:
    """Whitespace- and case-folded body used as a dedup key."""
    return " ".join(str(body or "").split()).strip().casefold()


def _iter_scored_providers(provider) -> Iterator[Any]:
    """Yield ``provider`` and any chained sub-providers exposing ``recall_scored``."""
    seen: list[int] = []
    candidates = getattr(provider, "providers", None)
    pool = candidates if isinstance(candidates, list) else [provider]
    for candidate in pool:
        if candidate is None or id(candidate) in seen:
            continue
        seen.append(id(candidate))
        if hasattr(candidate, "recall_scored"):
            yield candidate


def _recall_scored_lessons(
    provider,
    *,
    codename: str,
    repo: str,
    query: str | None,
    limit: int,
) -> list[tuple[object, float | None]] | None:
    """Return scored lessons from the first scored-capable provider, or ``None``.

    ``None`` signals "no provider can report scores; fall back to plain recall".
    """
    for candidate in _iter_scored_providers(provider):
        try:
            scored = candidate.recall_scored(codename=codename, repo=repo, query=query, limit=limit)
        except Exception:
            _LOG.exception("memory runtime: recall_scored failed")
            continue
        return list(scored)
    return None


def _gated_lessons(
    provider,
    *,
    codename: str,
    repo: str,
    query: str | None,
    limit: int,
    threshold: float,
) -> list[object]:
    """Recall lessons, gate by relevance threshold, and dedupe by body.

    Prefers the scored recall path so the threshold can act on real AMS
    similarity. Falls back to plain ``recall`` (threshold inapplicable, dedup
    still applied) for providers without scores so existing behavior is never
    weakened.
    """
    scored = _recall_scored_lessons(
        provider, codename=codename, repo=repo, query=query, limit=limit
    )
    if scored is None:
        lessons = provider.recall(codename=codename, repo=repo, query=query, limit=limit)
        pairs: list[tuple[object, float | None]] = [(lesson, None) for lesson in lessons]
    else:
        pairs = scored
    out: list[object] = []
    seen_bodies: set[str] = set()
    for lesson, score in pairs:
        # A reported score below threshold is dropped; an absent score (None)
        # is always kept (the gate cannot judge it).
        if score is not None and score < threshold:
            continue
        key = _normalized_body(getattr(lesson, "body", ""))
        if not key or key in seen_bodies:
            continue
        seen_bodies.add(key)
        out.append(lesson)
    return out


def format_memory_context(
    provider,
    *,
    codename: str,
    repo: str,
    query: str | None = None,
    limit: int = 3,
) -> str:
    """Return prompt-ready memory context, or an empty string.

    Recalled lessons are gated before injection: anything below the configured
    relevance threshold (``ALFRED_MEMORY_RECALL_THRESHOLD``) is dropped, and
    near-duplicate bodies are collapsed so the same lesson is never injected
    twice. This reuses the provider's own scoring rather than always injecting.
    """
    if provider is None or getattr(provider, "name", "") == "null":
        return ""
    threshold = _recall_relevance_threshold()
    try:
        lessons = _gated_lessons(
            provider,
            codename=codename,
            repo=repo,
            query=query,
            limit=limit,
            threshold=threshold,
        )
    except Exception:
        _LOG.exception("memory runtime: recall failed")
        return ""
    if not lessons:
        return ""
    lines = [
        "Alfred memory for this codename and repo:",
        "Use these as hints only. Trust the repository code and current issue first.",
    ]
    for idx, lesson in enumerate(lessons[:limit], start=1):
        severity = "" if getattr(lesson, "severity", "info") == "info" else "!"
        tags = getattr(lesson, "tags", []) or []
        tag_text = f" [{', '.join(tags)}]" if tags else ""
        body = str(getattr(lesson, "body", "")).strip()
        if body:
            lines.append(f"{idx}. {severity}{tag_text} {body}".strip())
    return "\n".join(lines).strip()


def memory_reflection_instructions() -> str:
    """Prompt appendix that lets a firing file durable lessons."""
    return f"""If this firing learned a durable repo convention, recurring bug pattern, or operator preference, append this optional block at the very end of your final message:
{BEGIN_MARKER}
[
  {{"body": "Short durable lesson for next time.", "tags": ["repo-convention"], "severity": "info"}}
]
{END_MARKER}

Only include durable lessons. Do not include secrets, tokens, customer data, stack traces with private values, or facts that are already obvious from nearby code."""


def with_memory_prompt(
    prompt: str,
    provider,
    *,
    codename: str,
    repo: str | None,
    query: str | None = None,
    limit: int = 3,
) -> str:
    """Prepend recall context and append reflection instructions when enabled."""
    if provider is None or not repo or getattr(provider, "name", "") == "null":
        return prompt
    context = format_memory_context(
        provider,
        codename=codename,
        repo=repo,
        query=query,
        limit=limit,
    )
    chunks = []
    if context:
        chunks.append(context)
    chunks.append(prompt)
    chunks.append(memory_reflection_instructions())
    return "\n\n".join(chunks)


def parse_memory_reflections(text: str) -> list[MemoryReflection]:
    """Parse all memory-reflection blocks from ``text``."""
    reflections: list[MemoryReflection] = []
    for match in _MEMORY_BLOCK_RE.finditer(text or ""):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            body = str(item.get("body") or "").strip()
            if not body:
                continue
            raw_tags = item.get("tags") or []
            tags: tuple[str, ...] = ()
            if isinstance(raw_tags, list):
                tags = tuple(str(tag).strip() for tag in raw_tags if str(tag).strip())
            severity = str(item.get("severity") or "info").strip().lower()
            if severity not in _VALID_SEVERITIES:
                severity = "info"
            reflections.append(MemoryReflection(body=body, tags=tags, severity=severity))
    return reflections


def strip_memory_reflections(text: str) -> str:
    """Remove machine-readable memory blocks from user-facing result text."""
    return _MEMORY_BLOCK_RE.sub("\n", text or "").strip()


def _iter_writable_memory_providers(provider) -> Iterator[object]:
    providers = getattr(provider, "providers", None)
    if isinstance(providers, list):
        yield from providers
    elif provider is not None:
        yield provider


def record_reflections(
    provider,
    reflections: Iterable[MemoryReflection],
    *,
    codename: str,
    repo: str,
    firing_id: str,
) -> int:
    """Persist parsed lessons. Returns the count written."""
    if provider is None:
        return 0
    # Default changed from direct lesson writes to reviewable candidates so
    # engine-generated memories never enter recall without operator review.
    mode = os.environ.get("ALFRED_MEMORY_REFLECTION_MODE", "candidate").strip().lower()
    if mode not in _REFLECTION_MODES:
        mode = "candidate"
    if mode == "off":
        return 0
    written = 0
    for reflection in reflections:
        try:
            if mode == "candidate":
                stored = False
                for candidate in _iter_writable_memory_providers(provider):
                    brain = getattr(candidate, "brain", None)
                    if brain is None or not hasattr(brain, "propose_memory"):
                        continue
                    brain.propose_memory(
                        codename=codename,
                        repo=repo,
                        body=reflection.body,
                        tags=reflection.tags,
                        severity=reflection.severity,
                        source="engine-reflection",
                        source_firing_id=firing_id,
                        confidence=0.6,
                    )
                    stored = True
                    break
                if not stored:
                    raise NotImplementedError("no candidate-capable memory provider")
            else:
                provider.reflect(
                    codename=codename,
                    repo=repo,
                    body=reflection.body,
                    tags=reflection.tags,
                    severity=reflection.severity,
                    firing_id=firing_id,
                )
            written += 1
        except NotImplementedError:
            continue
        except Exception:
            _LOG.exception("memory runtime: reflect failed")
    return written


def record_firing(
    provider,
    *,
    codename: str,
    repo: str,
    firing_id: str,
    result: ClaudeResult,
    engine_used: str,
) -> bool:
    """Best-effort write of the firing audit row into fleet-brain."""
    status = "ok" if result.success else "blocked"
    if result.subtype in {"error_max_turns", "error_timeout", "parse-failed"}:
        status = "partial"
    summary = f"engine={engine_used} subtype={result.subtype} turns={result.num_turns}"
    for candidate in _iter_writable_memory_providers(provider):
        brain = getattr(candidate, "brain", None)
        if brain is None or not hasattr(brain, "firing_log"):
            continue
        try:
            brain.firing_log(
                firing_id=firing_id,
                codename=codename,
                repo=repo,
                status=status,
                summary=summary,
                cost_cents=round(result.cost_usd * 100),
                sentinel=result.subtype,
                finished_at=datetime.now(UTC),
            )
            if status != "ok" and hasattr(brain, "record_failure"):
                try:
                    brain.record_failure(
                        codename=codename,
                        repo=repo,
                        firing_id=firing_id,
                        subtype=result.subtype,
                        summary=summary,
                        engine=engine_used,
                        severity="warning",
                    )
                except Exception:
                    _LOG.exception("memory runtime: record_failure failed")
            return True
        except Exception:
            _LOG.exception("memory runtime: firing_log failed")
    return False
