"""Resilience tests for :class:`RedisAgentMemoryProvider`.

Covers the retry-with-backoff + in-process circuit breaker added at the
``_request`` choke point, plus the preserved method-level semantics
(``recall`` swallows to ``[]``; ``reflect`` raises) and the
``forget_lesson`` / ``list_lessons`` helpers that back ``ams-reset``.

The transport is injected as a deterministic stub and ``sleep`` /
``clock`` are injected so no real time passes and backoff is not random.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "lib"))

from memory.redis_agent_memory import (  # noqa: E402
    RedisAgentMemoryProvider,
    _AmsHttpError,
    _BreakerState,
)


class _FakeClock:
    """Injectable monotonic clock the test can advance by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _provider(
    transport: Any,
    *,
    max_retries: int = 2,
    breaker_threshold: int = 5,
    breaker_cooldown_s: float = 30.0,
    clock: Any = None,
) -> RedisAgentMemoryProvider:
    sleeps: list[float] = []
    prov = RedisAgentMemoryProvider(
        transport=transport,
        max_retries=max_retries,
        breaker_threshold=breaker_threshold,
        breaker_cooldown_s=breaker_cooldown_s,
        sleep=sleeps.append,
        clock=clock or (lambda: 0.0),
    )
    # Stash the recorder so tests can assert on the number of sleeps.
    prov._test_sleeps = sleeps  # type: ignore[attr-defined]
    return prov


# ---------------------------------------------------------------------------
# Retry / classification
# ---------------------------------------------------------------------------


def test_transient_retries_then_succeeds() -> None:
    """A 503 then a clean response: one retry, then success."""
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _AmsHttpError(503, "service unavailable")
        return {"ok": True}

    prov = _provider(transport, max_retries=2)
    result = prov._request("GET", "/v1/health", None)
    assert result == {"ok": True}
    assert calls["n"] == 2
    assert len(prov._test_sleeps) == 1  # type: ignore[attr-defined]
    # A trailing success closes the breaker streak.
    assert prov._breaker.consecutive == 0


def test_no_status_exception_is_transient_and_bounded() -> None:
    """An injected exception with no HTTP status retries up to max_retries."""
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise ValueError("kaboom")  # unknown, no status -> transient

    prov = _provider(transport, max_retries=2)
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    # 1 initial + 2 retries = 3 attempts; 2 sleeps.
    assert calls["n"] == 3
    assert len(prov._test_sleeps) == 2  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_fatal_4xx_not_retried(status: int) -> None:
    """A 401/403/400/422 surfaces immediately with no retry."""
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise _AmsHttpError(status, f"http {status}")

    prov = _provider(transport, max_retries=3)
    with pytest.raises(_AmsHttpError) as excinfo:
        prov._request("POST", "/v1/long-term-memory/", {})
    assert excinfo.value.status == status
    assert calls["n"] == 1, f"status {status} should not retry"
    assert len(prov._test_sleeps) == 0  # type: ignore[attr-defined]
    # A fatal failure must not feed the transient breaker streak.
    assert prov._breaker.consecutive == 0


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504, None])
def test_transient_statuses_exhaust_retries(status: int | None) -> None:
    """Transient statuses (and no-status) retry up to the bound, then raise."""
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise _AmsHttpError(status, f"transient {status}")

    prov = _provider(transport, max_retries=2, breaker_threshold=100)
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert calls["n"] == 3  # 1 + 2 retries


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_breaker_opens_after_threshold_then_fails_fast() -> None:
    """After N consecutive failures the breaker fails fast without the transport."""
    clock = _FakeClock()
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise _AmsHttpError(503, "down")

    # threshold=3, no retries so each _request is one transport attempt.
    prov = _provider(
        transport, max_retries=0, breaker_threshold=3, breaker_cooldown_s=30.0, clock=clock
    )
    for _ in range(3):
        with pytest.raises(_AmsHttpError):
            prov._request("GET", "/v1/health", None)
    assert calls["n"] == 3
    assert prov._breaker.opened_at is not None

    # Breaker now open: this call must fail fast, transport untouched.
    before = calls["n"]
    with pytest.raises(_AmsHttpError) as excinfo:
        prov._request("GET", "/v1/health", None)
    assert "circuit breaker open" in str(excinfo.value)
    assert calls["n"] == before  # transport not called while open


