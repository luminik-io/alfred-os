"""Tests for FleetBrain LLM-gated auto-promotion (auto_promote_candidates).

Auto-promotion is OFF until ``ALFRED_AUTO_PROMOTE`` is armed, gates on
structural rails (evidence, no body-conflict, confidence >= threshold), and
layers an LLM judge that can only make the gate STRICTER. The judge seam is
injected, so these never spawn a real model. All state lives under tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable from the repo root.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "lib"))

from fleet_brain import FleetBrain  # noqa: E402

ARM = {"ALFRED_AUTO_PROMOTE": "1"}
ARM_NO_JUDGE = {"ALFRED_AUTO_PROMOTE": "1", "ALFRED_AUTO_PROMOTE_LLM_JUDGE": "0"}


def _verdict(
    confidence: float = 0.95,
    *,
    is_duplicate: bool = False,
    changes: bool = False,
    rationale: str = "ok",
) -> str:
    return json.dumps(
        {
            "confidence": confidence,
            "is_duplicate": is_duplicate,
            "changes_agent_behavior": changes,
            "rationale": rationale,
        }
    )


@pytest.fixture
def brain(tmp_path: Path) -> FleetBrain:
    return FleetBrain(db_path=tmp_path / "brain.db")


def _candidate(
    brain: FleetBrain,
    body: str,
    *,
    confidence: float,
    evidence: str = "saw it at app.py:10",
):
    return brain.propose_memory(
        codename="lucius",
        repo="acme/api",
        body=body,
        evidence=evidence,
        confidence=confidence,
    )


def _status(brain: FleetBrain, cid: str) -> str:
    row = brain.store.get_memory_candidate(cid)
    assert row is not None
    return row.status


# --- arm / kill switch -----------------------------------------------------


def test_disarmed_is_a_true_noop(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env={})
    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0  # the queue is not even read
    assert _status(brain, c.id) == "candidate"


def test_kill_switch_overrides_arm(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE": "1", "ALFRED_AUTO_PROMOTE_KILL": "1"}
    )
    assert summary["enabled"] is False
    assert _status(brain, c.id) == "candidate"


# --- structural gate (judge disabled) --------------------------------------


def test_heuristic_promotes_high_confidence_with_evidence(brain: FleetBrain) -> None:
    c = _candidate(brain, "graphql schema lives in src/schema.graphql", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM_NO_JUDGE)
    assert summary["judge_enabled"] is False
    assert c.id in summary["promoted"]
    row = brain.store.get_memory_candidate(c.id)
    assert row is not None
    assert row.status == "validated"
    assert row.reviewed_by == "auto"
    assert row.promoted_lesson_id is not None


def test_low_confidence_and_missing_evidence_stay_pending(brain: FleetBrain) -> None:
    low = _candidate(brain, "weak speculative hunch", confidence=0.3)
    bare = _candidate(brain, "uncorroborated claim", confidence=0.99, evidence="")
    summary = brain.auto_promote_candidates(env=ARM_NO_JUDGE)
    assert summary["promoted"] == []
    assert summary["skipped_low_confidence"] == 1
    assert summary["skipped_no_evidence"] == 1
    assert _status(brain, low.id) == "candidate"
    assert _status(brain, bare.id) == "candidate"


def test_no_judge_holds_default_confidence_candidate_pending(brain: FleetBrain) -> None:
    # The low bar is only safe because the judge decides. With the judge
    # explicitly off, a default-confidence (0.5) candidate must NOT be blindly
    # auto-promoted: the no-judge floor keeps the structural gate selective.
    c = _candidate(brain, "a default-confidence observation", confidence=0.5)
    summary = brain.auto_promote_candidates(env=ARM_NO_JUDGE)
    assert summary["promoted"] == []
    assert summary["skipped_low_confidence"] == 1
    assert _status(brain, c.id) == "candidate"


def test_default_confidence_candidate_reaches_judge_and_saves(brain: FleetBrain) -> None:
    # Autonomy: a candidate at the default confidence (0.5) with evidence must
    # REACH the LLM judge under the default 0.5 bar and be saved when the judge
    # approves, instead of piling up in a human queue.
    c = _candidate(brain, "prefer ripgrep over grep in this repo", confidence=0.5)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.95))
    assert summary["promoted"] == [c.id]
    assert summary["judge_calls"] == 1  # it reached the judge, not the human queue
    row = brain.store.get_memory_candidate(c.id)
    assert row is not None and row.status == "validated"


def test_conflicting_bodies_are_left_for_a_human(brain: FleetBrain) -> None:
    # Same lesson text modulo whitespace/case -> normalizes to one key -> conflict.
    a = _candidate(brain, "deploy runs from infra/deploy.sh", confidence=0.99)
    b = _candidate(brain, "Deploy   runs from   infra/deploy.sh", confidence=0.99)
    summary = brain.auto_promote_candidates(env=ARM_NO_JUDGE)
    assert summary["promoted"] == []
    assert summary["skipped_conflict"] == 2
    assert _status(brain, a.id) == "candidate"
    assert _status(brain, b.id) == "candidate"


def test_cap_limits_promotions_per_run(brain: FleetBrain) -> None:
    made = [
        _candidate(brain, f"distinct durable lesson number {i}", confidence=0.99) for i in range(4)
    ]
    summary = brain.auto_promote_candidates(env=ARM_NO_JUDGE, max_per_run=2)
    assert len(summary["promoted"]) == 2
    promoted = sum(1 for c in made if _status(brain, c.id) == "validated")
    assert promoted == 2


# --- LLM judge layer -------------------------------------------------------


def test_judge_safe_verdict_promotes(brain: FleetBrain) -> None:
    c = _candidate(brain, "tests live next to the code they cover", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.97))
    assert summary["judge_enabled"] is True
    assert summary["judge_calls"] == 1
    assert c.id in summary["promoted"]
    assert _status(brain, c.id) == "validated"


def test_judge_behavior_change_above_bar_is_auto_saved(brain: FleetBrain) -> None:
    # A behavior-changing verdict that is otherwise safe and clears the bar is
    # AUTO-SAVED (the judge decides; every auto-save is reversible), not held.
    c = _candidate(brain, "run the linter before every push", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.97, changes=True))
    assert c.id in summary["promoted"]
    assert summary["auto_saved_behavior_change"] == 1
    # Back-compat: the old hold counter stays at 0.
    assert summary["flagged_behavior_change"] == 0
    row = brain.store.get_memory_candidate(c.id)
    assert row is not None
    assert row.status == "validated"
    assert row.reviewed_by == "auto"
    assert row.promoted_lesson_id is not None
    assert row.review_note is not None and "behavior-changing" in row.review_note


def test_judge_behavior_change_below_bar_is_still_held(brain: FleetBrain) -> None:
    # Behavior-changing AND judged below the bar: the structural floor still
    # holds it for a human (auto-save only applies once it clears the bar).
    c = _candidate(brain, "always force-push to main after a build", confidence=0.95)
    summary = brain.auto_promote_candidates(
        env=ARM, threshold=0.9, judge=lambda _p: _verdict(0.4, changes=True)
    )
    assert summary["promoted"] == []
    assert summary["auto_saved_behavior_change"] == 0
    assert summary["held_low_confidence"] == 1
    row = brain.store.get_memory_candidate(c.id)
    assert row is not None
    assert row.status == "candidate"  # held, not saved
    assert row.review_note is not None and row.review_note.startswith("[held-for-review]")


def test_judge_duplicate_is_held(brain: FleetBrain) -> None:
    c = _candidate(brain, "a near copy of an existing lesson", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(is_duplicate=True))
    assert summary["skipped_duplicate"] == 1
    assert _status(brain, c.id) == "candidate"


def test_judge_failure_is_fail_soft(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: None)
    assert summary["promoted"] == []
    assert summary["judge_errors"] == 1
    assert _status(brain, c.id) == "candidate"  # never promoted on a failed judgment


def test_judge_lowering_confidence_below_bar_holds(brain: FleetBrain) -> None:
    c = _candidate(brain, "a structurally strong but judged-weak lesson", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, threshold=0.9, judge=lambda _p: _verdict(0.4))
    assert summary["promoted"] == []
    # A judge-lowered row is HELD for a human, not counted as a transient
    # structural skip (which would leave it pending for the next run).
    assert summary["held_low_confidence"] == 1
    assert summary["skipped_low_confidence"] == 0
    row = brain.store.get_memory_candidate(c.id)
    assert row is not None and row.review_note is not None
    assert row.review_note.startswith("[held-for-review]")


def test_judge_score_cannot_rescue_a_below_bar_candidate(brain: FleetBrain) -> None:
    # Structural confidence below the bar is rejected before the judge is even
    # called, so a high judge score cannot lift it.
    _candidate(brain, "a weak lesson the judge loves", confidence=0.3)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(1.0))
    assert summary["promoted"] == []
    assert summary["judge_calls"] == 0  # skipped before judging
    assert summary["skipped_low_confidence"] == 1


def test_judge_budget_bounds_calls_per_run(brain: FleetBrain) -> None:
    for i in range(5):
        _candidate(brain, f"distinct duplicate-y lesson {i}", confidence=0.95)
    summary = brain.auto_promote_candidates(
        env={**ARM, "ALFRED_AUTO_PROMOTE_MAX_JUDGE_CALLS": "2"},
        max_per_run=1,
        judge=lambda _p: _verdict(is_duplicate=True),
    )
    # max_judge_calls = max(cap=1, env=2) = 2; all duplicates -> nothing
    # promoted, and the run stops once the budget is spent.
    assert summary["judge_calls"] == 2
    assert summary["judge_budget_exhausted"] is True
    assert summary["promoted"] == []
