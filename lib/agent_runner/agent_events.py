"""Typed, sequenced event envelope for per-firing telemetry.

This module turns Alfred's previously-untyped ``EventLog`` JSONL into a
**closed, typed, monotonically sequenced** event stream. It is the substrate
for resumable run timelines, live run cards, and future fail-closed gates: a
per-firing ``seq`` lets a consumer tail/replay deterministically and lets the
reader detect gaps.

Design (deliberately small, no external deps):

- ``EventType``           - a closed ``str`` enum of every event the fleet
  actually emits, enumerated from every ``.emit(...)`` call site. Constructing
  an ``Event`` with a type outside this set raises :class:`UnknownEventType`,
  so a typo can never silently mint a new event kind. That is the whole point.
- ``Event``               - the envelope dataclass carrying a monotonic
  per-firing ``seq``, a UTC-ISO ``ts``, the ``type``, stable
  ``firing_id`` / ``agent`` / ``stage`` identity, and a validated ``payload``
  dict. Per-type required payload keys are enforced at construction.
- Serialization keeps the legacy top-level ``event`` field (== ``type``) so the
  existing reader / streaming consumers that key off ``event`` keep working,
  while ``seq`` / ``type`` / ``stage`` are added for the typed consumers.

The actual append/fsync/seq-stamping lives on ``EventLog`` in
``agent_runner.state`` (it owns the on-disk path); this module owns the
vocabulary, the envelope, and the validation so both the writer and the
reader share one source of truth.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


# ---------------------------------------------------------------------------
# Closed event vocabulary
#
# Every member here was enumerated from a real ``.emit(...)`` call site in
# ``bin/*.py`` plus the shared ``agent_runner`` library. Do NOT add a member
# unless a runner actually emits it; do NOT remove one without migrating its
# emitter. The step-level milestones (``plan_created``,
# ``pre_push_checks_passed``, ``branch_pushed``) were added so the run timeline
# reads as the firing's story rather than just ``firing_started`` ->
# ``firing_complete``; each is emitted only after the real action succeeded.
#
# Three lifecycle terminal names (``firing_failed``, ``firing_aborted``) are
# included even though no runner emits them today: the reader and the SSE
# streaming layer already treat them as terminal markers, and legacy on-disk
# data may carry them, so they are part of the closed contract.
# ---------------------------------------------------------------------------
class EventType(enum.StrEnum):
    """Closed set of firing event types. Membership is the validation gate.

    A :class:`enum.StrEnum` so each member is its own string value: ``str(member)``
    and JSON serialization round-trip to the raw event name, and ``member ==
    "firing_started"`` holds, which keeps the on-disk records and the legacy
    string-keyed call sites byte-compatible with the untyped predecessor.
    """

    # -- lifecycle ----------------------------------------------------------
    FIRING_STARTED = "firing_started"
    PREFLIGHT_PASSED = "preflight_passed"
    FIRING_COMPLETE = "firing_complete"
    FIRING_COMPLETED = "firing_completed"
    FIRING_DONE = "firing_done"
    # Terminal markers the reader / streaming layer recognise. No runner emits
    # these today, but they are part of the lifecycle contract and may appear
    # in legacy data, so they stay in the closed set.
    FIRING_FAILED = "firing_failed"
    FIRING_ABORTED = "firing_aborted"
    EXECUTION_ABORTED = "execution_aborted"

    # -- repo / issue / PR selection + work --------------------------------
    REPO_PICKED = "repo_picked"
    ISSUE_PICKED = "issue_picked"
    ISSUES_INSPECTED = "issues_inspected"
    PR_PICKED = "pr_picked"
    PR_OPENED = "pr_opened"
    WORKTREE_CREATED = "worktree_created"
    REPO_ALREADY_IMPLEMENTED = "repo_already_implemented"
    REPO_KILLED = "repo_killed"
    REPO_SMOKE = "repo_smoke"
    REPO_INVOKE_START = "repo_invoke_start"
    REPO_INVOKE_DONE = "repo_invoke_done"
    REPO_BUDGET_SKIP = "repo_budget_skip"
    FIX_PUSHED = "fix_pushed"
    # An example/agent skipped because an open authored PR already covers the
    # issue (dedup). Emitted by the reference echo agent and any dedup-aware
    # runner as a first-class event type, distinct from the ``dedup_skip``
    # firing-complete *outcome* the engineering runners record.
    DEDUP_SKIP = "dedup_skip"
    # Step-level work milestones. These mark real lifecycle points so the
    # run timeline tells the firing's story instead of just start -> complete:
    # the pre-push gate actually ran the repo's lint/compile/test command, and
    # the branch was actually pushed to the remote. Emit them only after the
    # underlying action truly succeeded - never as speculative progress.
    PRE_PUSH_CHECKS_PASSED = "pre_push_checks_passed"
    BRANCH_PUSHED = "branch_pushed"

    # -- engine / LLM invocation -------------------------------------------
    LLM_INVOKE_DONE = "llm_invoke_done"
    CLAUDE_INVOKE_DONE = "claude_invoke_done"
    LLM_FALLBACK = "llm_fallback"

    # -- review / triage ----------------------------------------------------
    REVIEW_POSTED = "review_posted"
    TRIAGED = "triaged"
    TRIAGES_REJECTED = "triages_rejected"
    TRIAGE_IMPLEMENT_STRIPPED = "triage_implement_stripped"
    TRIAGE_REFETCH_FAILED = "triage_refetch_failed"
    WORKFLOW_VALIDATION_FAILED = "workflow_validation_failed"

    # -- planning -----------------------------------------------------------
    # The plan was drafted and written to disk. This is the real moment the
    # planner produced its plan, distinct from posting it to Slack
    # (plan_posted_*) or an operator approving it (plan_approved).
    PLAN_CREATED = "plan_created"
    PLAN_APPROVED = "plan_approved"
    PLAN_FEEDBACK_CAPTURED = "plan_feedback_captured"
    PLAN_POSTED_SLACK = "plan_posted_slack"
    PLAN_POSTED_FILE_MODE = "plan_posted_file_mode"
    PLAN_POSTED_FALLBACK = "plan_posted_fallback"
    PLAN_REPO_SCOPE_AMENDED = "plan_repo_scope_amended"

    # -- goals --------------------------------------------------------------
    GOAL_ATTEMPT_LOGGED = "goal_attempt_logged"

    # -- budget / spend -----------------------------------------------------
    BUDGET_SKIP = "budget_skip"

    # -- pause / block ------------------------------------------------------
    AGENT_PAUSED = "agent_paused"
    EVENT_PAUSED_MARKER = "event_paused_marker"
    GLOBAL_BLOCK_SET = "global_block_set"
    GLOBAL_BLOCK_SET_FAILED = "global_block_set_failed"

    # -- slack --------------------------------------------------------------
    SLACK_POST_OK = "slack_post_ok"
    SLACK_POST_SKIPPED = "slack_post_skipped"

    # -- infra / ops checks (fleet-doctor, gordon, huntress) ---------------
    CHECKS_DONE = "checks_done"
    ECS_DRIFT_CHECKED = "ecs_drift_checked"
    STAGING_HEALTH_CHECKED = "staging_health_checked"
    SENTRY_FETCHED = "sentry_fetched"
    PLAYWRIGHT_DONE = "playwright_done"
    EVENT_DATA_OPS_SKIPPED = "event_data_ops_skipped"


# Fast membership set of the raw string values.
_KNOWN_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in EventType)

# Public alias: the closed set of recognized typed-envelope ``type`` values.
# Consumers (e.g. the reader's ``_event_type``) use this to tell a real typed
# envelope from a legacy line that merely carries a freeform payload field
# named ``type``, so they only trust ``type`` when it names a known envelope.
KNOWN_EVENT_TYPES: frozenset[str] = _KNOWN_TYPE_VALUES


# Lifecycle starts / terminals, exported so the reader and streaming layer
# share one definition instead of hard-coding string sets in three places.
START_TYPES: frozenset[str] = frozenset(
    {
        EventType.FIRING_STARTED.value,
        EventType.PREFLIGHT_PASSED.value,
    }
)

TERMINAL_TYPES: frozenset[str] = frozenset(
    {
        EventType.FIRING_COMPLETE.value,
        EventType.FIRING_COMPLETED.value,
        EventType.FIRING_DONE.value,
        EventType.FIRING_FAILED.value,
        EventType.FIRING_ABORTED.value,
    }
)


# ---------------------------------------------------------------------------
# Per-type required payload keys
#
# A validated dict (not a dataclass per type) keeps the freeform call sites
# working while still enforcing that the load-bearing keys are present. Only
# keys that a consumer actually reads, or that are meaningless to omit, are
# required. Everything else stays optional in the open payload bag.
#
# The dominant contract: ``firing_complete`` MUST carry ``outcome`` - the
# reader's success/failure classification (``_is_failure_outcome``) hinges on
# it, so emitting a terminal event without an outcome is a real bug we reject
# at write time rather than silently mislabel the firing as "ok".
# ---------------------------------------------------------------------------
REQUIRED_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    EventType.FIRING_COMPLETE.value: ("outcome",),
    EventType.LLM_FALLBACK.value: ("from_engine", "to_engine", "reason"),
}


# Top-level envelope keys that must never be shadowed by a payload kwarg.
_RESERVED_KEYS: frozenset[str] = frozenset(
    {"seq", "ts", "type", "event", "agent", "firing_id", "stage"}
)


class UnknownEventType(ValueError):
    """Raised when an event type outside the closed :class:`EventType` set is
    used. This is the typed-and-closed guarantee: a typo cannot mint a new
    silent event kind."""


class EventPayloadError(ValueError):
    """Raised when an event's payload is missing a required key, or a payload
    key collides with a reserved envelope key."""


def coerce_type(event_type: str | EventType) -> str:
    """Validate ``event_type`` against the closed set and return its str value.

    Accepts either an :class:`EventType` member or its raw string value.
    Raises :class:`UnknownEventType` for anything else.
    """
    if isinstance(event_type, EventType):
        return event_type.value
    value = str(event_type)
    if value not in _KNOWN_TYPE_VALUES:
        raise UnknownEventType(
            f"unknown event type {value!r}; not in the closed EventType set. "
            "Add a member to EventType (and migrate its emitter) before emitting it."
        )
    return value


def _validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    """Enforce reserved-key collisions and per-type required keys."""
    for key in payload:
        if key in _RESERVED_KEYS:
            raise EventPayloadError(
                f"payload key {key!r} collides with a reserved envelope field; "
                "rename it at the call site."
            )
    required = REQUIRED_PAYLOAD_KEYS.get(event_type, ())
    missing = [k for k in required if k not in payload]
    if missing:
        raise EventPayloadError(
            f"event {event_type!r} requires payload key(s) {missing}; got keys {sorted(payload)}."
        )


def utc_now_iso() -> str:
    """UTC ISO-8601 with microseconds and a trailing ``Z`` (matches the legacy
    EventLog stamp format exactly so timeline ordering is byte-compatible)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Event:
    """One typed, sequenced firing event.

    Construct via :meth:`create` (which validates) rather than the raw
    constructor. ``seq`` is stamped by :class:`EventLog` at append time, not
    by the caller.
    """

    seq: int
    ts: str
    type: str
    agent: str
    firing_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None

    @classmethod
    def create(
        cls,
        *,
        seq: int,
        agent: str,
        firing_id: str,
        event_type: str | EventType,
        payload: Mapping[str, Any] | None = None,
        ts: str | None = None,
        stage: str | None = None,
    ) -> Event:
        """Build + validate one event envelope.

        Raises :class:`UnknownEventType` for an out-of-set type and
        :class:`EventPayloadError` for a missing required key or a reserved-key
        collision.
        """
        typ = coerce_type(event_type)
        body = dict(payload or {})
        _validate_payload(typ, body)
        return cls(
            seq=int(seq),
            ts=ts or utc_now_iso(),
            type=typ,
            agent=agent,
            firing_id=firing_id,
            payload=body,
            stage=stage,
        )

    def to_record(self) -> dict[str, Any]:
        """Serialize to the on-disk JSONL record shape.

        The record keeps the legacy top-level ``event`` field (== ``type``) and
        flattens the payload to top level, so the existing reader / streaming
        consumers that read ``event`` and freeform top-level kwargs keep working
        unchanged. ``seq`` / ``type`` (and ``stage`` when set) are the new typed
        fields the strict parser uses.
        """
        record: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "agent": self.agent,
            "firing_id": self.firing_id,
            "type": self.type,
            # Legacy compatibility: consumers keyed on ``event`` still work.
            "event": self.type,
        }
        if self.stage is not None:
            record["stage"] = self.stage
        # Payload is flattened to top level (legacy shape). Reserved keys were
        # already rejected at construction, so this cannot clobber the envelope.
        record.update(self.payload)
        return record