def test_breaker_half_opens_after_cooldown() -> None:
    """After the cooldown a single trial is allowed; success closes it."""
    clock = _FakeClock()
    outcomes = {"fail": True}

    def transport(method, url, payload, headers, timeout):
        if outcomes["fail"]:
            raise _AmsHttpError(503, "down")
        return {"ok": True}

    prov = _provider(
        transport, max_retries=0, breaker_threshold=2, breaker_cooldown_s=30.0, clock=clock
    )
    # Trip the breaker.
    for _ in range(2):
        with pytest.raises(_AmsHttpError):
            prov._request("GET", "/v1/health", None)
    assert prov._breaker.opened_at is not None

    # Still inside cooldown: fail fast.
    clock.advance(10.0)
    with pytest.raises(_AmsHttpError) as excinfo:
        prov._request("GET", "/v1/health", None)
    assert "circuit breaker open" in str(excinfo.value)

    # Cooldown elapsed -> half-open. Let the trial succeed; breaker closes.
    clock.advance(25.0)
    outcomes["fail"] = False
    result = prov._request("GET", "/v1/health", None)
    assert result == {"ok": True}
    assert prov._breaker.opened_at is None
    assert prov._breaker.consecutive == 0


def test_breaker_half_open_failure_reopens() -> None:
    """A failed half-open trial re-opens the breaker with a fresh cooldown."""
    clock = _FakeClock()

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(
        transport, max_retries=0, breaker_threshold=2, breaker_cooldown_s=30.0, clock=clock
    )
    for _ in range(2):
        with pytest.raises(_AmsHttpError):
            prov._request("GET", "/v1/health", None)
    first_open = prov._breaker.opened_at
    assert first_open is not None

    # Half-open trial fails -> re-open with a fresh stamp.
    clock.advance(31.0)
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert prov._breaker.opened_at is not None
    assert prov._breaker.opened_at > first_open


def test_breaker_counts_one_failure_per_logical_request() -> None:
    """A failing call with retries trips the breaker ONCE, not once per attempt.

    Otherwise a single flaky call could open the breaker by itself via its retry
    budget (3 attempts -> 3 failures with the default threshold of 5).
    """

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=2, breaker_threshold=2)
    # One logical request makes 3 transport attempts but records ONE failure.
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert prov._breaker.consecutive == 1
    assert prov._breaker.opened_at is None  # threshold of 2 not reached by one call
    # A second logical request reaches the threshold and opens the breaker.
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert prov._breaker.consecutive == 2
    assert prov._breaker.opened_at is not None


def test_half_open_trial_makes_a_single_attempt() -> None:
    """A half-open probe gets exactly one attempt, even with retries configured,
    so a failed probe does not keep hammering AMS during the fresh cooldown."""
    calls = {"n": 0}

    def transport(method, url, payload, headers, timeout):
        calls["n"] += 1
        raise _AmsHttpError(503, "down")

    clock = _FakeClock()
    prov = _provider(
        transport, max_retries=3, breaker_threshold=1, breaker_cooldown_s=10.0, clock=clock
    )
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert prov._breaker.opened_at is not None
    before = calls["n"]
    # Cooldown elapsed -> half-open. The single trial must not retry.
    clock.advance(11.0)
    with pytest.raises(_AmsHttpError):
        prov._request("GET", "/v1/health", None)
    assert calls["n"] - before == 1


