"""Result classification — provider-error envelope detection.

Pins ``_build_claude_result``'s behaviour for the four common failure
shapes (auth, budget, rate-limit, overload) plus the false-positive
guards that prevent healthy completions from being misclassified.

Each test poses a synthetic raw final-event dict, runs it through
``_build_claude_result``, and asserts the resulting subtype +
success boolean.

Three families of assertions:
  1. Real envelope detection — when the API flags is_error=true and the
     body shape matches a known pattern, classification must pin the
     specific subtype (not the generic ``error_api`` fallback).
  2. False-positive guards — healthy completions that mention "rate-
     limit" / "/login" / etc. in implementation prose must NOT be
     reclassified.
  3. Defensive path — when is_error is false but the body carries a
     tight CLI-error signal (e.g. "Please run /login"), reclassify.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

from agent_runner import (  # noqa: E402
    _AUTH_RESULT_RE,
    _BUDGET_RESULT_RE,
    _OVERLOAD_RESULT_RE,
    _RATE_LIMIT_RESULT_RE,
    _build_claude_result,
)

# ---------------------------------------------------------------------------
# Family 1: real envelope detection (is_error=true)
# ---------------------------------------------------------------------------


def test_overload_envelope_with_is_error_classified() -> None:
    """Anthropic 529 envelope leaking into result_text."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 18,
        "total_cost_usd": 0.94,
        "result": (
            '{"type":"error","error":'
            '{"type":"overloaded_error","message":"Overloaded"},'
            '"request_id":"req_abc123"}'
        ),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_overloaded"
    assert r.stop_reason == "error"


def test_authentication_envelope_with_is_error_classified() -> None:
    """The morning-cap-cascade pattern: 401 envelope in result_text."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": (
            "Failed to authenticate. API Error: 401 "
            '{"type":"error","error":{"type":"authentication_error",'
            '"message":"Invalid authentication credentials"}}'
        ),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_authentication"
    assert r.stop_reason == "error"


def test_rate_limit_envelope_with_is_error_classified() -> None:
    """Real 429 envelope. The strict regex is loose enough to catch
    ``\\b429\\b`` so the haystack-with-result_text hit when
    is_error=true is enough."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 3,
        "total_cost_usd": 0.0,
        "result": (
            'API Error: 429 {"type":"error","error":'
            '{"type":"rate_limit_error","message":"Too many requests"}}'
        ),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_rate_limit"
    assert r.stop_reason == "error"


def test_extra_usage_envelope_with_is_error_classified() -> None:
    """Claude Code subscription budget exhaustion."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": "You're out of extra usage · resets 5:50pm (Europe/Oslo)",
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_budget"
    assert r.stop_reason == "error"


def test_bedrock_throttle_envelope_classified_as_overload() -> None:
    """Bedrock back-pressure inside an error envelope."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 9,
        "total_cost_usd": 0.3,
        "result": (
            '{"type":"error","error":{"type":"throttling_error",'
            '"message":"Bedrock: too many requests, throttled"}}'
        ),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_overloaded"


# ---------------------------------------------------------------------------
# Family 2: false-positive guards (healthy completions mentioning failure
# vocabulary in implementation prose)
# ---------------------------------------------------------------------------


def test_healthy_prose_mentioning_rate_limit_not_misclassified() -> None:
    """Engineering work commonly mentions "rate-limit" — adding handling,
    fixing 429 retry, etc. A healthy completion summarising that work
    must NOT be flipped to error_rate_limit."""
    cases = [
        "Added rate-limit handling to the connector with exponential backoff.",
        "The 429 rate-limit retries now respect the Retry-After header.",
        "Wired token-bucket rate-limit on the SQS consumer to prevent throttling.",
    ]
    for body in cases:
        raw = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "stop_reason": "end_turn",
            "num_turns": 6,
            "total_cost_usd": 0.18,
            "result": body,
        }
        r = _build_claude_result(raw)
        assert r.success is True, f"prose: {body[:50]!r}"
        assert r.subtype == "success", f"prose: {body[:50]!r}"