def parse_record(record: Mapping[str, Any]) -> Event | None:
    """Best-effort parse of one on-disk JSONL record into a typed :class:`Event`.

    Returns a typed ``Event`` only for records that carry the new envelope
    (a ``seq`` and a recognised ``type``/``event``). Legacy untyped records
    (no ``seq``, or an unknown type) return ``None`` so the caller can fall
    back to its loose parsing. This is the legacy-tolerance contract: NEW data
    is parsed strictly, OLD data is left to the best-effort path.
    """
    if not isinstance(record, Mapping):
        return None
    if "seq" not in record:
        return None
    try:
        seq = int(record["seq"])
    except (TypeError, ValueError):
        return None
    raw_type = record.get("type") or record.get("event")
    if raw_type is None or str(raw_type) not in _KNOWN_TYPE_VALUES:
        return None
    typ = str(raw_type)
    ts = str(record.get("ts") or "")
    agent = str(record.get("agent") or "")
    firing_id = str(record.get("firing_id") or "")
    stage_val = record.get("stage")
    stage = str(stage_val) if stage_val is not None else None
    payload = {k: v for k, v in record.items() if k not in _RESERVED_KEYS}
    return Event(
        seq=seq,
        ts=ts,
        type=typ,
        agent=agent,
        firing_id=firing_id,
        payload=payload,
        stage=stage,
    )


__all__ = [
    "KNOWN_EVENT_TYPES",
    "REQUIRED_PAYLOAD_KEYS",
    "START_TYPES",
    "TERMINAL_TYPES",
    "Event",
    "EventPayloadError",
    "EventType",
    "UnknownEventType",
    "coerce_type",
    "parse_record",
    "utc_now_iso",
]