def test_breaker_allow_grants_a_single_half_open_trial_atomically() -> None:
    """allow() returns the decision (closed/half_open/open) in one locked step,
    and hands out exactly one half-open trial so the retry budget cannot widen
    via a concurrent state change between two separate checks."""
    clock = _FakeClock()
    b = _BreakerState(threshold=1, cooldown_s=10.0, clock=clock)
    assert b.allow() == "closed"
    b.record_failure()  # threshold of 1 -> opens
    assert b.allow() == "open"  # still cooling down
    clock.advance(11.0)
    assert b.allow() == "half_open"  # the single trial is granted here...
    assert b.allow() == "open"  # ...and a concurrent caller is blocked
    # A successful trial closes the breaker; calls flow again.
    b.record_success()
    assert b.allow() == "closed"


def test_list_lessons_does_not_scope_to_user() -> None:
    """ams-reset enumerates the whole namespace: the user_id filter must not be
    sent, so a reset clears every user's lessons, not just the configured one."""
    seen: dict[str, Any] = {}

    def transport(method, url, payload, headers, timeout):
        seen["payload"] = payload
        return {"memories": []}

    prov = RedisAgentMemoryProvider(transport=transport, user_id="someone", namespace="alfred")
    prov.list_lessons(limit=50)
    assert "user_id" not in seen["payload"]
    assert seen["payload"]["namespace"] == {"eq": "alfred"}


# ---------------------------------------------------------------------------
# Preserved method-level semantics
# ---------------------------------------------------------------------------


def test_recall_returns_empty_on_exhausted() -> None:
    """recall() still swallows to [] after retries are exhausted."""

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=1, breaker_threshold=100)
    assert prov.recall(query="anything") == []


def test_recall_returns_empty_when_breaker_open() -> None:
    """recall() swallows the fail-fast breaker error to [] as well."""

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=0, breaker_threshold=1)
    assert prov.recall(query="x") == []  # trips breaker
    assert prov._breaker.opened_at is not None
    assert prov.recall(query="x") == []  # fail-fast, still []


def test_reflect_raises_on_exhausted() -> None:
    """reflect() still raises NotImplementedError after retries exhausted."""

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=1, breaker_threshold=100)
    with pytest.raises(NotImplementedError):
        prov.reflect(codename="robin", repo="acme/api", body="a lesson")


def test_reflect_raises_when_breaker_open() -> None:
    """reflect() raises when the breaker is open (promote path stays pending)."""

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=0, breaker_threshold=1)
    with pytest.raises(NotImplementedError):
        prov.reflect(codename="robin", repo="acme/api", body="x")  # trips breaker
    with pytest.raises(NotImplementedError):
        prov.reflect(codename="robin", repo="acme/api", body="y")  # fail-fast


# ---------------------------------------------------------------------------
# ams-reset helpers
# ---------------------------------------------------------------------------


def test_list_lessons_and_forget_lesson() -> None:
    """list_lessons enumerates the namespace; forget_lesson issues a DELETE."""
    deleted: list[str] = []

    def transport(method, url, payload, headers, timeout):
        if method == "POST" and url.endswith("/v1/long-term-memory/search"):
            return {
                "memories": [
                    {"id": "m1", "text": "lesson one", "topics": ["alfred"]},
                    {"id": "m2", "text": "lesson two", "topics": ["alfred"]},
                ]
            }
        if method == "DELETE":
            deleted.append(url)
            return {}
        raise AssertionError(f"unexpected {method} {url}")

    prov = _provider(transport)
    lessons = prov.list_lessons(limit=500)
    assert {lesson.id for lesson in lessons} == {"m1", "m2"}
    for lesson in lessons:
        assert prov.forget_lesson(lesson.id) is True
    assert len(deleted) == 2
    assert all("memory_ids=" in url for url in deleted)


def test_forget_lesson_swallows_transport_error() -> None:
    """forget_lesson returns False (not raise) so a sweep can tally failures."""

    def transport(method, url, payload, headers, timeout):
        raise _AmsHttpError(503, "down")

    prov = _provider(transport, max_retries=0, breaker_threshold=100)
    assert prov.forget_lesson("m1") is False


def test_empty_memory_id_is_rejected() -> None:
    def transport(method, url, payload, headers, timeout):
        raise AssertionError("transport should not be called for empty id")

    prov = _provider(transport)
    assert prov.forget_lesson("  ") is False
