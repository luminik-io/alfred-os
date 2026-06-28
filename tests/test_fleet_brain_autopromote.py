"""Tests for FleetBrain LLM-gated auto-promotion (auto_promote_candidates).

Auto-promotion is ON by default, gates on structural rails (evidence, no
body-conflict, confidence >= threshold), and layers an LLM judge that can only
make the gate STRICTER. ``ALFRED_AUTO_PROMOTE=0`` opts out, and
``ALFRED_AUTO_PROMOTE_KILL=1`` halts it immediately. The judge seam is injected,
so these never spawn a real model. All state lives under tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable from the repo root.
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "lib"))

from datetime import UTC, datetime  # noqa: E402

from fleet_brain import FleetBrain, Lesson, new_id  # noqa: E402

ARM = {"ALFRED_AUTO_PROMOTE": "1"}
ARM_NO_JUDGE = {"ALFRED_AUTO_PROMOTE": "1", "ALFRED_AUTO_PROMOTE_LLM_JUDGE": "0"}
OPT_OUT = {"ALFRED_AUTO_PROMOTE": "0"}


class _FakeAMS:
    """Stub Redis AMS provider; the promoted lesson is written here, not to the
    local SQLite store. Records reflects/forgets so tests can assert the write
    target and the deterministic memory id."""

    name = "redis"

    def __init__(self) -> None:
        self.reflected: list[dict] = []
        self.forgotten: list[str] = []
        self.fail = False

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags=None,  # type: ignore[no-untyped-def]
        severity="info",  # type: ignore[no-untyped-def]
        firing_id=None,  # type: ignore[no-untyped-def]
        created_at=None,  # type: ignore[no-untyped-def]
        memory_id=None,  # type: ignore[no-untyped-def]
    ) -> Lesson:
        if self.fail:
            raise RuntimeError("AMS unreachable")
        self.reflected.append({"memory_id": memory_id, "body": body})
        return Lesson(
            id=memory_id or new_id(),
            codename=codename,
            repo=repo,
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            created_at=created_at or datetime.now(UTC),
            firing_id=firing_id,
            severity=severity,
        )

    def forget_lesson(self, lesson_id: str) -> bool:
        self.forgotten.append(lesson_id)
        return True


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
    fb = FleetBrain(db_path=tmp_path / "brain.db")
    # The auto-promoter writes promoted lessons to Redis AMS via
    # ``_lesson_provider``. Swap in an in-memory stub so the pipeline runs end
    # to end (capture -> judge -> auto-promote -> AMS write) without a server.
    ams = _FakeAMS()
    fb.ams = ams  # type: ignore[attr-defined]
    fb._lesson_provider = lambda env=None: ams  # type: ignore[method-assign]
    return fb


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


def test_auto_promote_defaults_on_and_judges_candidates(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env={}, judge=lambda _p: _verdict(0.97))
    assert summary["enabled"] is True
    assert c.id in summary["promoted"]
    assert _status(brain, c.id) == "validated"


def test_explicit_opt_out_is_a_true_noop(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env=OPT_OUT)
    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0  # the queue is not even read
    assert _status(brain, c.id) == "candidate"


def test_direct_auto_promote_reads_runtime_env_opt_out(
    brain: FleetBrain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_home = tmp_path / "shell-home"
    runtime = tmp_path / "runtime"
    shell_home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(shell_home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_KILL", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_LLM_JUDGE", raising=False)

    c = _candidate(brain, "runtime env opted out", confidence=0.99)
    summary = brain.auto_promote_candidates(judge=lambda _p: _verdict(0.97))

    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


def test_direct_auto_promote_strips_persisted_alfredrc_pointer_comment(
    brain: FleetBrain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_home = tmp_path / "shell-home"
    runtime = tmp_path / "runtime"
    custom_rc = tmp_path / "custom.alfredrc"
    shell_home.mkdir()
    runtime.mkdir()
    (shell_home / ".alfredrc").write_text(
        f"ALFREDRC={custom_rc} # scheduler rc\n",
        encoding="utf-8",
    )
    custom_rc.write_text(
        f"ALFRED_HOME={runtime}\nALFRED_AUTO_PROMOTE=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(shell_home))
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_KILL", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_LLM_JUDGE", raising=False)

    c = _candidate(brain, "pointer comment opted out", confidence=0.99)
    summary = brain.auto_promote_candidates(judge=lambda _p: _verdict(0.97))

    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


def test_direct_auto_promote_pointed_rc_overrides_stale_parent_defaults(
    brain: FleetBrain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_home = tmp_path / "shell-home"
    stale_runtime = tmp_path / "stale-runtime"
    runtime = tmp_path / "runtime"
    custom_rc = tmp_path / "custom.alfredrc"
    shell_home.mkdir()
    stale_runtime.mkdir()
    runtime.mkdir()
    (shell_home / ".alfredrc").write_text(
        f"ALFRED_HOME={stale_runtime}\nALFREDRC={custom_rc}\n",
        encoding="utf-8",
    )
    (stale_runtime / ".env").write_text("ALFRED_AUTO_PROMOTE=1\n", encoding="utf-8")
    custom_rc.write_text(
        f"ALFRED_HOME={runtime}\nALFRED_AUTO_PROMOTE=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(shell_home))
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_KILL", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_LLM_JUDGE", raising=False)

    c = _candidate(brain, "custom rc opted out", confidence=0.99)
    summary = brain.auto_promote_candidates(judge=lambda _p: _verdict(0.97))

    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


def test_direct_auto_promote_runtime_judge_stop_overrides_stale_process_env(
    brain: FleetBrain,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_home = tmp_path / "shell-home"
    runtime = tmp_path / "runtime"
    shell_home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_AUTO_PROMOTE_LLM_JUDGE=treu\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(shell_home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE_KILL", raising=False)
    monkeypatch.setenv("ALFRED_AUTO_PROMOTE_LLM_JUDGE", "1")

    c = _candidate(brain, "runtime judge typo fails closed", confidence=0.99)
    summary = brain.auto_promote_candidates(judge=lambda _p: _verdict(0.97))

    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


def test_auto_promote_passes_runtime_env_to_ams_writer(tmp_path, monkeypatch) -> None:
    brain = FleetBrain(db_path=tmp_path / "brain.db")
    c = _candidate(brain, "write promoted lesson to configured runtime AMS", confidence=0.96)
    captured_envs: list[dict[str, str] | None] = []

    class Writer:
        def reflect(self, **kwargs):
            return Lesson(
                id=kwargs["memory_id"],
                codename=kwargs["codename"],
                repo=kwargs["repo"],
                body=kwargs["body"],
                tags=kwargs["tags"],
                severity=kwargs["severity"],
                firing_id=kwargs["firing_id"],
                created_at=datetime.now(UTC),
            )

    def provider(*, env=None):
        captured_envs.append(dict(env) if env is not None else None)
        return Writer()

    monkeypatch.setattr(brain, "_lesson_provider", provider)

    summary = brain.auto_promote_candidates(
        env={
            "ALFRED_AUTO_PROMOTE": "1",
            "ALFRED_AUTO_PROMOTE_LLM_JUDGE": "0",
            "ALFRED_REDIS_MEMORY_URL": "http://runtime-ams.local",
            "ALFRED_AMS_TOKEN": "runtime-secret",
        }
    )

    assert c.id in summary["promoted"]
    assert captured_envs[0] is not None
    assert captured_envs[0]["ALFRED_REDIS_MEMORY_URL"] == "http://runtime-ams.local"
    assert captured_envs[0]["ALFRED_AMS_TOKEN"] == "runtime-secret"


def test_unrecognized_auto_promote_value_fails_closed(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE": "fales"},
        judge=lambda _p: _verdict(0.97),
    )
    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


def test_kill_switch_overrides_arm(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env={"ALFRED_AUTO_PROMOTE_KILL": "1"})
    assert summary["enabled"] is False
    assert _status(brain, c.id) == "candidate"


@pytest.mark.parametrize("value", ["fales", "1#halt"])
def test_malformed_kill_switch_value_fails_closed(brain: FleetBrain, value: str) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE_KILL": value},
        judge=lambda _p: _verdict(0.97),
    )
    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


@pytest.mark.parametrize("value", ["0", "off", "disabled", "0 # keep running"])
def test_falsy_kill_switch_values_do_not_block_promotion(brain: FleetBrain, value: str) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE_KILL": value},
        judge=lambda _p: _verdict(0.97),
    )
    assert summary["enabled"] is True
    assert c.id in summary["promoted"]
    assert _status(brain, c.id) == "validated"


def test_inline_commented_stop_controls_fail_closed(brain: FleetBrain) -> None:
    c1 = _candidate(brain, "a strong durable lesson", confidence=0.99)
    killed = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE_KILL": "1 # halt"},
        judge=lambda _p: _verdict(0.97),
    )
    assert killed["enabled"] is False
    assert _status(brain, c1.id) == "candidate"

    c2 = _candidate(brain, "another strong durable lesson", confidence=0.99)
    opted_out = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE": "0 # operator opt-out"},
        judge=lambda _p: _verdict(0.97),
    )
    assert opted_out["enabled"] is False
    assert _status(brain, c2.id) == "candidate"


def test_malformed_leading_hash_auto_promote_value_fails_closed(brain: FleetBrain) -> None:
    c = _candidate(brain, "a durable lesson with malformed config", confidence=0.99)

    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE": "#abc"},
        judge=lambda _p: _verdict(0.97),
    )

    assert summary["enabled"] is False
    assert _status(brain, c.id) == "candidate"


def test_malformed_judge_flag_fails_closed(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)

    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE_LLM_JUDGE": "treu"},
        judge=lambda _p: _verdict(0.97),
    )

    assert summary["enabled"] is False
    assert summary["promoted"] == []
    assert summary["considered"] == 0
    assert _status(brain, c.id) == "candidate"


@pytest.mark.parametrize("value", ["enabled", "1 # keep judge on"])
def test_truthy_judge_flag_tokens_still_use_judge(brain: FleetBrain, value: str) -> None:
    c = _candidate(brain, "judge-gated durable lesson", confidence=0.99)

    summary = brain.auto_promote_candidates(
        env={"ALFRED_AUTO_PROMOTE_LLM_JUDGE": value},
        judge=lambda _p: _verdict(0.40),
    )

    assert summary["enabled"] is True
    assert summary["judge_enabled"] is True
    assert summary["judge_calls"] == 1
    assert c.id not in summary["promoted"]
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
    # The promoted lesson was written to Redis AMS (not the local store) under
    # the deterministic candidate-derived memory id.
    assert brain.ams.reflected[0]["memory_id"] == f"lesson:memory_candidate:{c.id}"  # type: ignore[attr-defined]
    assert brain.recall(codename="lucius", repo="acme/api") == []


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

    # On a re-run the held candidate is skipped before the judge is called, so
    # a queue of held behavior-changing rows cannot burn the judge budget.
    again = brain.auto_promote_candidates(
        env=ARM, threshold=0.9, judge=lambda _p: _verdict(0.99, changes=True)
    )
    assert again["promoted"] == []
    assert again["skipped_flagged"] == 1
    assert again["judge_calls"] == 0
    assert brain.store.get_memory_candidate(c.id).status == "candidate"


def test_judge_duplicate_is_held(brain: FleetBrain) -> None:
    c = _candidate(brain, "a near copy of an existing lesson", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(is_duplicate=True))
    assert summary["skipped_duplicate"] == 1
    assert _status(brain, c.id) == "candidate"


def test_default_on_judge_failure_is_fail_soft(brain: FleetBrain) -> None:
    c = _candidate(brain, "a strong durable lesson", confidence=0.99)
    summary = brain.auto_promote_candidates(env={}, judge=lambda _p: None)
    assert summary["promoted"] == []
    assert summary["judge_enabled"] is True
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


# --- AMS write target (the #411 cutover) -----------------------------------


def test_judge_gate_runs_before_the_ams_write(brain: FleetBrain) -> None:
    # The judge gate stays IN FRONT of promotion: a rejected (duplicate) verdict
    # must mean NO AMS write at all. Proves capture -> judge -> (no) promote.
    c = _candidate(brain, "a near-duplicate lesson", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(is_duplicate=True))
    assert summary["judge_calls"] == 1
    assert summary["skipped_duplicate"] == 1
    assert summary["promoted"] == []
    # Judge held it -> nothing was written to AMS.
    assert brain.ams.reflected == []  # type: ignore[attr-defined]
    assert _status(brain, c.id) == "candidate"


def test_safe_verdict_then_writes_to_ams(brain: FleetBrain) -> None:
    # A safe verdict that clears the bar promotes AND writes to AMS. This is the
    # full capture -> judge -> auto-promote -> AMS-write pipeline, end to end.
    c = _candidate(brain, "tests live next to the code they cover", confidence=0.95)
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.97))
    assert c.id in summary["promoted"]
    assert len(brain.ams.reflected) == 1  # type: ignore[attr-defined]
    assert brain.ams.reflected[0]["memory_id"] == f"lesson:memory_candidate:{c.id}"  # type: ignore[attr-defined]


def test_ams_write_failure_leaves_candidate_pending(brain: FleetBrain) -> None:
    # No silent loss, no local fallback: an unreachable AMS leaves the judged,
    # promotable candidate PENDING and re-promotable, and counts the failure.
    c = _candidate(brain, "a strong durable lesson", confidence=0.95)
    brain.ams.fail = True  # type: ignore[attr-defined]
    summary = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.97))
    assert summary["promoted"] == []
    assert summary["ams_write_errors"] == 1
    assert _status(brain, c.id) == "candidate"
    assert brain.store.get_memory_candidate(c.id).promoted_lesson_id is None  # type: ignore[union-attr]

    # AMS recovers -> the same candidate promotes on the next run with the same
    # deterministic memory id (idempotent upsert).
    brain.ams.fail = False  # type: ignore[attr-defined]
    again = brain.auto_promote_candidates(env=ARM, judge=lambda _p: _verdict(0.97))
    assert c.id in again["promoted"]
    assert brain.ams.reflected[-1]["memory_id"] == f"lesson:memory_candidate:{c.id}"  # type: ignore[attr-defined]
