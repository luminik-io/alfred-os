"""Focused unit tests for ``lib.agent_runner.reliability``.

Covers the four pieces of the self-healing core:

* the failure-classification table (the single source of truth),
* exponential backoff with full jitter honouring ``Retry-After``,
* the per-engine circuit breaker (trip + cooldown + reset),
* loop-fingerprint detection + the hard step ceiling,
* ``retry_with_backoff`` retrying TRANSIENT only.
"""

from __future__ import annotations

import random
import threading
from datetime import UTC, datetime

import pytest


def _result(
    ar,
    subtype: str,
    *,
    success: bool = False,
    stop_reason: str | None = "error",
    error_message: str = "",
    result_text: str = "",
    raw: dict | None = None,
):
    return ar.ClaudeResult(
        success=success,
        subtype=subtype,
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text=result_text,
        raw=raw if raw is not None else {},
        stop_reason=stop_reason,
        error_message=error_message,
    )


# --------------------------------------------------------------------------
# Classification table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subtype,expected",
    [
        ("error_rate_limit", "TRANSIENT"),
        ("error_overloaded", "TRANSIENT"),
        ("error_timeout", "TRANSIENT"),
        ("error_api", "TRANSIENT"),
        ("error_authentication", "FATAL"),
        ("error_budget", "FATAL"),
        ("error_max_turns", "CAPABILITY"),
        ("parse-failed", "CAPABILITY"),
        ("error_loop_detected", "CAPABILITY"),
    ],
)
def test_classify_result_subtype_table(fresh_agent_runner, subtype, expected):
    ar = fresh_agent_runner
    res = _result(ar, subtype)
    assert ar.classify_result(res) is getattr(ar.FailureClass, expected)


def test_classify_result_success_is_none(fresh_agent_runner):
    ar = fresh_agent_runner
    res = _result(ar, "success", success=True, stop_reason="end_turn")
    assert ar.classify_result(res) is ar.FailureClass.NONE


def test_classify_result_aborted_is_transient(fresh_agent_runner):
    ar = fresh_agent_runner
    res = _result(ar, "some-unmapped", stop_reason="aborted")
    assert ar.classify_result(res) is ar.FailureClass.TRANSIENT


def test_classify_result_text_markers(fresh_agent_runner):
    ar = fresh_agent_runner
    transient = _result(
        ar, "unknown", stop_reason="error", error_message="Connection reset by peer"
    )
    assert ar.classify_result(transient) is ar.FailureClass.TRANSIENT
    fatal = _result(ar, "unknown", stop_reason="error", error_message="HTTP 403 forbidden")
    assert ar.classify_result(fatal) is ar.FailureClass.FATAL
    context = _result(
        ar, "unknown", stop_reason="error", result_text="prompt is too long for context window"
    )
    assert ar.classify_result(context) is ar.FailureClass.TRANSIENT


def test_classify_result_posix_too_long_is_not_transient(fresh_agent_runner):
    """A bare ``too long`` POSIX/env error is NOT a transient context overflow.

    ``argument list too long`` (E2BIG), ``filename too long``
    (ENAMETOOLONG), and similar are unrecoverable environment problems: a
    same-engine retry cannot fix them, so they must not classify TRANSIENT
    and burn the retry budget. They fall through to the unplaceable default
    (CAPABILITY) instead.
    """
    ar = fresh_agent_runner
    for message in (
        "OSError: [Errno 7] argument list too long",
        "OSError: [Errno 36] filename too long",
        "path too long for this filesystem",
    ):
        res = _result(ar, "unknown", stop_reason="error", error_message=message)
        assert ar.classify_result(res) is not ar.FailureClass.TRANSIENT, message
    # The genuine context-overflow markers still classify transient.
    overflow = _result(
        ar, "unknown", stop_reason="error", error_message="maximum context length exceeded"
    )
    assert ar.classify_result(overflow) is ar.FailureClass.TRANSIENT


def test_classify_result_unplaceable_defaults_capability(fresh_agent_runner):
    ar = fresh_agent_runner
    res = _result(ar, "totally-unknown", stop_reason="error", error_message="weird")
    assert ar.classify_result(res) is ar.FailureClass.CAPABILITY


def test_classify_exception(fresh_agent_runner):
    ar = fresh_agent_runner
    assert ar.classify_exception(ConnectionResetError("reset")) is ar.FailureClass.TRANSIENT
    assert ar.classify_exception(RuntimeError("permission denied")) is ar.FailureClass.FATAL


# --------------------------------------------------------------------------
# Retry-After extraction
# --------------------------------------------------------------------------


def test_retry_after_numeric_and_clamped(fresh_agent_runner):
    ar = fresh_agent_runner
    assert ar.retry_after_seconds(_result(ar, "error_rate_limit", raw={"retry_after": 12})) == 12.0
    # Clamped to the ceiling.
    huge = _result(ar, "error_rate_limit", raw={"retry_after": 999999})
    assert ar.retry_after_seconds(huge) == 300.0


