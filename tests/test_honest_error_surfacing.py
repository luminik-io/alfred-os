"""Tests for honest subtype reporting and hybrid fallback audit metadata.

Hybrid fallback now fires only for capability failures. Provider auth, budget,
and rate-limit failures surface on the original engine instead of being hidden
behind a Codex attempt. These tests pin:

* ``reported_subtype`` reports the raw result subtype.
* ``invoke_agent_engine`` stamps ``fallback_from_subtype`` on the Codex result
  when a Claude capability failure triggered the fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from agent_runner import (  # noqa: E402
    ClaudeResult,
    invoke_agent_engine,
    reported_subtype,
)


def _result(subtype: str, *, success: bool = False, **kw) -> ClaudeResult:
    return ClaudeResult(
        success=success,
        subtype=subtype,
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="",
        raw={},
        **kw,
    )


# --------------------------------------------------------------------------
# reported_subtype
# --------------------------------------------------------------------------
def test_reported_subtype_does_not_rewrite_fallback_trigger() -> None:
    result = _result("error_rate_limit", fallback_from_subtype="error_authentication")
    assert reported_subtype(result) == "error_rate_limit"


def test_reported_subtype_returns_subtype_when_no_fallback() -> None:
    result = _result("error_rate_limit")
    assert reported_subtype(result) == "error_rate_limit"


def test_reported_subtype_keeps_result_limit_when_trigger_was_also_a_limit() -> None:
    result = _result("error_rate_limit", fallback_from_subtype="error_rate_limit")
    assert reported_subtype(result) == "error_rate_limit"


def test_reported_subtype_returns_auth_result() -> None:
    result = _result("error_authentication", fallback_from_subtype="error_authentication")
    assert reported_subtype(result) == "error_authentication"


def test_reported_subtype_success_unchanged() -> None:
    result = _result("success", success=True)
    assert reported_subtype(result) == "success"


# --------------------------------------------------------------------------
# invoke_agent_engine fallback stamping
# --------------------------------------------------------------------------
def _engine_kwargs():
    return {
        "agent": "test",
        "firing_id": "fid",
        "workdir": Path("."),
        "claude_allowed_tools": "Read",
        "timeout": 10,
    }


def test_hybrid_stamps_fallback_trigger_on_codex_result() -> None:
    """When Claude hits a capability gap in hybrid mode and codex then
    rate-limits, the returned codex result carries fallback_from_subtype so
    callers can report the original trigger as the root cause.

    Note: only CAPABILITY failures trigger the fallback now (transient
    failures retry the same engine, fatal failures surface honestly), so
    the trigger here is ``error_max_turns``, not auth."""
    captured: list[ClaudeResult] = []

    def fake_claude(*a, **kw):
        return _result("error_max_turns")

    def fake_codex(*a, **kw):
        return _result("error_rate_limit")

    result, engine_used = invoke_agent_engine(
        "prompt",
        engine="hybrid",
        claude_fn=fake_claude,
        codex_fn=fake_codex,
        on_fallback=captured.append,
        **_engine_kwargs(),
    )

    assert engine_used == "codex-fallback"
    assert result.subtype == "error_rate_limit"
    assert result.fallback_from_subtype == "error_max_turns"
    # The Codex subtype is the honest headline; the Claude trigger is audit
    # metadata for the fallback path.
    assert reported_subtype(result) == "error_rate_limit"
    # on_fallback fired with the ORIGINAL claude failure.
    assert captured and captured[0].subtype == "error_max_turns"


def test_hybrid_no_fallback_leaves_trigger_unset() -> None:
    def fake_claude(*a, **kw):
        return _result("success", success=True)

    def fake_codex(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("codex should not run when claude succeeds")

    result, engine_used = invoke_agent_engine(
        "prompt",
        engine="hybrid",
        claude_fn=fake_claude,
        codex_fn=fake_codex,
        **_engine_kwargs(),
    )

    assert engine_used == "claude"
    assert result.fallback_from_subtype is None
    assert reported_subtype(result) == "success"
