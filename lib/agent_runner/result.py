"""``ClaudeResult`` dataclass plus provider-error envelope detection.

This module owns the structured shape that every engine returns and the
classifier that decides whether a "success-looking" response actually
contains a provider-side error envelope leaked into the result text.

Public surface:

* :class:`ClaudeResult` dataclass: the engine-agnostic return type.
* :data:`STOP_REASON_HEALTHY` / :data:`STOP_REASON_FAIL` frozensets.
* :func:`_derive_success` and :func:`_build_claude_result` builders.
* :func:`dry_run_claude_result` for synthetic dry-run responses.
* The compiled error-envelope regexes (``_OVERLOAD_RESULT_RE``,
  ``_AUTH_RESULT_RE``, ``_BUDGET_RESULT_RE``, ``_RATE_LIMIT_RESULT_RE``)
  exported because the unit tests pin their behaviour.
* :func:`_quarantine_stale_claude_credentials` and
  :func:`_should_retry_claude_auth` for the one-shot auth-repair retry
  path.

What this module does NOT own:

* Shelling out to the Claude or Codex CLIs -> ``process.py``.
* Per-firing ledger / spend tracking -> ``state.py``.
* Transcript reading -> ``transcripts.py``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import _truthy_env
from .paths import HOME

# --------------------------------------------------------------------------
# Stop-reason discipline (ported from pi-mono):
#   end_turn       : assistant finished cleanly
#   tool_use       : assistant stopped to invoke a tool (healthy)
#   stop_sequence  : assistant hit a configured stop sequence (healthy)
#   max_tokens     : assistant ran out of output budget (not a hard error)
#   error          : provider / transport / wrapper error
#   aborted        : run was cancelled (signal, timeout, user kill)
#   None           : claude did not surface stop_reason (older runtime)
#
# success is derived from stop_reason in STOP_REASON_HEALTHY. It is forced
# False when stop_reason is in STOP_REASON_FAIL, regardless of the legacy
# subtype heuristic. When stop_reason is absent or "max_tokens", success
# falls back to the legacy subtype check so already-deployed agents keep
# their existing behaviour.
# --------------------------------------------------------------------------
STOP_REASON_HEALTHY: frozenset[str] = frozenset({"end_turn", "tool_use", "stop_sequence"})
STOP_REASON_FAIL: frozenset[str] = frozenset({"error", "aborted"})


# --------------------------------------------------------------------------
# Provider-error envelope detection
#
# Claude Code's ``-p`` mode sometimes returns ``subtype=success`` and a
# healthy ``stop_reason`` even when the underlying API call hit a
# rate-limit, an auth failure, an Anthropic 529 overload, or a usage cap.
# The error body leaks into ``result_text`` and (usually) ``is_error=true``
# is also set. We detect the four common shapes so spend tracking, retry,
# and fleet-wide global block work correctly.
#
# Trigger discipline:
#  * ``is_error=true`` is the primary trigger. When the API explicitly
#    flags the response as an error envelope, we trust it; the regex is
#    just a sanity boost.
#  * Without ``is_error=true``, we require the strict regex match against
#    the JSON error envelope shape. Bare prose mentioning "HTTP 500" or
#    "service unavailable" no longer flips a healthy stop_reason.
#  * Auth + budget regexes match CLI-specific phrasing tight enough to
#    scan against result_text without false-positiving on engineering
#    prose.
#  * Rate-limit detection is the loose one: ``\brate-limit\b`` matches
#    common implementation prose like "added rate-limit handling". Its
#    haystack drops result_text when ``is_error=false`` so a healthy PR
#    summary cannot get reclassified.
# --------------------------------------------------------------------------
_OVERLOAD_RESULT_RE = re.compile(
    # Anthropic JSON error envelope.
    r'"type"\s*:\s*"error"[^\n]{0,400}?"type"\s*:\s*"overloaded_error"'
    # Literal "API Error" CLI prefix paired with overloaded_error on the same line.
    r"|(?m:^API Error[^\n]{0,400}overloaded_error)"
    # Anthropic 529 explicitly.
    r"|\bHTTP\s*529\b"
    r"|\b529\b\s*[:.\-]\s*(?:overloaded|too\s+many\s+requests)"
    # Bedrock throttle inside an error envelope (not bare prose).
    r'|"type"\s*:\s*"error"[^\n]{0,400}?[Bb]edrock[^\n]{0,400}?throttl(?:ing|ed)'
    r'|"type"\s*:\s*"error"[^\n]{0,400}?throttl(?:ing|ed)[^\n]{0,400}?[Bb]edrock',
    re.IGNORECASE,
)

_AUTH_RESULT_RE = re.compile(
    r"authentication_(?:error|failed)|failed to authenticate|invalid authentication credentials"
    r"|\bAPI Error:\s*401\b|\b401\b[^\n]{0,120}authentication"
    r"|not logged in|please run /login",
    re.IGNORECASE,
)

_BUDGET_RESULT_RE = re.compile(
    r"\b(?:you(?:'re| are) out of extra usage|you(?:'ve| have) hit your usage limit)\b"
    r"|\bout of extra usage\b",
    re.IGNORECASE,
)

_RATE_LIMIT_RESULT_RE = re.compile(
    r"\brate[_ -]?limit(?:ed|_exceeded| exceeded)?\b"
    r"|\b429\b|\btoo many requests\b|\bquota exceeded\b"
    # Anthropic emits this wording when a subscription-backed Claude
    # Code session hits its hidden subscription cap. The message reads
    # as a workspace-admin policy block ("your organization has
    # disabled Claude subscription access for Claude Code · Use an
    # Anthropic API key instead, or ask your admin to enable access"),
    # but the actual cause is the cap, and the response shape is the
    # same as a rate limit. Treating it as ``error_rate_limit`` lets
    # the retry/breaker layer handle it as a provider wall instead of a
    # generic API failure with misleading operator guidance.
    r"|\bdisabled Claude subscription access\b"
    r"|\bClaude subscription access for Claude Code\b"
    r"|\bsubscription access.{0,40}Claude Code\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Result dataclass + builders
# --------------------------------------------------------------------------


@dataclass
class ClaudeResult:
    """Engine-agnostic invocation result.

    Returned by every engine adapter (Claude, Codex, Ollama). Legacy
    callers read the first five fields; newer callers also use
    ``stop_reason`` + ``error_message``.
    """

    success: bool
    subtype: str  # "success" | "error_max_turns" | "error_budget" | ...
    num_turns: int
    cost_usd: float
    session_id: str | None
    result_text: str
    raw: dict
    # Additive (opt-in) fields. Existing agents that read only the five
    # legacy fields keep working unchanged.
    stop_reason: str | None = None
    error_message: str | None = None
    # Set on a fallback result (Codex after a Claude capability failure in
    # hybrid mode) to the subtype of the Claude failure that triggered the
    # fallback. This is audit context; it no longer rewrites the reported
    # result subtype. ``None`` when no fallback ran.
    fallback_from_subtype: str | None = None


def _derive_success(subtype: str, stop_reason: str | None) -> bool:
    """Map ``(subtype, stop_reason)`` to a single ``success`` boolean.

    ``stop_reason`` wins when it carries a definite signal. We fall back
    to the legacy subtype heuristic only when ``stop_reason`` is ``None``
    or ``"max_tokens"`` so callers see the same answer they did before
    the stop-reason field was introduced.
    """
    if stop_reason in STOP_REASON_FAIL:
        return False
    if stop_reason in STOP_REASON_HEALTHY:
        return True
    # stop_reason is None or "max_tokens" or some new value we don't
    # model yet, fall back to the legacy heuristic for backward compat.
    return subtype == "success"


def _build_claude_result(raw: dict, *, fallback_text: str = "") -> ClaudeResult:
    """Build a ``ClaudeResult`` from the parsed final JSON event.

    Centralises the ``stop_reason -> success`` mapping plus
    envelope-shape error reclassification so tests hit the same code
    path the runtime hits.
    """
    subtype = raw.get("subtype", "missing")
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        stop_reason = str(stop_reason)

    result_text = raw.get("result", "") or ""

    strict_haystack = "\n".join(
        str(raw.get(key) or "")
        for key in ("error", "error_message", "errorMessage", "api_error_status")
    )
    is_error_flag = bool(raw.get("is_error"))
    full_haystack = f"{result_text}\n{strict_haystack}"
    looks_auth = bool(_AUTH_RESULT_RE.search(full_haystack))
    looks_budget = bool(_BUDGET_RESULT_RE.search(full_haystack))
    rate_limit_haystack = full_haystack if is_error_flag else strict_haystack
    looks_rate_limit = bool(_RATE_LIMIT_RESULT_RE.search(rate_limit_haystack))
    looks_overloaded = bool(_OVERLOAD_RESULT_RE.search(result_text))

    def mark_error(new_subtype: str) -> None:
        nonlocal subtype, stop_reason
        subtype = new_subtype
        stop_reason = "error"

    if is_error_flag:
        # Primary path: the API said is_error=true. Trust that and pin
        # the subtype specific so auth failures don't masquerade as overloads.
        if stop_reason in STOP_REASON_FAIL:
            pass
        elif looks_budget:
            mark_error("error_budget")
        elif looks_auth:
            mark_error("error_authentication")
        elif looks_overloaded:
            mark_error("error_overloaded")
        elif looks_rate_limit:
            mark_error("error_rate_limit")
        elif stop_reason in STOP_REASON_HEALTHY and str(subtype).startswith("error"):
            # Claude can report e.g. subtype=error_max_turns with
            # stop_reason=tool_use. Preserve the specific subtype while
            # forcing success=False via stop_reason=error.
            mark_error(str(subtype))
        elif stop_reason in STOP_REASON_HEALTHY or (stop_reason is None and subtype == "success"):
            mark_error("error_api")
    elif stop_reason in STOP_REASON_HEALTHY:
        # Defensive path: is_error missing/false but the body carries a
        # genuine provider error marker. The strict regexes make this
        # safe enough for the wrapper edge cases we have seen live.
        if looks_budget:
            mark_error("error_budget")
        elif looks_auth:
            mark_error("error_authentication")
        elif looks_overloaded:
            mark_error("error_overloaded")
        elif looks_rate_limit:
            mark_error("error_rate_limit")

    error_message: str | None = None
    if stop_reason in STOP_REASON_FAIL:
        for key in ("error_message", "errorMessage", "error", "api_error_status"):
            val = raw.get(key)
            if val:
                error_message = str(val)
                break
        if not error_message:
            text = result_text or fallback_text
            error_message = (
                text or f"claude stop_reason={stop_reason}"
            ).strip() or f"claude stop_reason={stop_reason}"

    return ClaudeResult(
        success=_derive_success(subtype, stop_reason),
        subtype=subtype,
        num_turns=int(raw.get("num_turns", 0) or 0),
        cost_usd=float(raw.get("total_cost_usd", 0) or 0),
        session_id=raw.get("session_id"),
        result_text=result_text,
        raw=raw,
        stop_reason=stop_reason,
        error_message=error_message,
    )


def dry_run_claude_result(
    prompt: str,
    *,
    model: str | None = None,
    engine: str = "claude",
    num_turns: int = 3,
) -> ClaudeResult:
    """Build a clearly-marked synthetic :class:`ClaudeResult` for dry-run.

    Returned instead of shelling out to ``claude`` / ``codex``. ``success``
    is True with ``subtype="success"`` so the lifecycle flows down the
    happy path; ``cost_usd`` is always ``0.0``, a dry-run never spends.
    The ``result_text`` is explicitly labelled so a runner that echoes
    it (and a human watching the trace) can never mistake it for real
    model output.
    """
    label_model = model or "(cli-default)"
    text = (
        f"[dry-run] synthetic {engine} result, no LLM was invoked. "
        f"Would have called {engine} with a prompt of {len(prompt)} chars, "
        f"model={label_model}."
    )
    return ClaudeResult(
        success=True,
        subtype="success",
        num_turns=num_turns,
        cost_usd=0.0,
        session_id=f"dry-run-{engine}-session",
        result_text=text,
        raw={"dry_run": True, "engine": engine, "prompt_chars": len(prompt)},
        stop_reason="end_turn",
        error_message=None,
    )


# --------------------------------------------------------------------------
# Stale Claude credential repair
# --------------------------------------------------------------------------


def _claude_credentials_file() -> Path:
    """Return Claude Code's legacy disk credential cache path.

    Current Claude Code on macOS uses Keychain as the live credential
    store, but older or stale ``.credentials.json`` files can still be
    picked up by non-interactive subprocesses and produce a 401 despite
    ``claude auth status`` reporting logged in. We never delete the
    file; we quarantine it and let the CLI fall back to Keychain on the
    retry.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if config_dir:
        return Path(config_dir).expanduser() / ".credentials.json"
    return HOME / ".claude" / ".credentials.json"


