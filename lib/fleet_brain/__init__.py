"""Alfred's fleet-brain: a local procedural-learning memory layer.

``fleet_brain`` records what each agent firing learned about a repo
or codename. It keeps reviewable candidates, firing history, file
touches, GitHub cache rows, and local evidence under ``$ALFRED_HOME``.
Redis Agent Memory is the default recalled-lesson layer for new
installs; FleetBrain is the local ledger behind that review loop.

Quick start::

    from fleet_brain import FleetBrain

    brain = FleetBrain()
    brain.reflect(
        codename="lucius",
        repo="your-org/api",
        body="GraphQL schema lives in src/schema.graphql; tests live next to it.",
        tags=["graphql", "layout"],
    )
    lessons = brain.recall(codename="lucius", repo="your-org/api")
    for L in lessons:
        print(L.body)

Public surface:

* :class:`FleetBrain`: the main API: ``recall``, ``reflect``,
  ``firing_log``, ``record_file_touch``, ``note_repo``, ``forget``,
  ``export``.
* :class:`fleet_brain.store.Lesson`, :class:`FiringLog`,
  :class:`FileTouch`, :class:`RepoNote`: entity dataclasses,
  re-exported here.
* :class:`fleet_brain.store.Store`: the Protocol the public API
  depends on. The default local ledger implementation is
  :class:`SQLiteStore`.

Privacy: the FleetBrain ledger is a SQLite file in your
``$ALFRED_HOME``. It never leaves your machine. The only outbound
surface is prompt context sent to Claude Code or Codex on your
existing CLI auth, plus anonymous usage totals if telemetry is left
on. No raw prompts, transcripts, or candidate text are sent by
telemetry.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import (
    BundleItem,
    FailureEvent,
    FileChangeType,
    FileTouch,
    FiringLog,
    FiringStatus,
    GitHubItem,
    GitHubItemKind,
    GitHubItemState,
    Lesson,
    MemoryCandidate,
    MemoryCandidateStatus,
    RepoNote,
    Severity,
    SQLiteStore,
    Store,
    WorkerHeartbeat,
    WorkerStatus,
    default_db_path,
    new_id,
)

__all__ = [
    "BundleItem",
    "FailureEvent",
    "FileChangeType",
    "FileTouch",
    "FiringLog",
    "FiringStatus",
    "FleetBrain",
    "GitHubItem",
    "GitHubItemKind",
    "GitHubItemState",
    "Lesson",
    "MemoryCandidate",
    "MemoryCandidateStatus",
    "RepoNote",
    "SQLiteStore",
    "Severity",
    "Store",
    "WorkerHeartbeat",
    "WorkerStatus",
    "default_db_path",
    "new_id",
]


_LOG = logging.getLogger(__name__)

# Cap recall output so a runaway codename can't blow up a prompt.
_RECALL_DEFAULT = 8
_RECALL_MAX = 50
_NON_ACTIONABLE_FAILURE_SUBTYPES = {
    "already_implemented",
    "already-implemented",
    "daily-cap",
    "dedup-skip",
    "dedup_skip",
    "fixes-landed",
    "green",
    "idle-no-candidates",
    "idle-no-comments",
    "idle-no-pr",
    "noop",
    "ok",
    "pr-opened",
    "review-cap",
    "review-posted",
    "silent-no-work",
    "silent_no_work",
    "success",
    "test-ok",
    "test_ok",
    "triage-cap",
    "triaged",
}

# Auto-promotion defaults. Every one is env-tunable so a deployment can tune
# the gate without a code change, and all of it is OFF until ALFRED_AUTO_PROMOTE
# is explicitly set (see ``FleetBrain.auto_promote_enabled``).
# The threshold is a LIGHT pre-filter, not the decision: any evidenced
# candidate (candidates default to confidence 0.5) must reach the LLM judge,
# which makes the real save/skip call. Memory has to capture AND save
# autonomously via the model; a high bar that dumps observed lessons to a
# human queue just piles up and never gets reviewed.
AUTO_PROMOTE_DEFAULT_THRESHOLD = 0.5
AUTO_PROMOTE_DEFAULT_MAX_PER_RUN = 5
AUTO_PROMOTE_DEFAULT_MAX_JUDGE_CALLS = 25
# When the LLM judge is explicitly disabled, the structural confidence is the
# ONLY gate, so the low judge-era bar would auto-promote every evidenced
# default-confidence candidate with no review. Hold a conservative floor in
# that case (env-tunable) so heuristic-only promotion stays selective.
AUTO_PROMOTE_NO_JUDGE_THRESHOLD = 0.9

# A candidate the auto-promoter has set aside for a human keeps status
# ``candidate`` (so it stays in the review queue and the dedup index) but its
# review_note is stamped with this marker. Subsequent runs see the marker and
# never re-judge the row, so a held candidate cannot starve the per-run judge
# budget or re-post the same alert every run.
_AUTO_HELD_MARKER = "[held-for-review]"


def _env_flag_on(name: str, env: Mapping[str, str] | None = None) -> bool:
    """True only for an explicit truthy token (1/true/yes/on)."""
    src = env if env is not None else os.environ
    return str(src.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, env: Mapping[str, str] | None = None) -> float:
    """Read a float from the environment, falling back on missing/bad input."""
    src = env if env is not None else os.environ
    raw = src.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _auto_dedup_key(body: str) -> str:
    """Normalize a candidate body to a conflict key.

    The OSS ledger has no precomputed ``dedup_hash`` column, so derive a stable
    key from the body: lowercased with collapsed whitespace. Two pending
    candidates that normalize to the same key are treated as a conflict (two
    unreviewed versions of one lesson) and both are left for a human."""
    return re.sub(r"\s+", " ", (body or "").strip().lower())


class FleetBrain:
    """Local procedural-memory layer for the Alfred fleet.

    Operates on a SQLite file by default; tests can inject a custom
    :class:`Store` through the constructor.

    Method names map to the operator-facing verbs:

    * :meth:`reflect`: file a lesson the firing learned.
    * :meth:`recall`: pull lessons relevant to the next firing.
    * :meth:`firing_log`: record one firing's audit row.
    * :meth:`record_file_touch`: record a file changed by an agent.
    * :meth:`propose_memory`: stage a lesson candidate for review.
    * :meth:`record_failure`: normalize non-success outcomes for later diagnosis.
    * :meth:`upsert_github_item`: cache GitHub issue/PR state from a poller.
    * :meth:`upsert_worker_heartbeat`: record worker liveness.
    * :meth:`note_repo`: upsert a free-text repo summary.
    * :meth:`forget`: remove a lesson by id.
    * :meth:`export`: JSON-serializable snapshot for backup or
      cross-host export (the operator must do the transfer; the
      brain never phones home).
    """

    def __init__(self, store: Store | None = None, *, db_path: Path | str | None = None) -> None:
        if store is not None:
            self.store: Store = store
        else:
            resolved = Path(db_path) if db_path is not None else default_db_path()
            self.store = SQLiteStore(db_path=resolved)
        self.store.ensure_schema()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FleetBrain:
        """Build a brain from the public environment contract."""
        if env is None:
            return cls()
        explicit = env.get("ALFRED_FLEET_BRAIN_DB", "").strip()
        if explicit:
            return cls(db_path=Path(explicit).expanduser())
        alfred_home = env.get("ALFRED_HOME", "").strip()
        if alfred_home:
            return cls(db_path=Path(alfred_home).expanduser() / "fleet-brain.db")
        return cls(db_path=Path.home() / ".alfred" / "fleet-brain.db")

    # ----- write paths --------------------------------------------------

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        firing_id: str | None = None,
        severity: Severity = "info",
        lesson_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        """File a lesson the firing learned. Returns the persisted row.

        ``severity`` follows the same taxonomy as the fleet's Slack
        severity routing: ``info`` (recall-only context), ``warning``
        (worth bubbling into a future prompt), ``blocker`` (the next
        firing must read this before doing anything).
        """
        if not codename or not repo or not body:
            raise ValueError("reflect: codename, repo, and body are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"reflect: unknown severity {severity!r}")
        lesson = Lesson(
            id=lesson_id or new_id(),
            codename=codename,
            repo=repo,
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            created_at=created_at or datetime.now(UTC),
            firing_id=firing_id,
            severity=severity,
        )
        _LOG.debug("reflect: codename=%s repo=%s tags=%s", codename, repo, lesson.tags)
        return self.store.insert_lesson(lesson)

    def firing_log(
        self,
        *,
        firing_id: str,
        codename: str,
        status: FiringStatus,
        summary: str = "",
        repo: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        cost_cents: int = 0,
        pr_url: str | None = None,
        sentinel: str | None = None,
    ) -> FiringLog:
        """Persist one firing's audit row. Upserts on ``firing_id``."""
        if not firing_id or not codename:
            raise ValueError("firing_log: firing_id and codename are required")
        if status not in ("ok", "blocked", "partial", "silent"):
            raise ValueError(f"firing_log: unknown status {status!r}")
        now = datetime.now(UTC)
        log = FiringLog(
            firing_id=firing_id,
            codename=codename,
            repo=repo,
            status=status,
            summary=summary or "",
            started_at=started_at or now,
            finished_at=finished_at or now,
            cost_cents=int(cost_cents),
            pr_url=pr_url,
            sentinel=sentinel,
        )
        return self.store.insert_firing_log(log)

    def note_repo(self, *, repo: str, body: str, updated_at: datetime | None = None) -> RepoNote:
        """Upsert the free-text rollup for ``repo``."""
        if not repo or not body:
            raise ValueError("note_repo: repo and body are required")
        note = RepoNote(
            repo=repo,
            body=body.strip(),
            updated_at=updated_at or datetime.now(UTC),
        )
        return self.store.upsert_repo_note(note)

    def record_file_touch(
        self,
        *,
        repo: str,
        path: str,
        codename: str,
        firing_id: str | None = None,
        pr_url: str | None = None,
        change_type: FileChangeType = "modified",
        touch_id: str | None = None,
        touched_at: datetime | None = None,
    ) -> FileTouch:
        """Persist one repo file touched by an agent firing or PR."""
        if not repo or not path or not codename:
            raise ValueError("record_file_touch: repo, path, and codename are required")
        if change_type not in ("added", "modified", "deleted", "renamed", "unknown"):
            raise ValueError(f"record_file_touch: unknown change_type {change_type!r}")
        touch = FileTouch(
            id=touch_id or new_id(),
            repo=repo.strip(),
            path=path.strip(),
            codename=codename.strip(),
            firing_id=firing_id,
            pr_url=pr_url,
            change_type=change_type,
            touched_at=touched_at or datetime.now(UTC),
        )
        return self.store.insert_file_touch(touch)

    def propose_memory(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        source: str = "manual",
        source_firing_id: str | None = None,
        evidence: str = "",
        confidence: float = 0.5,
        candidate_id: str | None = None,
        created_at: datetime | None = None,
    ) -> MemoryCandidate:
        """Stage a lesson candidate without adding it to prompt recall.

        ``reflect`` is intentionally direct for trusted operator input.
        ``propose_memory`` is the safer path for automated summaries,
        imported notes, and speculative engine reflections: the row is
        visible to ``alfred brain candidates`` and can later be promoted
        into a real lesson.
        """
        if not codename or not repo or not body:
            raise ValueError("propose_memory: codename, repo, and body are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"propose_memory: unknown severity {severity!r}")
        if not 0.0 <= float(confidence) <= 1.0:
            raise ValueError("propose_memory: confidence must be between 0 and 1")
        candidate = MemoryCandidate(
            id=candidate_id or new_id(),
            codename=codename.strip(),
            repo=repo.strip(),
            body=body.strip(),
            tags=sorted({t.strip() for t in (tags or []) if t.strip()}),
            severity=severity,
            source=(source or "manual").strip(),
            source_firing_id=source_firing_id,
            evidence=evidence.strip(),
            confidence=float(confidence),
            status="candidate",
            created_at=created_at or datetime.now(UTC),
        )
        return self.store.insert_memory_candidate(candidate)

    def promote_memory_candidate(
        self,
        candidate_id: str,
        *,
        reviewer: str = "operator",
        review_note: str = "",
        reviewed_at: datetime | None = None,
    ) -> Lesson:
        """Promote a candidate into a trusted lesson and mark it validated."""
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"promote_memory_candidate: unknown candidate {candidate_id!r}")
        if candidate.status != "candidate":
            raise ValueError(
                f"promote_memory_candidate: candidate {candidate_id!r} is {candidate.status}"
            )
        lesson = self.reflect(
            codename=candidate.codename,
            repo=candidate.repo,
            body=candidate.body,
            tags=candidate.tags,
            firing_id=candidate.source_firing_id,
            severity=candidate.severity,
        )
        self.store.update_memory_candidate(
            replace(
                candidate,
                status="validated",
                reviewed_at=reviewed_at or datetime.now(UTC),
                reviewed_by=reviewer.strip() or "operator",
                review_note=review_note.strip() or None,
                promoted_lesson_id=lesson.id,
            )
        )
        return lesson

    def reject_memory_candidate(
        self,
        candidate_id: str,
        *,
        reviewer: str = "operator",
        review_note: str = "",
        reviewed_at: datetime | None = None,
    ) -> MemoryCandidate:
        """Reject a candidate so it remains auditable but never enters recall."""
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None:
            raise ValueError(f"reject_memory_candidate: unknown candidate {candidate_id!r}")
        if candidate.status != "candidate":
            raise ValueError(
                f"reject_memory_candidate: candidate {candidate_id!r} is {candidate.status}"
            )
        updated = replace(
            candidate,
            status="rejected",
            reviewed_at=reviewed_at or datetime.now(UTC),
            reviewed_by=reviewer.strip() or "operator",
            review_note=review_note.strip() or None,
        )
        return self.store.update_memory_candidate(updated)

    def auto_promote_enabled(self, env: Mapping[str, str] | None = None) -> bool:
        """True only when armed AND not kill-switched.

        OFF by default: ``auto_promote_candidates`` returns immediately (a true
        no-op, no writes) unless ``ALFRED_AUTO_PROMOTE`` is truthy.
        ``ALFRED_AUTO_PROMOTE_KILL`` wins over the arm flag so a bad batch can
        be stopped without un-setting the deployment config."""
        if _env_flag_on("ALFRED_AUTO_PROMOTE_KILL", env):
            return False
        return _env_flag_on("ALFRED_AUTO_PROMOTE", env)

    def hold_candidate_for_review(
        self, candidate_id: str, *, note: str = ""
    ) -> MemoryCandidate | None:
        """Set a candidate aside for a human without promoting or rejecting it.

        The row keeps status ``candidate`` (so it stays in the review queue and
        the dedup index) but its review_note is stamped with the held marker so
        later auto-promote runs skip it. Returns None if the candidate is gone
        or already left the candidate state."""
        candidate = self.store.get_memory_candidate(candidate_id)
        if candidate is None or candidate.status != "candidate":
            return None
        held = f"{_AUTO_HELD_MARKER} {note}".strip()
        return self.store.update_memory_candidate(
            replace(
                candidate,
                reviewed_at=datetime.now(UTC),
                reviewed_by="auto",
                review_note=held[:500],
            )
        )

    def auto_promote_candidates(
        self,
        *,
        threshold: float | None = None,
        max_per_run: int | None = None,
        reviewer: str = "auto",
        judge: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Promote high-confidence, corroborated, non-conflicting candidates.

        Structural gate (every condition must hold):

          * the master arm flag is on and the kill-switch is off
            (``auto_promote_enabled``); otherwise this is a NO-OP that touches
            nothing and the manual queue is unchanged;
          * the candidate is still ``candidate`` and not already held for a
            human on a prior run;
          * the candidate carries evidence (no bare assertion auto-enters
            recall);
          * it does not conflict with another pending candidate that normalizes
            to the same body (two unreviewed versions => leave both for a
            human);
          * ``confidence >= threshold`` (default 0.5, env-tunable) -- a light
            pre-filter so any evidenced candidate reaches the judge, which is
            the real save/skip decision (autonomous LLM-driven capture+save).

        LLM judge (additive, default ON when armed, gated behind
        ``ALFRED_AUTO_PROMOTE_LLM_JUDGE``): for each candidate that clears the
        structural gate, an LLM is asked whether the lesson is safe to promote.
        The verdict only ever makes the gate STRICTER:

          * ``changes_agent_behavior`` => held for a human, stays a candidate;
          * ``is_duplicate``           => held for a human (dedup owns merging);
          * otherwise the judge confidence is taken as the LOWER of itself and
            the structural confidence (never a rescue);
          * FAIL-SOFT: any LLM error/timeout/parse/empty judgment leaves the
            candidate PENDING. A candidate is NEVER auto-promoted on a failed or
            empty judgment, only on an explicit safe verdict that also clears
            the threshold. With the judge disabled, the heuristic alone gates.

        Promotions are capped per run (``max_per_run``) and recorded with
        ``reviewer="auto"`` so the whole batch stays auditable. ``judge`` is an
        injectable ``str -> str|None`` seam; tests pass a stub so no real model
        process is spawned. Returns a summary dict (always safe to log)."""
        env_src = env if env is not None else os.environ
        summary: dict[str, Any] = {
            "enabled": self.auto_promote_enabled(env_src),
            "judge_enabled": False,
            "threshold": None,
            "cap": None,
            "considered": 0,
            "promoted": [],
            "skipped_low_confidence": 0,
            "skipped_no_evidence": 0,
            "skipped_conflict": 0,
            "skipped_duplicate": 0,
            "skipped_flagged": 0,
            "flagged_behavior_change": 0,
            "held_low_confidence": 0,
            "judge_errors": 0,
            "judge_calls": 0,
            "judge_budget_exhausted": False,
        }
        if not summary["enabled"]:
            # No-op when disarmed: do not even read the queue.
            return summary

        from memory_judge import judge_candidate, judge_enabled

        use_judge = judge_enabled(env_src)
        summary["judge_enabled"] = use_judge

        bar = (
            float(threshold)
            if threshold is not None
            else _env_float(
                "ALFRED_AUTO_PROMOTE_THRESHOLD", AUTO_PROMOTE_DEFAULT_THRESHOLD, env_src
            )
        )
        # The low default bar only makes sense because the LLM judge is the
        # real decider. With the judge off, raise the bar to a conservative
        # floor so default-confidence candidates are not blindly promoted with
        # no model or human review.
        if not use_judge:
            bar = max(
                bar,
                _env_float(
                    "ALFRED_AUTO_PROMOTE_NO_JUDGE_THRESHOLD",
                    AUTO_PROMOTE_NO_JUDGE_THRESHOLD,
                    env_src,
                ),
            )
        cap = (
            int(max_per_run)
            if max_per_run is not None
            else int(
                _env_float(
                    "ALFRED_AUTO_PROMOTE_MAX_PER_RUN",
                    AUTO_PROMOTE_DEFAULT_MAX_PER_RUN,
                    env_src,
                )
            )
        )
        # Per-run judge-call budget. The promotion ``cap`` only limits successful
        # promotions, but a rejected/duplicate/flagged row still costs a judge
        # call, so judging is bounded by this instead. Never below the promotion
        # cap (you must be able to judge enough to fill it).
        max_judge_calls = max(
            cap,
            int(
                _env_float(
                    "ALFRED_AUTO_PROMOTE_MAX_JUDGE_CALLS",
                    AUTO_PROMOTE_DEFAULT_MAX_JUDGE_CALLS,
                    env_src,
                )
            ),
        )
        summary["threshold"] = bar
        summary["cap"] = cap
        summary["max_judge_calls"] = max_judge_calls
        judge_calls = 0

        candidates = self.list_memory_candidates(status="candidate", limit=500)
        summary["considered"] = len(candidates)
        # Count normalized bodies so genuine conflicts (>1 unreviewed version)
        # are left for a human.
        seen: dict[str, int] = {}
        for cand in candidates:
            key = _auto_dedup_key(cand.body)
            seen[key] = seen.get(key, 0) + 1
        conflict_keys = {key for key, count in seen.items() if count > 1}

        promoted = 0
        for candidate in candidates:
            if promoted >= cap:
                break
            if (candidate.review_note or "").startswith(_AUTO_HELD_MARKER):
                # Already held for a human on a prior run; never reprocess.
                summary["skipped_flagged"] += 1
                continue
            if not (candidate.evidence or "").strip():
                summary["skipped_no_evidence"] += 1
                continue
            if _auto_dedup_key(candidate.body) in conflict_keys:
                summary["skipped_conflict"] += 1
                continue
            try:
                confidence = float(candidate.confidence)
            except (TypeError, ValueError):
                confidence = 0.0

            # Structural confidence is a prerequisite, and the judge can only
            # LOWER it (never rescue), so a below-bar candidate can never pass.
            # Skip it BEFORE spending a judge call so a queue of newer
            # low-confidence rows cannot exhaust the budget and starve older
            # promotable candidates.
            if confidence < bar:
                summary["skipped_low_confidence"] += 1
                continue

            note = f"auto-promoted (confidence={confidence:.3f} >= {bar:.3f})"
            if use_judge:
                if judge_calls >= max_judge_calls:
                    # Spent the per-run judge budget. Stop here so the run stays
                    # bounded; remaining rows are picked up next run.
                    summary["judge_budget_exhausted"] = True
                    break
                judge_calls += 1
                verdict = judge_candidate(
                    topic=(candidate.body or "").split("\n", 1)[0][:200],
                    body=candidate.body or "",
                    evidence=candidate.evidence or "",
                    judge=judge,
                )
                if verdict is None:
                    # FAIL-SOFT: a failed/empty/unparseable judgment must NEVER
                    # auto-promote. Leave the candidate pending for the human.
                    summary["judge_errors"] += 1
                    continue
                if verdict.changes_agent_behavior:
                    rationale = verdict.rationale or "changes agent behavior"
                    self.hold_candidate_for_review(
                        candidate.id,
                        note=f"behavior-changing: {rationale}",
                    )
                    summary["flagged_behavior_change"] += 1
                    continue
                if verdict.is_duplicate:
                    # Hold (not reject): a rejected row drops out of the dedup
                    # index, so the next harvest would re-propose, re-create, and
                    # re-judge the same lesson. Held keeps it in the index while
                    # keeping it out of the re-judge loop.
                    self.hold_candidate_for_review(
                        candidate.id,
                        note=f"LLM judge: duplicate {verdict.rationale}".strip(),
                    )
                    summary["skipped_duplicate"] += 1
                    continue
                # Safe verdict. Take the LOWER of structural and judge
                # confidence so a high judge score can never lift a candidate
                # that failed the structural bar.
                confidence = min(confidence, verdict.confidence)
                if confidence < bar:
                    # The judge lowered confidence under the bar. Unlike a purely
                    # structural skip (which leaves the row pending for the next
                    # run), this row was JUDGED and is HELD for a human, so count
                    # it as a hold, not a transient low-confidence skip.
                    self.hold_candidate_for_review(
                        candidate.id,
                        note=(f"LLM judge confidence {confidence:.3f} < {bar:.3f}"),
                    )
                    summary["held_low_confidence"] += 1
                    continue
                note = (
                    f"auto-promoted (structural + LLM judge "
                    f"confidence={confidence:.3f} >= {bar:.3f})"
                )

            try:
                self.promote_memory_candidate(candidate.id, reviewer=reviewer, review_note=note)
            except ValueError:
                # The candidate changed under us (already promoted/rejected by a
                # concurrent reviewer). Skip without counting it.
                continue
            promoted += 1
            summary["promoted"].append(candidate.id)

        summary["judge_calls"] = judge_calls
        return summary

    def record_failure(
        self,
        *,
        codename: str,
        subtype: str,
        summary: str,
        repo: str | None = None,
        firing_id: str | None = None,
        engine: str | None = None,
        severity: Severity = "warning",
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FailureEvent:
        """Persist a normalized non-success event for later diagnosis."""
        if not codename or not subtype:
            raise ValueError("record_failure: codename and subtype are required")
        if severity not in ("info", "warning", "blocker"):
            raise ValueError(f"record_failure: unknown severity {severity!r}")
        event = FailureEvent(
            id=event_id or new_id(),
            codename=codename.strip(),
            repo=repo.strip() if repo else None,
            firing_id=firing_id,
            subtype=subtype.strip(),
            summary=(summary or "").strip(),
            engine=engine.strip() if engine else None,
            severity=severity,
            created_at=created_at or datetime.now(UTC),
        )
        return self.store.insert_failure_event(event)

    def upsert_github_item(
        self,
        *,
        repo: str,
        number: int,
        kind: GitHubItemKind,
        state: GitHubItemState,
        title: str = "",
        url: str = "",
        labels: Iterable[str] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        closed_at: datetime | None = None,
        merged_at: datetime | None = None,
        head_ref: str | None = None,
        base_ref: str | None = None,
        bundle_slug: str | None = None,
        changed_files: int | None = None,
        additions: int | None = None,
        deletions: int | None = None,
    ) -> GitHubItem:
        """Cache one GitHub issue or PR row.

        The poller is deliberately pull-based and idempotent: every run
        replaces the cached row for ``repo#number`` / ``kind`` with the
        latest shape it saw.
        """
        if not repo or not int(number):
            raise ValueError("upsert_github_item: repo and number are required")
        if kind not in ("issue", "pr"):
            raise ValueError(f"upsert_github_item: unknown kind {kind!r}")
        if state not in ("open", "closed", "merged", "unknown"):
            raise ValueError(f"upsert_github_item: unknown state {state!r}")
        now = datetime.now(UTC)
        clean_labels = sorted(
            {str(label).strip() for label in (labels or []) if str(label).strip()}
        )
        resolved_bundle = (bundle_slug or "").strip() or _bundle_slug_from_labels(clean_labels)
        item = GitHubItem(
            id=f"{repo}#{int(number)}:{kind}",
            repo=repo.strip(),
            number=int(number),
            kind=kind,
            state=state,
            title=(title or "").strip(),
            url=(url or "").strip(),
            labels=clean_labels,
            created_at=created_at,
            updated_at=updated_at or now,
            last_seen_at=last_seen_at or now,
            closed_at=closed_at,
            merged_at=merged_at,
            head_ref=head_ref,
            base_ref=base_ref,
            bundle_slug=resolved_bundle,
            changed_files=max(0, int(changed_files)) if changed_files is not None else None,
            additions=max(0, int(additions)) if additions is not None else None,
            deletions=max(0, int(deletions)) if deletions is not None else None,
        )
        persisted = self.store.upsert_github_item(item)
        if persisted.bundle_slug:
            self.store.upsert_bundle_item(
                BundleItem(
                    id=f"{persisted.bundle_slug}:{persisted.repo}#{persisted.number}:{persisted.kind}",
                    bundle_slug=persisted.bundle_slug,
                    repo=persisted.repo,
                    item_kind=persisted.kind,
                    number=persisted.number,
                    state=persisted.state,
                    title=persisted.title,
                    url=persisted.url,
                    labels=persisted.labels,
                    updated_at=persisted.updated_at,
                    last_seen_at=persisted.last_seen_at,
                )
            )
        return persisted

    def upsert_bundle_item(
        self,
        *,
        bundle_slug: str,
        repo: str,
        item_kind: GitHubItemKind,
        number: int,
        state: GitHubItemState,
        title: str = "",
        url: str = "",
        labels: Iterable[str] | None = None,
        updated_at: datetime | None = None,
        last_seen_at: datetime | None = None,
    ) -> BundleItem:
        """Upsert bundle membership without requiring a full GitHub row."""
        if not bundle_slug or not repo or not int(number):
            raise ValueError("upsert_bundle_item: bundle_slug, repo, and number are required")
        now = datetime.now(UTC)
        item = BundleItem(
            id=f"{bundle_slug}:{repo}#{int(number)}:{item_kind}",
            bundle_slug=bundle_slug.strip(),
            repo=repo.strip(),
            item_kind=item_kind,
            number=int(number),
            state=state,
            title=(title or "").strip(),
            url=(url or "").strip(),
            labels=sorted({str(label).strip() for label in (labels or []) if str(label).strip()}),
            updated_at=updated_at or now,
            last_seen_at=last_seen_at or now,
        )
        return self.store.upsert_bundle_item(item)

    def upsert_worker_heartbeat(
        self,
        *,
        codename: str,
        firing_id: str,
        status: WorkerStatus = "running",
        started_at: datetime | None = None,
        heartbeat_at: datetime | None = None,
        repo: str | None = None,
        pid: int | None = None,
        detail: str = "",
    ) -> WorkerHeartbeat:
        """Record the latest liveness signal for one worker firing."""
        if not codename or not firing_id:
            raise ValueError("upsert_worker_heartbeat: codename and firing_id are required")
        if status not in ("running", "ok", "failed", "stale", "cancelled"):
            raise ValueError(f"upsert_worker_heartbeat: unknown status {status!r}")
        now = datetime.now(UTC)
        heartbeat = WorkerHeartbeat(
            id=f"{codename.strip()}:{firing_id.strip()}",
            codename=codename.strip(),
            firing_id=firing_id.strip(),
            status=status,
            started_at=started_at or now,
            heartbeat_at=heartbeat_at or now,
            repo=repo.strip() if repo else None,
            pid=int(pid) if pid is not None else None,
            detail=(detail or "").strip(),
        )
        return self.store.upsert_worker_heartbeat(heartbeat)

    # ----- read paths ---------------------------------------------------

    def recall(
        self,
        codename: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        *,
        limit: int = _RECALL_DEFAULT,
    ) -> list[Lesson]:
        """Return the most-recent-first lessons matching the filters.

        Calling shape mirrors the prompt-prepend pattern: the runner
        does ``brain.recall(codename, repo)`` and dumps the bodies
        into the firing's system prompt.
        """
        clamped = max(1, min(int(limit), _RECALL_MAX))
        return self.store.recall_lessons(
            codename=codename,
            repo=repo,
            query=query,
            limit=clamped,
        )

    def get_repo_note(self, repo: str) -> RepoNote | None:
        return self.store.get_repo_note(repo)

    def list_lessons(self, limit: int | None = None) -> list[Lesson]:
        return self.store.list_lessons(limit=limit)

    def list_firings(
        self,
        codename: str | None = None,
        status: FiringStatus | None = None,
        limit: int = 50,
    ) -> list[FiringLog]:
        return self.store.list_firing_logs(codename=codename, status=status, limit=limit)

    def list_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        limit: int = 50,
    ) -> list[FileTouch]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_file_touches(
            repo=repo,
            codename=codename,
            path=path,
            limit=clamped,
        )

    def count_file_touches(
        self,
        repo: str | None = None,
        codename: str | None = None,
        path: str | None = None,
        touched_since: datetime | None = None,
    ) -> int:
        """Exact COUNT(*) of file_touches, unbounded by the list 500-row cap.

        ``list_file_touches`` clamps ``limit`` to 500, so callers that need a
        true total (e.g. proof-telemetry's lifetime counts) must use this rather
        than ``len(list_...())``, which silently freezes at 500 on a busy brain.
        """
        return self.store.count_file_touches(
            repo=repo,
            codename=codename,
            path=path,
            touched_since=touched_since,
        )

    def list_memory_candidates(
        self,
        status: MemoryCandidateStatus | None = "candidate",
        repo: str | None = None,
        codename: str | None = None,
        limit: int = 50,
    ) -> list[MemoryCandidate]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_memory_candidates(
            status=status,
            repo=repo,
            codename=codename,
            limit=clamped,
        )

    def list_failures(
        self,
        repo: str | None = None,
        codename: str | None = None,
        subtype: str | None = None,
        limit: int = 50,
    ) -> list[FailureEvent]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_failure_events(
            repo=repo,
            codename=codename,
            subtype=subtype,
            limit=clamped,
        )

    def list_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        limit: int = 50,
    ) -> list[GitHubItem]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_github_items(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            limit=clamped,
        )

    def count_github_items(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Exact COUNT(*) of github_items, unbounded by the list 500-row cap.

        ``list_github_items`` clamps ``limit`` to 500, so any caller needing a
        true total (proof-telemetry's lifetime PR counts) must use this. Counting
        by paginating ``list_github_items`` can never exceed 500 because the list
        method re-clamps every request.

        ``authored_only=True`` restricts the count to agent-authored PRs/issues:
        rows carrying the ``agent:authored`` provenance label or pushed from an
        agent branch prefix. The poller stores EVERY PR from ``gh pr list`` (not
        just Alfred's), so proof-telemetry passes this to avoid claiming PRs the
        fleet did not open. The filter is a SQL predicate on already-stored
        columns, so it stays an exact COUNT(*).

        ``agent_labeled_only=True`` restricts the count to rows with any
        ``agent:*`` label. Proof telemetry uses this for issue counts, where the
        public signal is the issue label rather than a branch name.
        """
        return self.store.count_github_items(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def sum_github_changed_lines(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Sum additions + deletions from cached GitHub PR rows.

        Proof telemetry uses this with ``kind="pr"`` and
        ``authored_only=True`` so the line-count metric is anchored to the same
        Alfred-authored PR subset as the PR counters.
        """
        return self.store.sum_github_changed_lines(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def sum_github_changed_files(
        self,
        repo: str | None = None,
        kind: GitHubItemKind | None = None,
        state: GitHubItemState | None = None,
        bundle_slug: str | None = None,
        authored_only: bool = False,
        agent_labeled_only: bool = False,
        created_since: datetime | None = None,
        closed_since: datetime | None = None,
        merged_since: datetime | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """Sum changed-file counts from cached GitHub PR rows."""
        return self.store.sum_github_changed_files(
            repo=repo,
            kind=kind,
            state=state,
            bundle_slug=bundle_slug,
            authored_only=authored_only,
            agent_labeled_only=agent_labeled_only,
            created_since=created_since,
            closed_since=closed_since,
            merged_since=merged_since,
            updated_since=updated_since,
        )

    def list_bundle_items(
        self,
        bundle_slug: str | None = None,
        state: GitHubItemState | None = None,
        limit: int = 50,
    ) -> list[BundleItem]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_bundle_items(bundle_slug=bundle_slug, state=state, limit=clamped)

    def list_worker_heartbeats(
        self,
        codename: str | None = None,
        status: WorkerStatus | None = None,
        limit: int = 50,
    ) -> list[WorkerHeartbeat]:
        clamped = max(1, min(int(limit), 500))
        return self.store.list_worker_heartbeats(
            codename=codename,
            status=status,
            limit=clamped,
        )

    def list_stale_workers(self, *, max_age_minutes: int = 60) -> list[WorkerHeartbeat]:
        """Return running worker heartbeats older than ``max_age_minutes``."""
        cutoff = datetime.now(UTC) - timedelta(minutes=max(1, int(max_age_minutes)))
        return [
            hb
            for hb in self.list_worker_heartbeats(status="running", limit=500)
            if hb.heartbeat_at < cutoff
        ]

    def list_failure_patterns(
        self,
        *,
        repo: str | None = None,
        codename: str | None = None,
        window_days: int = 7,
        min_count: int = 2,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Group repeated failures and attach a suggested operator action.

        This is the "reliability governor" read path. It does not mutate
        fleet state. The goal is to turn repeated Slack-style error noise
        into a small queue of concrete next actions.
        """
        cutoff = datetime.now(UTC) - timedelta(days=max(1, int(window_days)))
        grouped: dict[tuple[str, str, str, str], list[FailureEvent]] = {}
        for failure in self.list_failures(repo=repo, codename=codename, limit=500):
            if failure.created_at < cutoff:
                continue
            key = (
                failure.codename,
                failure.repo or "",
                failure.subtype or "unknown",
                failure.engine or "",
            )
            grouped.setdefault(key, []).append(failure)

        patterns: list[dict[str, Any]] = []
        threshold = max(1, int(min_count))
        for (agent, failure_repo, subtype, engine), rows in grouped.items():
            if len(rows) < threshold:
                continue
            rows.sort(key=lambda item: item.created_at)
            latest = rows[-1]
            if _is_non_actionable_failure_pattern(subtype, latest.summary):
                continue
            classification = _classify_failure_pattern(subtype, latest.summary)
            action = _suggest_failure_action(
                classification=classification,
                codename=agent,
                count=len(rows),
            )
            severity = "blocker" if action in {"pause_agent", "file_setup_issue"} else "warning"
            patterns.append(
                {
                    "key": "|".join([agent, failure_repo or "-", subtype, engine or "-"]),
                    "codename": agent,
                    "repo": failure_repo or None,
                    "subtype": subtype,
                    "engine": engine or None,
                    "count": len(rows),
                    "first_seen": rows[0].created_at.isoformat(),
                    "last_seen": latest.created_at.isoformat(),
                    "latest_summary": latest.summary,
                    "classification": classification,
                    "suggested_action": action,
                    "severity": severity,
                    "evidence_ids": [row.id for row in rows[-5:]],
                }
            )
        patterns.sort(
            key=lambda item: (
                item["severity"] != "blocker",
                -int(item["count"]),
                str(item["last_seen"]),
            )
        )
        return patterns[: max(1, min(int(limit), 100))]

    def suggest_memory_promotions(
        self,
        *,
        min_confidence: float = 0.75,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return reviewable candidates that look safe to promote.

        This is intentionally advisory. Alfred still keeps the human
        promotion step unless an operator explicitly scripts around it.
        """
        rows = self.list_memory_candidates(status="candidate", limit=500)
        suggestions: list[dict[str, Any]] = []
        trusted_bodies = {
            (lesson.repo, _canonical_memory_body(lesson.body)) for lesson in self.list_lessons()
        }
        for candidate in rows:
            canonical = _canonical_memory_body(candidate.body)
            if (candidate.repo, canonical) in trusted_bodies:
                continue
            score = float(candidate.confidence)
            reasons: list[str] = []
            if candidate.confidence >= min_confidence:
                reasons.append(f"confidence {candidate.confidence:.2f}")
            if candidate.evidence:
                score += 0.08
                reasons.append("has evidence")
            if candidate.tags:
                score += 0.03
                reasons.append("tagged")
            if candidate.severity in {"warning", "blocker"}:
                score += 0.04
                reasons.append(f"severity {candidate.severity}")
            if not reasons or score < min_confidence:
                continue
            suggestions.append(
                {
                    "candidate_id": candidate.id,
                    "codename": candidate.codename,
                    "repo": candidate.repo,
                    "body": candidate.body,
                    "score": round(min(score, 1.0), 3),
                    "reasons": reasons,
                }
            )
        suggestions.sort(key=lambda item: float(item["score"]), reverse=True)
        return suggestions[: max(1, min(int(limit), 100))]

    def reliability_report(
        self,
        *,
        window_days: int = 7,
        failure_min_count: int = 2,
        stale_worker_minutes: int = 60,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return the operator-facing reliability governor report."""
        patterns = self.list_failure_patterns(
            window_days=window_days,
            min_count=failure_min_count,
            limit=limit,
        )
        stale_workers = self.list_stale_workers(max_age_minutes=stale_worker_minutes)
        promotions = self.suggest_memory_promotions(limit=limit)
        actions: list[dict[str, Any]] = []
        for pattern in patterns:
            actions.append(
                {
                    "kind": "failure_pattern",
                    "severity": pattern["severity"],
                    "action": pattern["suggested_action"],
                    "summary": _failure_action_summary(pattern),
                    "target": pattern["codename"],
                    "evidence": pattern["evidence_ids"],
                }
            )
        for worker in stale_workers[:limit]:
            actions.append(
                {
                    "kind": "stale_worker",
                    "severity": "warning",
                    "action": "inspect_worker",
                    "summary": (
                        f"{worker.codename} firing {worker.firing_id} has not "
                        f"sent a heartbeat recently"
                    ),
                    "target": worker.codename,
                    "evidence": [worker.id],
                }
            )
        if promotions:
            actions.append(
                {
                    "kind": "memory_promotion",
                    "severity": "info",
                    "action": "review_memory",
                    "summary": f"{len(promotions)} memory candidate(s) look promotable",
                    "target": None,
                    "evidence": [str(item["candidate_id"]) for item in promotions[:limit]],
                }
            )

        status = "ok"
        if any(item["severity"] == "blocker" for item in actions):
            status = "fail"
        elif actions:
            status = "warn"
        return {
            "status": status,
            "checked_at": datetime.now(UTC).isoformat(),
            "window_days": max(1, int(window_days)),
            "failure_min_count": max(1, int(failure_min_count)),
            "failure_patterns": patterns,
            "stale_workers": [_serialize(asdict(worker)) for worker in stale_workers[:limit]],
            "promotion_suggestions": promotions,
            "actions": actions,
        }

    def stats(self) -> dict[str, int]:
        return self.store.stats()

    def doctor(self) -> dict[str, Any]:
        """Return a read-only health report for the memory store."""
        from .schema import SCHEMA_VERSION

        stats = self.stats()
        checks: list[dict[str, str]] = []

        def check(name: str, status: str, detail: str) -> None:
            checks.append({"name": name, "status": status, "detail": detail})

        check("schema", "ok", f"expected schema v{SCHEMA_VERSION}")
        open_candidates = stats.get("memory_candidates_open", 0)
        if open_candidates > 100:
            check("candidate_backlog", "fail", f"{open_candidates} candidates need review")
        elif open_candidates > 20:
            check("candidate_backlog", "warn", f"{open_candidates} candidates need review")
        else:
            check("candidate_backlog", "ok", f"{open_candidates} open candidates")

        recent_failures = self.list_failures(limit=20)
        blocker_failures = [F for F in recent_failures if F.severity == "blocker"]
        if blocker_failures:
            check("recent_failures", "fail", f"{len(blocker_failures)} blocker failure(s)")
        elif recent_failures:
            check("recent_failures", "warn", f"{len(recent_failures)} recorded failure(s)")
        else:
            check("recent_failures", "ok", "no recorded failures")

        stale_workers = self.list_stale_workers(max_age_minutes=60)
        if stale_workers:
            check("stale_workers", "warn", f"{len(stale_workers)} running worker(s) look stale")
        else:
            check("stale_workers", "ok", f"{stats.get('workers_running', 0)} running worker(s)")

        github_items = stats.get("github_items", 0)
        if github_items:
            check("github_poll", "ok", f"{github_items} cached GitHub issue/PR item(s)")
        else:
            check("github_poll", "warn", "no cached GitHub poll data yet")

        bundle_items = stats.get("bundle_items", 0)
        check("bundles", "ok", f"{bundle_items} cached bundle item(s)")

        suggestions = self.suggest_memory_promotions(limit=5)
        if suggestions:
            check("promotion_loop", "warn", f"{len(suggestions)} candidate(s) look promotable")
        else:
            check("promotion_loop", "ok", "no high-confidence candidates waiting")

        patterns = self.list_failure_patterns(limit=5)
        blocker_patterns = [p for p in patterns if p["severity"] == "blocker"]
        if blocker_patterns:
            check(
                "reliability_governor",
                "fail",
                f"{len(blocker_patterns)} repeated blocker failure pattern(s)",
            )
        elif patterns:
            check("reliability_governor", "warn", f"{len(patterns)} repeated pattern(s)")
        else:
            check("reliability_governor", "ok", "no repeated failure patterns")

        if stats.get("lessons", 0) == 0 and open_candidates == 0:
            check("recall_seed", "warn", "no trusted lessons or candidates yet")
        else:
            check("recall_seed", "ok", "memory has seed data")

        status = "ok"
        if any(c["status"] == "fail" for c in checks):
            status = "fail"
        elif any(c["status"] == "warn" for c in checks):
            status = "warn"
        return {
            "status": status,
            "checked_at": datetime.now(UTC).isoformat(),
            "stats": stats,
            "checks": checks,
        }

    # ----- delete paths -------------------------------------------------

    def forget(self, lesson_id: str) -> bool:
        """Delete a single lesson by id. Returns True if it existed."""
        return self.store.delete_lesson(lesson_id)

    def forget_before(self, *, days: int | None = None, before: datetime | None = None) -> int:
        """GC lessons older than ``days`` (or older than ``before``).

        Pass exactly one of ``days`` or ``before``.
        """
        if (days is None) == (before is None):
            raise ValueError("forget_before: pass exactly one of days= or before=")
        cutoff = before
        if cutoff is None:
            assert days is not None  # for mypy
            cutoff = datetime.now(UTC) - timedelta(days=int(days))
        return self.store.delete_lessons_before(cutoff)

    # ----- export -------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of the entire brain.

        Format::

            {
              "schema_version": 3,
              "exported_at": "2026-05-23T...Z",
              "lessons": [{...}, ...],
              "repo_notes": [{...}, ...],
              "firings": [{...}, ...],
              "file_touches": [{...}, ...],
              "memory_candidates": [{...}, ...],
              "failure_events": [{...}, ...]
            }

        ``alfred brain export`` writes this to disk. Restoring is
        currently manual: re-run reflect/firing_log/note_repo on the
        target host.
        """
        from .schema import SCHEMA_VERSION

        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "lessons": [_serialize(asdict(L)) for L in self.list_lessons()],
            "repo_notes": [_serialize(asdict(n)) for n in self._all_repo_notes()],
            "firings": [_serialize(asdict(F)) for F in self.list_firings(limit=10_000)],
            "file_touches": [_serialize(asdict(T)) for T in self.list_file_touches(limit=10_000)],
            "memory_candidates": [
                _serialize(asdict(C))
                for C in self.list_memory_candidates(status=None, limit=10_000)
            ],
            "failure_events": [_serialize(asdict(F)) for F in self.list_failures(limit=10_000)],
            "github_items": [_serialize(asdict(G)) for G in self.list_github_items(limit=10_000)],
            "bundle_items": [_serialize(asdict(B)) for B in self.list_bundle_items(limit=10_000)],
            "worker_heartbeats": [
                _serialize(asdict(H)) for H in self.list_worker_heartbeats(limit=10_000)
            ],
        }

    def _all_repo_notes(self) -> list[RepoNote]:
        """Pull every repo note via a list_lessons-style sweep.

        The store doesn't expose a list method for notes today (the
        operator queries by repo); export needs everything, so we
        derive the repo set from existing lessons + any note we have.
        For now we use the lessons table as the source of repo keys.
        """
        seen: set[str] = set()
        out: list[RepoNote] = []
        for L in self.list_lessons():
            if L.repo in seen:
                continue
            seen.add(L.repo)
            note = self.store.get_repo_note(L.repo)
            if note is not None:
                out.append(note)
        return out


def _serialize(d: dict[str, Any]) -> dict[str, Any]:
    """Best-effort JSON serialization: datetime -> ISO, everything else
    passes through. Used for export only."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, datetime):
            out[k] = v.astimezone(UTC).isoformat()
        else:
            out[k] = v
    return out


def _classify_failure_pattern(subtype: str, summary: str) -> str:
    text = f"{subtype} {summary}".lower()
    if any(token in text for token in ("executable doesn't exist", "playwright", "chromium")):
        return "local_setup"
    if any(token in text for token in ("auth", "token", "sso", "accessdenied", "permission")):
        return "auth"
    if any(token in text for token in ("rate_limit", "quota", "budget", "too many requests")):
        return "provider_limit"
    if any(token in text for token in ("timeout", "timed out", "error_timeout")):
        return "timeout"
    if any(token in text for token in ("no-commit", "no commit", "wip", "salvage")):
        return "agent_quality"
    return "unknown"


def _is_non_actionable_failure_pattern(subtype: str, summary: str) -> bool:
    normalized = str(subtype or "").strip().lower()
    if normalized in _NON_ACTIONABLE_FAILURE_SUBTYPES:
        return True
    text = f"{normalized} {summary or ''}".lower()
    if any(token in text for token in ("error", "fail", "timeout", "blocked", "crash")):
        return False
    return normalized.endswith("-cap")


def _suggest_failure_action(*, classification: str, codename: str, count: int) -> str:
    if classification == "local_setup":
        return "file_setup_issue"
    if classification == "auth":
        return "ask_human"
    if classification == "provider_limit":
        return "retry_later"
    if classification == "agent_quality":
        return "review_prompt_or_checks"
    if classification == "timeout" and count >= 3:
        return "pause_agent"
    if classification == "timeout":
        return "retry_later"
    if count >= 3:
        return "pause_agent"
    return "inspect"


def _failure_action_summary(pattern: dict[str, Any]) -> str:
    repo = f" on {pattern['repo']}" if pattern.get("repo") else ""
    return (
        f"{pattern['codename']} has {pattern['count']} repeated "
        f"{pattern['classification']} failure(s){repo}: "
        f"{pattern['suggested_action']}"
    )


def _canonical_memory_body(body: str) -> str:
    return " ".join((body or "").strip().lower().split())


def _bundle_slug_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("agent:bundle:"):
            return label.removeprefix("agent:bundle:").strip() or None
        if label.startswith("bundle:"):
            return label.removeprefix("bundle:").strip() or None
    return None
