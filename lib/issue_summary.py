"""Plain-English issue/plan summaries for Slack surfaces.

The fleet's Slack posts have historically carried a bare link + title for a
filed issue or an approved plan. An operator scanning ``#alfred`` sees *that*
something was filed, but not *what* it actually is. This module closes that
gap: ``summarize_issue`` asks the engine Alfred already uses (Claude / Codex
via ``invoke_agent_engine``) for a short, plain-English summary of what an
issue changes, why it matters, and its blast radius.

Design contract
---------------

- **Bounded + fast.** The engine call runs with a short timeout (default 25s,
  ``ALFRED_ISSUE_SUMMARY_TIMEOUT``). It is never on the critical path: every
  failure mode (engine off, timeout, empty output, exception) falls back
  instantly to a trimmed first paragraph of the body, or the title.
- **Strict short output.** The prompt asks for 2-3 short lines. The result is
  hard-capped (default ~360 chars, ``ALFRED_ISSUE_SUMMARY_MAX_CHARS``) so a
  runaway model cannot bloat a Slack context block.
- **Injectable engine.** ``engine_invoke`` is a callable injected by the
  caller (or resolved from env via :func:`default_engine_invoke`). It takes a
  prompt string and returns the model's raw text, or raises / returns empty on
  failure. Injection keeps the whole path testable without the network.
- **Off by default.** With no engine configured the helper returns the
  deterministic fallback. Callers wire it in optionally; a forked install with
  no engine still gets a useful (if terse) summary line.

No em-dashes in any prompt or operator-facing string here (fleet rule).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

# Env knobs (12-factor; all optional with safe defaults).
ENV_ENABLED = "ALFRED_ISSUE_SUMMARY_ENABLED"
ENV_TIMEOUT = "ALFRED_ISSUE_SUMMARY_TIMEOUT"
ENV_MAX_CHARS = "ALFRED_ISSUE_SUMMARY_MAX_CHARS"
ENV_ENGINE = "ALFRED_ISSUE_SUMMARY_ENGINE"

DEFAULT_TIMEOUT = 25
DEFAULT_MAX_CHARS = 360
# Floor so an operator cannot misconfigure the cap down to nothing and lose
# every summary to a 1-char trim.
_MIN_MAX_CHARS = 80

# Type of the injected engine call: prompt -> raw model text.
EngineInvoke = Callable[[str], str]


def summarize_issue(
    title: str,
    body: str,
    *,
    engine_invoke: EngineInvoke | None = None,
    max_chars: int | None = None,
) -> str:
    """Return a short plain-English summary of an issue or plan.

    Args:
        title: the issue/plan title.
        body: the issue/plan body (markdown is fine; only used as raw text).
        engine_invoke: optional callable ``(prompt) -> raw_text``. When
            ``None`` the helper skips the engine entirely and returns the
            deterministic fallback. Inject a real engine (or
            :func:`default_engine_invoke`) to get a model-written summary.
        max_chars: optional hard cap override. Defaults to
            ``ALFRED_ISSUE_SUMMARY_MAX_CHARS`` (or 360).

    The engine call is best-effort: any failure (exception, empty output,
    refusal) falls back to a trimmed first paragraph of the body, then the
    title. This function never raises.
    """
    cap = _resolve_max_chars(max_chars)
    fallback = _fallback_summary(title, body, cap)

    if engine_invoke is None:
        return fallback

    prompt = build_summary_prompt(title, body)
    try:
        raw = engine_invoke(prompt)
    except Exception as exc:  # engine must never crash the Slack post
        print(
            f"[issue-summary] engine_invoke raised: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return fallback

    cleaned = _clean_engine_output(raw)
    if not cleaned:
        return fallback
    return _cap(_operator_voice_clean(cleaned), cap)


def build_summary_prompt(title: str, body: str) -> str:
    """Build the engine prompt asking for a short plain summary.

    The prompt pins three things the operator wants on every line: what
    changes, why it matters, and the blast radius. It demands a strict short
    output so the model does not pad. No em-dashes (fleet rule).
    """
    # Trim the body fed to the model: a 5k-line issue body wastes tokens and
    # the summary only needs the gist. The model still gets title + the lead.
    body_excerpt = _cap((body or "").strip(), 4000)
    title_text = (title or "").strip() or "(no title)"
    return (
        "You are summarizing a GitHub issue or engineering plan for a Slack "
        "message so an operator instantly understands what it is.\n\n"
        "Write 2 to 3 short plain-English lines covering, in order:\n"
        "1. WHAT changes (the concrete action).\n"
        "2. WHY it matters (the user or system payoff).\n"
        "3. BLAST RADIUS (which area or service is touched, and how risky).\n\n"
        "Rules: plain language, no jargon, no preamble, no markdown headers, "
        "no bullet characters, no quotes around the answer. Do not use "
        "em-dashes. Keep the whole answer under 60 words. Output only the "
        "summary text, nothing else.\n\n"
        f"TITLE: {title_text}\n\n"
        f"BODY:\n{body_excerpt or '(no body provided)'}\n"
    )


def default_engine_invoke(*, workdir: Path | None = None) -> EngineInvoke | None:
    """Resolve an engine-backed invoker from env, or ``None`` if disabled.

    Returns ``None`` (so the caller falls back to the deterministic summary)
    unless BOTH ``ALFRED_ISSUE_SUMMARY_ENABLED`` is truthy AND an engine is
    resolvable. Mirrors ``planning_assistant.engine_refiner_from_env``: the
    actual engine call is deferred behind a closure so importing this module
    never drags in ``agent_runner`` until a summary is actually requested.
    """
    if not _env_flag(ENV_ENABLED):
        return None
    engine = (os.environ.get(ENV_ENGINE) or "").strip() or "hybrid"
    timeout = _env_int(ENV_TIMEOUT, DEFAULT_TIMEOUT)
    root = workdir or Path.cwd()

    def _invoke(prompt: str) -> str:
        try:
            from agent_runner import invoke_agent_engine
        except Exception:
            return ""
        firing_id = datetime.now(UTC).strftime("issue-summary-%Y%m%d-%H%M%S")
        result, _engine_used = invoke_agent_engine(
            prompt,
            engine=engine,
            agent="issue-summary",
            firing_id=firing_id,
            workdir=root,
            claude_allowed_tools="",
            timeout=timeout,
            claude_max_turns=1,
            codex_timeout=timeout,
        )
        if not getattr(result, "success", False):
            return ""
        return getattr(result, "result_text", "") or ""

    return _invoke


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _fallback_summary(title: str, body: str, cap: int) -> str:
    """Deterministic summary when the engine is unavailable or fails.

    Prefer the first non-empty paragraph of the body (the lead usually
    describes the issue); fall back to the title. Markdown noise (headers,
    bullets, blockquotes) is stripped so the Slack context block reads clean.
    """
    paragraph = _first_paragraph(body)
    if paragraph:
        return _cap(_operator_voice_clean(paragraph), cap)
    return _cap(_operator_voice_clean((title or "").strip()), cap)


def _first_paragraph(body: str) -> str:
    """Return the first meaningful paragraph of ``body`` as one clean line.

    Splits on blank lines, skips paragraphs that are pure markdown noise
    (a heading line alone, a horizontal rule, an HTML comment), and collapses
    internal whitespace so a multi-line paragraph renders on one Slack line.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in text.split("\n\n"):
        cleaned = _strip_markdown_noise(block)
        if cleaned:
            return cleaned
    return ""