def _quarantine_stale_claude_credentials(reason: str) -> bool:
    """Move a stale Claude credential cache out of the way, if present.

    Disabled by setting ``ALFRED_DISABLE_CLAUDE_AUTH_REPAIR=1``. Returns
    True only when a file was moved and a retry is worth attempting.
    """
    if _truthy_env("ALFRED_DISABLE_CLAUDE_AUTH_REPAIR"):
        return False
    path = _claude_credentials_file()
    if not path.exists():
        return False
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak.auth-failed-{stamp}")
    try:
        path.replace(target)
    except OSError as exc:
        print(
            f"[claude-auth-repair] could not quarantine {path}: {exc}",
            file=sys.stderr,
        )
        return False
    print(
        f"[claude-auth-repair] quarantined stale credential cache {path} -> {target} "
        f"after {reason}; retrying once",
        file=sys.stderr,
    )
    return True


def _should_retry_claude_auth(result: ClaudeResult, *, already_retried: bool) -> bool:
    """Decide whether to retry once after an authentication failure.

    True only when (a) we have not retried this firing yet AND (b) the
    result classified as ``error_authentication`` AND (c) quarantining
    a stale ``.credentials.json`` actually moved a file out of the way.
    Lets the CLI fall back to Keychain on the retry.
    """
    return (
        not already_retried
        and result.subtype == "error_authentication"
        and _quarantine_stale_claude_credentials(result.error_message or result.result_text)
    )