def test_healthy_prose_mentioning_overload_words_not_misclassified() -> None:
    """The overload regex is strict — it requires a JSON envelope OR an
    explicit "API Error overloaded_error" / 529 / Bedrock-in-envelope.
    Bare "throttling" or "service unavailable" in prose must not match.
    """
    cases = [
        "I added retry logic for throttling cases on the SQS consumer.",
        "The deployment encountered HTTP 503 service_unavailable but recovered.",
        "Documented the throttling behaviour for downstream consumers.",
    ]
    for body in cases:
        raw = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "stop_reason": "end_turn",
            "num_turns": 4,
            "total_cost_usd": 0.1,
            "result": body,
        }
        r = _build_claude_result(raw)
        assert r.success is True, f"prose: {body[:50]!r}"
        assert r.subtype == "success", f"prose: {body[:50]!r}"


# ---------------------------------------------------------------------------
# Family 3: defensive path (is_error missing but tight CLI signal in body)
# ---------------------------------------------------------------------------


def test_not_logged_in_message_classified_when_is_error_present() -> None:
    """Real Claude CLI emission when not authed: short prose body, but
    is_error=true is set."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": "Not logged in · Please run /login",
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_authentication"
    assert r.stop_reason == "error"


def test_overload_without_is_error_caught_by_strict_envelope() -> None:
    """Defensive: even if Claude omits is_error, a JSON error envelope
    with overloaded_error in result_text reclassifies. The strict regex
    requires the literal envelope shape, not bare prose."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "stop_sequence",
        "num_turns": 12,
        "total_cost_usd": 0.4,
        "result": ('{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_overloaded"


# ---------------------------------------------------------------------------
# Healthy baseline — sanity check that the new layer doesn't break
# already-working classifications.
# ---------------------------------------------------------------------------


def test_healthy_completion_stays_success() -> None:
    """Plain successful run with no error markers anywhere."""
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "num_turns": 5,
        "total_cost_usd": 0.1,
        "result": "Done. Edited 3 files and added 2 tests.",
    }
    r = _build_claude_result(raw)
    assert r.success is True
    assert r.subtype == "success"
    assert r.stop_reason == "end_turn"
    assert r.error_message is None


def test_max_turns_subtype_preserved_when_stop_reason_looks_healthy() -> None:
    """Claude can report ``subtype=error_max_turns`` with
    ``stop_reason=tool_use``. Preserve the specific subtype while
    forcing success=False."""
    raw = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "stop_reason": "tool_use",
        "num_turns": 41,
        "total_cost_usd": 1.4,
        "result": "",
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_max_turns"
    assert r.stop_reason == "error"


# ---------------------------------------------------------------------------
# Regex-level sanity (catches accidental loosening / tightening)
# ---------------------------------------------------------------------------


def test_overload_regex_rejects_bare_http_500_prose() -> None:
    """The overload regex must NOT match generic prose about 5xx errors.
    This is the Codex P1 false-positive guard from upstream alfred."""
    assert _OVERLOAD_RESULT_RE.search("the deployment hit HTTP 500 once") is None
    assert _OVERLOAD_RESULT_RE.search("retry logic for throttling cases") is None


def test_overload_regex_matches_real_envelope() -> None:
    body = '{"type":"error","error":{"type":"overloaded_error","message":"x"}}'
    assert _OVERLOAD_RESULT_RE.search(body) is not None


def test_auth_regex_matches_real_signals() -> None:
    assert _AUTH_RESULT_RE.search("API Error: 401 authentication_error") is not None
    assert _AUTH_RESULT_RE.search("Not logged in · Please run /login") is not None
    assert _AUTH_RESULT_RE.search("Invalid authentication credentials") is not None


def test_budget_regex_matches_real_signals() -> None:
    assert _BUDGET_RESULT_RE.search("You're out of extra usage · resets 5pm") is not None
    assert _BUDGET_RESULT_RE.search("you've hit your usage limit") is not None


def test_rate_limit_regex_matches_real_signals() -> None:
    assert _RATE_LIMIT_RESULT_RE.search("rate_limit_error: too many requests") is not None
    assert _RATE_LIMIT_RESULT_RE.search("HTTP 429") is not None
    assert _RATE_LIMIT_RESULT_RE.search("quota exceeded") is not None