def test_retry_after_from_headers_and_error_block(fresh_agent_runner):
    ar = fresh_agent_runner
    via_header = _result(ar, "error_rate_limit", raw={"headers": {"retry-after": "30"}})
    assert ar.retry_after_seconds(via_header) == 30.0
    via_error = _result(ar, "error_rate_limit", raw={"error": {"retry_after": 7}})
    assert ar.retry_after_seconds(via_error) == 7.0


def test_retry_after_absent(fresh_agent_runner):
    ar = fresh_agent_runner
    assert ar.retry_after_seconds(_result(ar, "error_rate_limit", raw={})) is None


# --------------------------------------------------------------------------
# Backoff with mandatory full jitter + Retry-After floor
# --------------------------------------------------------------------------


def test_compute_backoff_grows_and_is_jittered(fresh_agent_runner):
    ar = fresh_agent_runner
    rng = random.Random(0)
    # Full jitter: every delay is within [0, window]. Window grows with attempt.
    for attempt in range(1, 5):
        window = min(60.0, 2.0 * (2 ** (attempt - 1)))
        for _ in range(20):
            d = ar.compute_backoff_delay(attempt, base=2, cap=60, rng=rng)
            assert 0.0 <= d <= window


def test_compute_backoff_jitter_varies(fresh_agent_runner):
    ar = fresh_agent_runner
    rng = random.Random(1234)
    draws = {ar.compute_backoff_delay(3, base=2, cap=60, rng=rng) for _ in range(50)}
    # Jitter is mandatory: many distinct values, not a constant.
    assert len(draws) > 10


def test_compute_backoff_honors_retry_after_floor(fresh_agent_runner):
    ar = fresh_agent_runner
    rng = random.Random(0)
    # retry_after dominates a small jittered window.
    d = ar.compute_backoff_delay(1, base=2, cap=60, retry_after=45, rng=rng)
    assert d >= 45.0


def test_compute_backoff_cap(fresh_agent_runner):
    ar = fresh_agent_runner
    rng = random.Random(0)
    for _ in range(50):
        d = ar.compute_backoff_delay(20, base=2, cap=10, rng=rng)
        assert d <= 10.0


# --------------------------------------------------------------------------
# retry_with_backoff: TRANSIENT only
# --------------------------------------------------------------------------


def test_retry_with_backoff_retries_transient_then_succeeds(fresh_agent_runner):
    ar = fresh_agent_runner
    slept: list[float] = []
    outcomes = [
        _result(ar, "error_rate_limit"),
        _result(ar, "error_rate_limit"),
        _result(ar, "success", success=True, stop_reason="end_turn"),
    ]
    calls = {"n": 0}

    def invoke():
        i = calls["n"]
        calls["n"] += 1
        return outcomes[i]

    out = ar.retry_with_backoff(
        invoke,
        classify=ar.classify_result,
        max_retries=5,
        sleep=slept.append,
        rng=random.Random(0),
    )
    assert out.success is True
    assert calls["n"] == 3
    assert len(slept) == 2  # two backoff sleeps before the success


def test_retry_with_backoff_does_not_retry_capability(fresh_agent_runner):
    ar = fresh_agent_runner
    slept: list[float] = []
    calls = {"n": 0}

    def invoke():
        calls["n"] += 1
        return _result(ar, "error_max_turns")

    out = ar.retry_with_backoff(
        invoke,
        classify=ar.classify_result,
        max_retries=5,
        sleep=slept.append,
    )
    assert out.subtype == "error_max_turns"
    assert calls["n"] == 1  # capability is NOT retried
    assert slept == []


def test_retry_with_backoff_respects_retry_after(fresh_agent_runner):
    ar = fresh_agent_runner
    slept: list[float] = []
    calls = {"n": 0}

    def invoke():
        calls["n"] += 1
        if calls["n"] == 1:
            return _result(ar, "error_rate_limit", raw={"retry_after": 50})
        return _result(ar, "success", success=True, stop_reason="end_turn")

    ar.retry_with_backoff(
        invoke,
        classify=ar.classify_result,
        retry_after_of=ar.retry_after_seconds,
        max_retries=3,
        sleep=slept.append,
        rng=random.Random(0),
    )
    assert slept and slept[0] >= 50.0


def test_retry_with_backoff_exhausts_and_returns_last(fresh_agent_runner):
    ar = fresh_agent_runner
    slept: list[float] = []

    def invoke():
        return _result(ar, "error_rate_limit")

    out = ar.retry_with_backoff(
        invoke,
        classify=ar.classify_result,
        max_retries=2,
        sleep=slept.append,
        rng=random.Random(0),
    )
    assert out.subtype == "error_rate_limit"
    assert len(slept) == 2  # exactly max_retries attempts


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def test_breaker_trips_after_threshold_and_opens(fresh_agent_runner):
    ar = fresh_agent_runner
    cb = ar.CircuitBreaker("claude", threshold=3, cooldown_seconds=600)
    assert cb.is_open() is False
    s1 = cb.record_transient_failure(reason="error_rate_limit")
    assert s1.open is False and s1.consecutive == 1
    cb.record_transient_failure()
    s3 = cb.record_transient_failure()
    assert s3.open is True and s3.consecutive == 3
    assert cb.is_open() is True


