"""Result classification, provider-error envelope detection.

Pins ``_build_claude_result``'s behaviour for the four common failure
shapes (auth, budget, rate-limit, overload) plus the false-positive
guards that prevent healthy completions from being misclassified.

Each test poses a synthetic raw final-event dict, runs it through
``_build_claude_result``, and asserts the resulting subtype +
success boolean.

Three families of assertions:
  1. Real envelope detection, when the API flags is_error=true and the
     body shape matches a known pattern, classification must pin the
     specific subtype (not the generic ``error_api`` fallback).
  2. False-positive guards, healthy completions that mention "rate-
     limit" / "/login" / etc. in implementation prose must NOT be
     reclassified.
  3. Defensive path, when is_error is false but the body carries a
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
        "result": "You're out of extra usage · resets 5:50pm (UTC)",
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
    """Engineering work commonly mentions "rate-limit", adding handling,
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
    """The overload regex is strict, it requires a JSON envelope OR an
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
# Healthy baseline, sanity check that the new layer doesn't break
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


# ---------------------------------------------------------------------------
# Auth-retry helper, gates the post-401 quarantine+retry path inside
# claude_invoke.
# ---------------------------------------------------------------------------


def test_should_retry_claude_auth_requires_classification_match(monkeypatch) -> None:
    """The helper must NOT trigger a retry when the result is anything
    other than ``error_authentication``. A result classifying as e.g.
    ``error_rate_limit`` or ``success`` should not quarantine the
    credential file (that would cause spurious re-auths)."""
    import agent_runner as ar

    quarantine_called = {"n": 0}

    def fake_quarantine(reason):
        quarantine_called["n"] += 1
        return True

    monkeypatch.setattr(ar, "_quarantine_stale_claude_credentials", fake_quarantine)

    healthy = ar.ClaudeResult(
        success=True,
        subtype="success",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="",
        raw={},
        stop_reason="end_turn",
    )
    assert ar._should_retry_claude_auth(healthy, already_retried=False) is False
    assert quarantine_called["n"] == 0

    rate_limited = ar.ClaudeResult(
        success=False,
        subtype="error_rate_limit",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="",
        raw={},
        stop_reason="error",
    )
    assert ar._should_retry_claude_auth(rate_limited, already_retried=False) is False
    assert quarantine_called["n"] == 0


def test_should_retry_claude_auth_does_not_retry_twice(monkeypatch) -> None:
    """The re-entry guard ``already_retried=True`` must prevent a second
    quarantine + retry. Without this guard a persistent 401 would loop
    forever, deleting credential caches each round."""
    import agent_runner as ar

    monkeypatch.setattr(ar, "_quarantine_stale_claude_credentials", lambda _r: True)

    auth_failed = ar.ClaudeResult(
        success=False,
        subtype="error_authentication",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="Failed to authenticate",
        raw={},
        stop_reason="error",
    )
    assert ar._should_retry_claude_auth(auth_failed, already_retried=False) is True
    assert ar._should_retry_claude_auth(auth_failed, already_retried=True) is False


def test_should_retry_skipped_when_no_credential_file_to_quarantine(monkeypatch) -> None:
    """When ``_quarantine_stale_claude_credentials`` returns False (no
    file to move, or operator disabled the repair via env var), the
    helper must not signal a retry, there's nothing to fix, retrying
    would loop on the same 401."""
    import agent_runner as ar

    monkeypatch.setattr(ar, "_quarantine_stale_claude_credentials", lambda _r: False)

    auth_failed = ar.ClaudeResult(
        success=False,
        subtype="error_authentication",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="Not logged in",
        raw={},
        stop_reason="error",
    )
    assert ar._should_retry_claude_auth(auth_failed, already_retried=False) is False


def test_quarantine_disabled_by_env_var(monkeypatch, tmp_path) -> None:
    """``ALFRED_DISABLE_CLAUDE_AUTH_REPAIR=1`` must short-circuit the
    quarantine before any filesystem I/O. Operators on hosts that store
    Claude creds elsewhere can opt out of the auto-repair entirely."""
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DISABLE_CLAUDE_AUTH_REPAIR", "1")
    # Even if the credential file exists, the env-disabled path returns
    # False without touching it. Use a fake config dir under tmp_path.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    fake_creds = tmp_path / ".credentials.json"
    fake_creds.write_text('{"foo": "bar"}')
    assert ar._quarantine_stale_claude_credentials("test") is False
    # File untouched.
    assert fake_creds.exists()
    assert fake_creds.read_text() == '{"foo": "bar"}'


def test_anthropic_subscription_cap_classified_as_rate_limit() -> None:
    """Anthropic's "subscription cap exceeded" response uses misleading
    wording that looks like a workspace-admin policy block but is
    actually a soft rate limit. Without classifying it as
    ``error_rate_limit`` the result falls through to generic
    ``error_api``, so the retry/breaker layer reports the wrong
    operator-actionable cause.

    Regression test for the 2026-05-24 incident: ~70 firings burned
    through the subscription cap and the whole fleet went red because
    none of the existing patterns matched this specific wording.
    """
    raw = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "stop_reason": "stop_sequence",
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "result": (
            "Your organization has disabled Claude subscription access for "
            "Claude Code · Use an Anthropic API key instead, or ask your "
            "admin to enable access"
        ),
    }
    r = _build_claude_result(raw)
    assert r.success is False
    assert r.subtype == "error_rate_limit", (
        "must classify as error_rate_limit so hybrid agents fall back to codex"
    )
    assert r.stop_reason == "error"


def test_workspace_admin_policy_block_wording_also_classified() -> None:
    """Shorter / paraphrased versions of the same Anthropic wording also
    classify as rate-limit. The regex needs enough flex to catch the
    common variations of "subscription access ... Claude Code"."""
    for body in (
        "Anthropic has disabled Claude subscription access for Claude Code in this workspace.",
        "Subscription access for Claude Code is currently disabled.",
        "subscription access has been temporarily restricted on Claude Code",
    ):
        raw = {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "stop_reason": "stop_sequence",
            "num_turns": 1,
            "total_cost_usd": 0.0,
            "result": body,
        }
        r = _build_claude_result(raw)
        assert r.subtype == "error_rate_limit", f"body did not match: {body!r}"
