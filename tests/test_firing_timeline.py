"""Tests for the honest firing-timeline distillation (``server.firing_timeline``).

These lock in the operator-facing contract for the Activity view: a quiet
one-line headline for idle/no-work runs, a clean milestone headline for real
work (opened / reviewed), and a LOUD, honestly-classified error for failures.
The honest-error case is the load-bearing one: an auth failure that the
provider dresses up as a rate limit must read as authentication, never the
downstream rate_limit.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from server.firing_timeline import (  # noqa: E402
    classify_outcome_error,
    derive_timeline,
)


def _ev(event: str, **payload: object) -> dict:
    return {"event": event, "ts": "2026-06-24T10:00:00.000000Z", **payload}


def test_pr_opened_run_is_ok_with_milestone_headline() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("repo_picked", repo="acme-org/api"),
            _ev("worktree_created", branch="bane/fix-123"),
            _ev("llm_invoke_done", engine="claude", turns=12, subtype="success", success=True),
            _ev("pr_opened", url="https://github.com/acme-org/api/pull/1048", repo="acme-org/api"),
            _ev("firing_complete", outcome="pr-opened"),
        ]
    )
    assert timeline.severity == "ok"
    assert timeline.error is None
    assert timeline.headline == "Opened PR #1048"
    kinds = [s.kind for s in timeline.steps]
    assert kinds == ["started", "picked", "worktree", "engine", "pr", "complete"]
    engine_step = next(s for s in timeline.steps if s.kind == "engine")
    assert "12 turns" in engine_step.detail
    assert engine_step.tone == "ok"


def test_review_run_summarises_findings() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("pr_picked", repo="acme-org/api", number=1051, comment_count=3),
            _ev("review_posted", repo="acme-org/api", number=1051, p0_count=1, p1_count=1, turns=8),
            _ev("firing_complete", outcome="review-posted"),
        ]
    )
    assert timeline.severity == "ok"
    assert timeline.headline == "Reviewed PR #1051 · 2 findings"
    review_step = next(s for s in timeline.steps if s.kind == "review")
    assert review_step.detail == "2 findings"


def test_multiple_reviews_sum_findings_without_masking() -> None:
    # A clean second review must never hide blocking findings from the first:
    # the headline summarises both reviews and totals their findings.
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("review_posted", repo="acme-org/api", number=100, p0_count=3, p1_count=0),
            _ev("review_posted", repo="acme-org/api", number=200, p0_count=0, p1_count=0),
            _ev("firing_complete", outcome="review-posted"),
        ]
    )
    assert timeline.severity == "ok"
    assert timeline.headline == "Reviewed 2 PRs · 3 findings"


def test_multiple_pr_opens_are_counted() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("pr_opened", url="https://github.com/acme-org/api/pull/1", repo="acme-org/api"),
            _ev("pr_opened", url="https://github.com/acme-org/api/pull/2", repo="acme-org/api"),
            _ev("firing_complete", outcome="pr-opened"),
        ]
    )
    assert timeline.headline == "Opened 2 PRs"


def test_review_with_no_findings_reads_clean() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("review_posted", repo="acme-org/api", number=1051, p0_count=0, p1_count=0),
            _ev("firing_complete", outcome="review-posted"),
        ]
    )
    assert "no blocking findings" in timeline.headline


def test_idle_run_is_quiet() -> None:
    for outcome in ("noop", "idle-no-pr", "idle-no-comments", "silent-well-covered"):
        timeline = derive_timeline([_ev("firing_started"), _ev("firing_complete", outcome=outcome)])
        assert timeline.severity == "idle", outcome
        assert timeline.error is None, outcome
        assert timeline.headline == "Idle · no work", outcome


def test_auth_failure_reads_as_authentication_not_rate_limit() -> None:
    """The honest-error contract: an auth-triggered fallback must NOT surface as
    the downstream rate_limit text the provider returns."""
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("repo_picked", repo="acme-org/api"),
            # The fallback reason carries misleading downstream provider text.
            _ev(
                "llm_fallback",
                from_engine="claude",
                to_engine="codex",
                reason="API Error: 429 rate_limit_exceeded - too many requests",
            ),
            _ev(
                "llm_invoke_done",
                engine="codex",
                turns=2,
                subtype="error_authentication",
                success=False,
            ),
            _ev("firing_complete", outcome="llm-error_authentication", engine="codex"),
        ]
    )
    assert timeline.severity == "error"
    assert timeline.error == "authentication"
    # Headline must name authentication, never rate limit.
    assert "authentication" in timeline.headline.lower()
    assert "rate" not in timeline.headline.lower()
    # The fallback step explains the honest cause, not the raw reason string.
    fallback_step = next(s for s in timeline.steps if s.kind == "fallback")
    assert "authentication" in fallback_step.detail
    assert "429" not in fallback_step.detail


def test_rate_limit_failure_is_classified() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev(
                "llm_invoke_done",
                engine="claude",
                turns=3,
                subtype="error_rate_limit",
                success=False,
            ),
            _ev("firing_complete", outcome="llm-error_rate_limit"),
        ]
    )
    assert timeline.severity == "error"
    assert timeline.error == "rate_limit"
    assert "rate limit" in timeline.headline.lower()


def test_failure_anchors_to_the_pr_it_was_working_on() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("pr_picked", repo="acme-org/api", number=900),
            _ev(
                "llm_invoke_done",
                engine="claude",
                turns=5,
                subtype="error_overloaded",
                success=False,
            ),
            _ev("firing_complete", outcome="llm-error_overloaded"),
        ]
    )
    assert timeline.error == "overloaded"
    assert "PR #900" in timeline.headline


def test_blocked_outcome_is_an_error() -> None:
    timeline = derive_timeline(
        [
            _ev("firing_started"),
            _ev("firing_complete", outcome="blocked-untrusted-author"),
        ]
    )
    assert timeline.severity == "error"
    assert timeline.error == "failed"


def test_running_firing_has_no_terminal_outcome() -> None:
    timeline = derive_timeline([_ev("firing_started"), _ev("repo_picked", repo="acme-org/api")])
    assert timeline.outcome is None
    assert timeline.headline == "Running"
    assert timeline.severity == "idle"


def test_classify_prefers_outcome_over_invoke_subtype() -> None:
    # Outcome names auth even if a stray invoke subtype says success.
    assert (
        classify_outcome_error("llm-error_authentication", invoke_subtype="success")
        == "authentication"
    )
    # Generic failure-* falls back to the invoke subtype.
    assert classify_outcome_error("failure-unknown", invoke_subtype="error_budget") == "budget"
    # Idle outcomes never report an error.
    assert classify_outcome_error("idle-no-pr") is None


def test_empty_events_degrade_gracefully() -> None:
    timeline = derive_timeline([])
    assert timeline.headline == "No summary captured"
    assert timeline.error is None
    assert timeline.steps == []


def test_garbage_events_are_skipped() -> None:
    timeline = derive_timeline([None, "nonsense", 42, _ev("firing_complete", outcome="noop")])  # type: ignore[list-item]
    assert timeline.severity == "idle"
    assert timeline.headline == "Idle · no work"