def test_breaker_success_resets_streak(fresh_agent_runner):
    ar = fresh_agent_runner
    cb = ar.CircuitBreaker("claude", threshold=3)
    cb.record_transient_failure()
    cb.record_transient_failure()
    cb.record_success()
    assert cb.status().consecutive == 0
    assert cb.is_open() is False


def test_breaker_cooldown_expires_then_closes(fresh_agent_runner):
    ar = fresh_agent_runner
    clock = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    cb = ar.CircuitBreaker("codex", threshold=1, cooldown_seconds=300, now=lambda: clock["now"])
    status = cb.record_transient_failure()
    assert status.open is True
    # Still open during the cooldown window.
    clock["now"] = datetime(2026, 1, 1, 0, 4, tzinfo=UTC)
    assert cb.is_open() is True
    # After the cooldown the breaker is half-open: the next call is allowed.
    clock["now"] = datetime(2026, 1, 1, 0, 6, tzinfo=UTC)
    assert cb.is_open() is False


def test_breaker_state_is_shared_across_instances(fresh_agent_runner):
    """Persistence: a second worker reading the same engine sees the trip."""
    ar = fresh_agent_runner
    cb_a = ar.CircuitBreaker("claude", threshold=2, cooldown_seconds=600)
    cb_a.record_transient_failure()
    cb_a.record_transient_failure()
    cb_b = ar.CircuitBreaker("claude", threshold=2, cooldown_seconds=600)
    assert cb_b.is_open() is True


def test_breaker_concurrent_writes_do_not_lose_increments(fresh_agent_runner):
    """Concurrent failure records on the shared ledger keep an exact count.

    Each worker uses its own ``CircuitBreaker`` instance pointed at the
    same on-disk engine file. The exclusive cross-process lock around the
    load -> increment -> store sequence (plus the per-writer-unique temp
    name) means no writer reads a stale count or clobbers another's bytes,
    so every increment lands and the committed counter equals the number
    of writers rather than collapsing to a single surviving write. A high
    threshold keeps the breaker closed so the counter keeps climbing.
    """
    ar = fresh_agent_runner
    workers = 16
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            cb = ar.CircuitBreaker("claude", threshold=10_000, cooldown_seconds=600)
            barrier.wait()
            cb.record_transient_failure()
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    final = ar.CircuitBreaker("claude", threshold=10_000, cooldown_seconds=600)
    status = final.status()
    assert status.consecutive == workers
    # No stray per-writer temp files leaked into the breaker dir.
    leftover = list(final._path.parent.glob("*.tmp"))
    assert leftover == [], leftover


def test_breaker_reset_clears_state(fresh_agent_runner):
    ar = fresh_agent_runner
    cb = ar.CircuitBreaker("claude", threshold=1)
    cb.record_transient_failure()
    assert cb.is_open() is True
    cb.reset()
    assert cb.is_open() is False
    assert cb.status().consecutive == 0


# --------------------------------------------------------------------------
# Loop fingerprint + step ceiling
# --------------------------------------------------------------------------


def test_step_fingerprint_stable_and_distinct(fresh_agent_runner):
    ar = fresh_agent_runner
    a = ar.step_fingerprint("Bash", "ls -la  output")
    b = ar.step_fingerprint("Bash", "ls -la output")  # whitespace-normalised
    c = ar.step_fingerprint("Bash", "different output")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_loop_detector_trips_on_repeated_steps(fresh_agent_runner):
    ar = fresh_agent_runner
    det = ar.LoopDetector(window=3, max_steps=100)
    assert det.observe("Bash", "same") is False
    assert det.observe("Bash", "same") is False
    assert det.observe("Bash", "same") is True  # 3 identical in a row
    assert "loop detected" in (det.tripped_reason or "")


def test_loop_detector_does_not_trip_on_progress(fresh_agent_runner):
    ar = fresh_agent_runner
    det = ar.LoopDetector(window=3, max_steps=100)
    assert det.observe("Bash", "a") is False
    assert det.observe("Bash", "b") is False
    assert det.observe("Bash", "c") is False
    assert det.observe("Bash", "b") is False
    assert det.tripped_reason is None


def test_loop_detector_step_ceiling(fresh_agent_runner):
    ar = fresh_agent_runner
    det = ar.LoopDetector(window=99, max_steps=4)
    results = [det.observe("Tool", f"unique-{i}") for i in range(4)]
    assert results[-1] is True
    assert "step ceiling" in (det.tripped_reason or "")
    assert det.steps == 4


def test_loop_detector_env_overridable(fresh_agent_runner, monkeypatch):
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_LOOP_WINDOW", "2")
    det = ar.LoopDetector()
    assert det.window == 2
    assert det.observe("X", "same") is False
    assert det.observe("X", "same") is True