def _strip_markdown_noise(block: str) -> str:
    """Collapse a markdown block to a single clean prose line, or ''.

    A standalone heading line (``## Problem``) is treated as noise and
    dropped entirely rather than demoted to prose, so a body that opens with
    a section heading yields the first real sentence, not the heading word.
    """
    lines: list[str] = []
    for raw_line in block.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("<!--") or set(line) <= {"-", "=", "*", "_"}:
            # HTML comment or a horizontal-rule / setext-underline line.
            continue
        if re.match(r"^#{1,6}\s+", line):
            # A markdown heading carries no summary value on its own.
            continue
        # Drop leading bullet / blockquote markers, keep the prose.
        line = re.sub(r"^[>\-*+]\s+", "", line)
        if line:
            lines.append(line)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def _clean_engine_output(raw: str) -> str:
    """Normalize raw engine text into a tidy multi-line summary.

    Strips surrounding code fences / quotes, drops empty lines, and joins the
    surviving lines with single newlines so the Slack context block keeps the
    model's 2-3 line shape. Returns '' when nothing survives.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Strip a wrapping code fence if the model added one.
    fence = re.match(r"^```[\w-]*\n(.*)\n```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    lines = [line.strip().strip('"').strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _cap(text: str, limit: int) -> str:
    """Hard-cap ``text`` to ``limit`` chars with a visible ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keep = max(1, limit - 3)
    return text[:keep].rstrip() + "..."


def _operator_voice_clean(text: str) -> str:
    """Normalize output before Slack render to honor workspace voice rules."""
    text = (text or "").replace("\u2014", " - ").replace("\u2013", " - ")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _resolve_max_chars(override: int | None) -> int:
    if override is not None:
        return max(_MIN_MAX_CHARS, int(override))
    return max(_MIN_MAX_CHARS, _env_int(ENV_MAX_CHARS, DEFAULT_MAX_CHARS))


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT",
    "ENV_ENABLED",
    "ENV_ENGINE",
    "ENV_MAX_CHARS",
    "ENV_TIMEOUT",
    "EngineInvoke",
    "build_summary_prompt",
    "default_engine_invoke",
    "summarize_issue",
]
