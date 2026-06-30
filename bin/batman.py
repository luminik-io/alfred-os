#!/usr/bin/env python3
"""``batman``, multi-repo feature architect.

Picks the oldest open ``agent:large-feature`` parent issue from
``BATMAN_PARENT_REPO``, drafts the rollout, captures approval when
configured, files scoped child issues, and reports what happened.
Batman does not directly edit repo files in the OSS reference runner;
Lucius, Bane, and Nightwing own the worktrees and PRs that flow from
those child issues.

Wiring:

  - Reads ``BATMAN_PARENT_REPO`` as the single parent queue repo.
  - Posts a plan summary via the ``slack_format`` thread root when a
    bot token is configured, falling back to the webhook ``slack_post``
    otherwise.
  - Honours the fleet enable file: if ``batman`` is not enabled there,
    the runner exits early with a one-line stderr note.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists():
        candidate_path = str(candidate)
        if candidate_path in sys.path:
            sys.path.remove(candidate_path)
        sys.path.insert(0, candidate_path)

import labels as label_constants  # noqa: E402
from agent_runner import (  # noqa: E402
    LIFECYCLE_LABELS,
    STATE_ROOT,
    EventLog,
    PreflightSpec,
    agent_engine,
    doctor_mode,
    dry_run_log,
    ensure_labels,
    gh_issue_edit,
    gh_json,
    is_agent_enabled,
    is_dry_run,
    preflight,
    run,
    slack_post,
    with_lock,
)
from batman import (  # noqa: E402
    APPROVAL_MODE_FILE,
    EXEC_GATE_DISABLED,
    EXEC_NO_CHILDREN,
    EXEC_OK,
    LARGE_FEATURE_LABEL,
    ApprovalEnvelope,
    BatmanLifecycle,
    BatmanLifecycleConfig,
    SlackReporter,
)
from labels import PLAN_PENDING_APPROVAL  # noqa: E402

CODENAME = os.environ.get("AGENT_CODENAME", "batman")
BATMAN_ENGINE = agent_engine(CODENAME, default="hybrid")
ENV_EXECUTING_FANOUT_STALE_AFTER_S = "BATMAN_EXECUTING_FANOUT_STALE_AFTER_S"
DEFAULT_EXECUTING_FANOUT_STALE_AFTER_S = 3600
BATMAN_PICKUP_BLOCKING_LABELS = {
    label_constants.IN_FLIGHT,
    label_constants.PR_OPEN,
    label_constants.LEGACY_PR_OPEN,
    label_constants.DO_NOT_PICKUP,
    label_constants.NEEDS_HUMAN_SCOPE,
    label_constants.NEEDS_HUMAN_REVIEW,
    label_constants.NEEDS_INFO,
    label_constants.DONE,
    label_constants.DONE_ALREADY,
}


def _has_batman_pickup_blocker(label_names: set[str] | frozenset[str]) -> bool:
    """Batman owns large-feature and bundle labels; block only hard gates."""
    labels = set(label_names)
    return bool(
        (labels & BATMAN_PICKUP_BLOCKING_LABELS) or label_constants.agent_pr_open_labels(labels)
    )


def _firing_id() -> str:
    import secrets

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _list_parent_repo_large_features(parent_repo: str) -> list[dict]:
    """Return open ``agent:large-feature`` issues in ``parent_repo``.

    ``parent_repo`` is an ``owner/repo`` slug. Used by the lifecycle path
    (``BATMAN_PARENT_REPO``); falls back to ``[]`` on any gh search
    failure so the runner skips cleanly rather than crashing.
    """
    if not parent_repo:
        return []
    rows = gh_json(
        [
            "gh",
            "issue",
            "list",
            "-R",
            parent_repo,
            "--label",
            LARGE_FEATURE_LABEL,
            "--state",
            "open",
            "--json",
            "number,title,url,labels,createdAt,body",
            "--limit",
            "20",
        ],
        default=[],
    )
    if not isinstance(rows, list):
        return []
    eligible: list[dict] = []
    for r in rows:
        issue_number = int(r.get("number") or 0)
        if _has_completed_fanout_marker(parent_repo, issue_number):
            marker_state = _completed_fanout_marker_state(parent_repo, issue_number)
            recovered_completed_fanout = (
                marker_state == "executing"
                and _executing_fanout_marker_completed_remotely(parent_repo, issue_number)
            )
            if marker_state == "completed" or recovered_completed_fanout:
                event = (
                    "BATMAN-PARENT-FINALIZE-RECOVER"
                    if recovered_completed_fanout
                    else "BATMAN-PARENT-FINALIZE-RETRY"
                )
                print(f"[{event}] parent={parent_repo}#{issue_number}")
                if _finalize_parent_after_child_fanout(parent_repo, issue_number):
                    _clear_completed_fanout_marker(parent_repo, issue_number)
                continue
            if marker_state == "executing" and _executing_fanout_marker_is_stale(
                parent_repo, issue_number
            ):
                print(
                    f"[BATMAN-PARENT-FANOUT-MARKER-STALE] parent={parent_repo}#{issue_number} "
                    f"state=executing; clearing marker and retrying fanout",
                    file=sys.stderr,
                )
                _clear_completed_fanout_marker(parent_repo, issue_number)
                if _has_completed_fanout_marker(parent_repo, issue_number):
                    continue
            elif marker_state == "executing":
                print(
                    f"[BATMAN-PARENT-FANOUT-MARKER] parent={parent_repo}#{issue_number} "
                    f"state=executing; skipping re-fanout",
                    file=sys.stderr,
                )
                continue
            else:
                print(
                    f"[BATMAN-PARENT-FANOUT-MARKER-UNKNOWN] "
                    f"parent={parent_repo}#{issue_number} state={marker_state!r}; "
                    f"clearing unreadable marker and retrying fanout",
                    file=sys.stderr,
                )
                _clear_completed_fanout_marker(parent_repo, issue_number)
                if _has_completed_fanout_marker(parent_repo, issue_number):
                    continue
        labels = {label.get("name") for label in r.get("labels", []) if isinstance(label, dict)}
        if _has_batman_pickup_blocker(labels):
            continue
        eligible.append(r)
    return eligible


def _pick_parent_issue(issues: list[dict], *, picker: str = "oldest") -> dict | None:
    """Return the next parent issue to act on, or ``None`` if list empty.

    ``picker`` is read from ``BATMAN_PICKER``. ``oldest`` picks by
    ``createdAt`` ascending (the default and the safest pickup order:
    nothing starves while newer work jumps the queue). ``newest`` is
    available for operators who explicitly want last-filed first.
    """
    if not issues:
        return None
    if picker == "newest":
        return max(issues, key=lambda i: i.get("createdAt", ""))
    return min(issues, key=lambda i: i.get("createdAt", ""))


def _run_lifecycle(
    *,
    config: BatmanLifecycleConfig,
    parent_issue: dict,
    firing_id: str,
) -> int:
    """Run plan -> approve -> execute -> report for one parent issue.

    Wires up the real SlackReporter, gh CLI issue client, and (when the
    operator opts in via ``BATMAN_AUTO_EXECUTE=approval-gate``) the
    ``SlackApproval`` gate. The function is intentionally short: every
    interesting branch lives on the lifecycle dataclasses so the same
    code paths are exercised by ``tests/test_batman_execute.py`` via
    injected fakes.

    The body is bracketed with the standard ``firing_started`` ...
    ``firing_complete`` lifecycle records (matching every other runner) so
    the per-firing event log the lifecycle writes reads as a completed run.
    Without the terminal record, ``lib/server/reader.py`` would leave the
    firing stuck as ``unknown`` because the lone mid-firing ``plan_created``
    milestone is neither a start nor a terminal marker.
    """
    events = EventLog(agent=CODENAME, firing_id=firing_id)
    events.emit("firing_started")
    rc, outcome = _run_lifecycle_body(
        config=config,
        parent_issue=parent_issue,
        firing_id=firing_id,
        events=events,
    )
    # ``firing_complete`` MUST carry ``outcome`` (closed-set contract); the
    # reader's success/failure classification hinges on it. A broken event-log
    # write can never kill the firing - ``EventLog.append`` absorbs I/O errors
    # and only an unknown event type (a programmer typo) would raise here.
    events.emit("firing_complete", outcome=outcome)
    return rc


def _run_lifecycle_body(
    *,
    config: BatmanLifecycleConfig,
    parent_issue: dict,
    firing_id: str,
    events: EventLog,
) -> tuple[int, str]:
    """Plan -> approve -> execute -> report core, returning ``(rc, outcome)``.

    Split out of :func:`_run_lifecycle` so the lifecycle brackets
    (``firing_started`` / ``firing_complete``) wrap every terminal return in
    one place. The returned ``outcome`` string is the terminal classification
    stamped on the ``firing_complete`` record.
    """
    # Build the lifecycle. Imports here are deferred so no-op firings
    # do not pay for the optional slack_approval / slack_sdk dependency.
    reporter = SlackReporter(firing_id=firing_id, codename=CODENAME)
    gate = None
    file_only_approval = config.approval_mode == APPROVAL_MODE_FILE
    if config.gate_enabled and not file_only_approval:
        try:
            from slack_approval import (
                SlackApproval,
                default_slack_client,
                operator_user_id_from_env,
            )

            operator = operator_user_id_from_env()
            if not operator:
                print(
                    "[BATMAN-GATE-DISABLED] BATMAN_AUTO_EXECUTE=approval-gate "
                    "but ALFRED_OPERATOR_SLACK_USER_ID is unset; falling back "
                    "to halt-after-plan",
                    file=sys.stderr,
                )
            else:
                gate = SlackApproval(default_slack_client(), operator_user_id=operator)
        except Exception as e:
            print(
                f"[BATMAN-GATE-INIT-FAIL] {type(e).__name__}: {e}; halting after plan",
                file=sys.stderr,
            )

    lifecycle = BatmanLifecycle(
        config=config,
        gate=gate,
        reporter=reporter,
    )

    plan = lifecycle.plan(
        body=parent_issue.get("body") or "",
        title=parent_issue.get("title") or "",
        parent_repo=config.parent_repo,
        parent_issue_number=int(parent_issue.get("number") or 0),
    )

    print(
        f"[BATMAN-PLAN-DRAFTED] firing_id={firing_id} bundle={plan.bundle_slug} "
        f"children={len(plan.children)} repos={len(plan.affected_repos)}"
    )

    parent_repo = config.parent_repo
    parent_issue_number = int(parent_issue.get("number") or 0)

    # The plan is now drafted. Record it as a real step in the per-firing event
    # log, distinct from posting it for approval (the Slack approval gate) and
    # from an operator approving it, so the run timeline shows the plan came
    # into being. ``EventLog.append`` absorbs I/O errors itself (a broken write
    # never kills a firing); only an unknown event type - a closed-set
    # programmer error - would raise, and that is meant to crash loudly.
    events.emit(
        "plan_created",
        issue=parent_issue_number,
        affected_repos=list(plan.affected_repos),
        bundle=plan.bundle_slug,
        children=len(plan.children),
        detail=(
            f"{parent_repo}#{parent_issue_number} ({', '.join(plan.affected_repos) or 'no repos'})"
        ),
    )

    if not plan.children:
        detail = "; ".join(f.message for f in plan.readiness_blockers) or (
            "No child issues were parsed from the parent body."
        )
        print(
            f"[BATMAN-DECOMPOSITION-FAILED] parent={parent_repo}#{parent_issue_number} "
            f"bundle={plan.bundle_slug} children=0 repos={len(plan.affected_repos)} "
            f"detail={detail!r}",
            flush=True,
        )
        slack_post(
            f"[BATMAN-DECOMPOSITION-FAILED] parent={parent_repo}#{parent_issue_number}: "
            f"{detail} No approval was requested.",
            severity="warn",
        )
        _clear_pending_envelope(parent_repo, parent_issue_number)
        _unset_pending_approval_label(parent_repo, parent_issue_number)
        ensure_labels(parent_repo, LIFECYCLE_LABELS)
        gh_issue_edit(
            parent_repo,
            parent_issue_number,
            add_labels=[label_constants.NEEDS_HUMAN_SCOPE],
        )
        lifecycle.report(plan, _empty_result_reason(reason=EXEC_NO_CHILDREN))
        return 0, EXEC_NO_CHILDREN

    # Idempotent approval state (issue #115). On a pending parent issue
    # whose label says we already drafted a plan, do not re-post; instead
    # resume polling the previous Slack message. Operators see one plan
    # per parent issue instead of one per firing.
    existing_envelope: ApprovalEnvelope | None = None
    if _has_pending_approval_label(parent_issue):
        existing_envelope = _load_pending_envelope(parent_repo, parent_issue_number, plan=plan)
        if existing_envelope is not None:
            print(
                f"[BATMAN-APPROVAL-RESUME] parent={parent_repo}#{parent_issue_number} "
                f"ts={existing_envelope.message_ts}; not re-posting plan"
            )
        else:
            # Label says pending but state file is gone; treat as
            # stale and re-post. Operator can still see the label drop
            # at the end of this firing.
            print(
                "[BATMAN-APPROVAL-STALE-LABEL] `agent:plan-pending-approval` set "
                "but no recoverable state; re-drafting once.",
                file=sys.stderr,
            )

    envelope = existing_envelope
    if envelope is None:
        envelope = lifecycle.request_approval(plan)
        if envelope is None:
            print(
                f"[BATMAN-PLAN-POSTED-NO-TS] gate unavailable; respecting {config.auto_execute!r}",
                file=sys.stderr,
            )
        else:
            _save_pending_envelope(parent_repo, parent_issue_number, envelope, firing_id=firing_id)
            _set_pending_approval_label(parent_repo, parent_issue_number)

    # Decide whether to execute. The matrix:
    #   auto_execute=0 (off):        halt after plan, no execute.
    #   auto_execute=approval-gate:  poll the configured approval surface.
    #   auto_execute=1 (force):      execute immediately, no gate.
    if not config.execute_enabled:
        print("[BATMAN-HALT-AFTER-PLAN] BATMAN_AUTO_EXECUTE=0; not filing children")
        return 0, "halt-after-plan"

    if config.gate_enabled:
        if envelope is None and file_only_approval:
            envelope = ApprovalEnvelope(
                channel="file",
                message_ts=f"issue-{parent_issue_number}",
                plan=plan,
            )
        if envelope is None or (gate is None and not file_only_approval):
            # We could not stand up the gate; do NOT silently execute.
            print(
                "[BATMAN-HALT-NO-GATE] approval-gate requested but unavailable; "
                "not filing children",
                file=sys.stderr,
            )
            lifecycle.report(
                plan,
                _empty_result_reason(reason=EXEC_GATE_DISABLED),
            )
            return 0, EXEC_GATE_DISABLED
        print(
            f"[BATMAN-AWAITING-APPROVAL] parent={parent_repo}#{parent_issue_number} "
            f"channel={envelope.channel} message_ts={envelope.message_ts} "
            f"timeout_s={config.approval_timeout_s}",
            flush=True,
        )
        verdict = lifecycle.await_approval(envelope)
        if not verdict.approved:
            print(
                f"[BATMAN-APPROVAL-{verdict.verdict.upper()}] "
                f"elapsed={verdict.elapsed_s:.0f}s detail={verdict.detail!r}"
            )
            # On rejection or transport-down, clear the pending state so
            # the operator's next manual nudge can start fresh. On a plain
            # timeout (still no reaction), keep the state so the NEXT
            # firing resumes polling the same plan post without
            # re-posting.
            if verdict.verdict != "approval_timeout":
                _clear_pending_envelope(parent_repo, parent_issue_number)
                _unset_pending_approval_label(parent_repo, parent_issue_number)
            lifecycle.report(plan, _empty_result_reason(reason=verdict.verdict))
            return 0, verdict.verdict
        print(f"[BATMAN-APPROVED] elapsed={verdict.elapsed_s:.0f}s")
        # Approval landed: clear the pending state before execute so the
        # next firing doesn't think we're still waiting.
        _clear_pending_envelope(parent_repo, parent_issue_number)
        _unset_pending_approval_label(parent_repo, parent_issue_number)

    execution_plan = _execution_plan(lifecycle, plan)
    planned_marker_children = _fanout_marker_children(execution_plan)
    marker_saved = _save_completed_fanout_marker(
        parent_repo,
        parent_issue_number,
        firing_id=firing_id,
        reason="fanout-started",
        state="executing",
        children=planned_marker_children,
    )
    if not marker_saved:
        outcome = "failure-parent-fanout-marker-failed"
        lifecycle.report(execution_plan, _empty_result_reason(reason=outcome))
        return 1, outcome

    try:
        result = lifecycle.execute(execution_plan)
    except Exception:
        print(
            f"[BATMAN-FANOUT-CRASH-MARKER-KEPT] parent={parent_repo}#{parent_issue_number}",
            file=sys.stderr,
        )
        raise
    print(
        f"[BATMAN-EXECUTE-DONE] reason={result.reason} "
        f"filed={len(result.created_issue_urls)} failed={len(result.failed_repos)}"
    )
    finalization_outcome = ""
    if result.reason == EXEC_OK:
        executed_marker_children = _fanout_marker_children(result) or planned_marker_children
        completed_marker_saved = _save_completed_fanout_marker(
            parent_repo,
            parent_issue_number,
            firing_id=firing_id,
            reason=result.reason,
            state="completed",
            children=executed_marker_children,
        )
        if not completed_marker_saved:
            print(
                f"[BATMAN-COMPLETED-FANOUT-UPGRADE-WARN] "
                f"parent={parent_repo}#{parent_issue_number}",
                file=sys.stderr,
            )
        if _finalize_parent_after_child_fanout(parent_repo, parent_issue_number):
            _clear_completed_fanout_marker(parent_repo, parent_issue_number)
        else:
            finalization_outcome = "failure-parent-finalization-pending"
    else:
        _clear_completed_fanout_marker(parent_repo, parent_issue_number)
    lifecycle.report(execution_plan, result)
    if finalization_outcome:
        return 1, finalization_outcome
    return 0, result.reason


# ---------------------------------------------------------------------------
# Idempotent approval state (issue #115).
#
# A parent issue carries `agent:plan-pending-approval` while Batman is
# waiting on the operator's Slack reaction. The Slack `(channel_id,
# message_ts)` we posted lives on disk under
# `${ALFRED_HOME}/state/batman/pending-approvals/<safe-key>.json` so the
# NEXT firing can resume polling the same message instead of drafting a
# duplicate plan post.
# ---------------------------------------------------------------------------


_PENDING_APPROVAL_DIR = STATE_ROOT / "batman" / "pending-approvals"
_COMPLETED_FANOUT_DIR = STATE_ROOT / "batman" / "completed-fanouts"


def _execution_plan(lifecycle: object, plan: object) -> object:
    get_execution_plan = getattr(lifecycle, "execution_plan", None)
    if callable(get_execution_plan):
        return get_execution_plan(plan)
    return plan


def _pending_approval_path(parent_repo: str, parent_issue_number: int) -> Path:
    safe = parent_repo.replace("/", "__")
    return _PENDING_APPROVAL_DIR / f"{safe}__{parent_issue_number}.json"


def _completed_fanout_path(parent_repo: str, parent_issue_number: int) -> Path:
    safe = parent_repo.replace("/", "__")
    return _COMPLETED_FANOUT_DIR / f"{safe}__{parent_issue_number}.json"


def _fanout_marker_children(plan) -> list[dict[str, object]]:
    children = []
    for child in getattr(plan, "children", ()) or ():
        repo = str(getattr(child, "repo", "") or "").strip()
        title = str(getattr(child, "title", "") or "").strip()
        if not repo or not title:
            continue
        labels = [
            str(label).strip()
            for label in (getattr(child, "labels", ()) or ())
            if str(label).strip()
        ]
        children.append({"labels": labels, "repo": repo, "title": title})
    return children


def _save_completed_fanout_marker(
    parent_repo: str,
    parent_issue_number: int,
    *,
    firing_id: str,
    reason: str,
    state: str,
    children: list[dict[str, object]] | None = None,
) -> bool:
    import json

    if not parent_repo or parent_issue_number <= 0:
        return False
    if state not in {"executing", "completed"}:
        raise ValueError(f"unsupported completed fanout marker state: {state}")
    path = _completed_fanout_path(parent_repo, parent_issue_number)
    try:
        _COMPLETED_FANOUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[BATMAN-COMPLETED-FANOUT-SAVE-WARN] {path}: {exc}", file=sys.stderr)
        return False
    payload = {
        "firing_id": firing_id,
        "parent_issue": parent_issue_number,
        "parent_repo": parent_repo,
        "reason": reason,
        "saved_at": datetime.now(UTC).isoformat(),
        "state": state,
        "children": children or [],
    }
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        print(f"[BATMAN-COMPLETED-FANOUT-SAVE-WARN] {path}: {exc}", file=sys.stderr)
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        return False
    return True


def _completed_fanout_marker_payload(
    parent_repo: str, parent_issue_number: int
) -> dict[str, object] | None:
    import json

    if not parent_repo or parent_issue_number <= 0:
        return None
    path = _completed_fanout_path(parent_repo, parent_issue_number)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[BATMAN-COMPLETED-FANOUT-READ-WARN] {path}: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"[BATMAN-COMPLETED-FANOUT-READ-WARN] {path}: invalid payload", file=sys.stderr)
        return None
    return payload


def _completed_fanout_marker_state(parent_repo: str, parent_issue_number: int) -> str:
    payload = _completed_fanout_marker_payload(parent_repo, parent_issue_number)
    if payload is None:
        if _completed_fanout_path(parent_repo, parent_issue_number).exists():
            return "unknown"
        return ""
    state = str(payload.get("state") or "").strip()
    if state in {"executing", "completed"}:
        return state
    print(
        f"[BATMAN-COMPLETED-FANOUT-READ-WARN] "
        f"{_completed_fanout_path(parent_repo, parent_issue_number)}: unknown state {state!r}",
        file=sys.stderr,
    )
    return "unknown"


def _executing_fanout_marker_completed_remotely(parent_repo: str, parent_issue_number: int) -> bool:
    payload = _completed_fanout_marker_payload(parent_repo, parent_issue_number)
    if payload is None:
        return False
    children = payload.get("children")
    if not isinstance(children, list) or not children:
        return False
    for child in children:
        if not isinstance(child, dict):
            return False
        repo = str(child.get("repo") or "").strip()
        title = str(child.get("title") or "").strip()
        labels = [str(label).strip() for label in (child.get("labels") or []) if str(label).strip()]
        if not repo or not title:
            return False
        if not _child_issue_exists(
            repo,
            title=title,
            labels=labels,
            parent_repo=parent_repo,
            parent_issue_number=parent_issue_number,
        ):
            return False
    return True


def _executing_fanout_marker_is_stale(parent_repo: str, parent_issue_number: int) -> bool:
    payload = _completed_fanout_marker_payload(parent_repo, parent_issue_number)
    if payload is None:
        return False
    raw_saved_at = str(payload.get("saved_at") or "").strip()
    if not raw_saved_at:
        return False
    try:
        saved_at = datetime.fromisoformat(raw_saved_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - saved_at.astimezone(UTC)).total_seconds()
    return age_s >= _executing_fanout_stale_after_s()


def _executing_fanout_stale_after_s() -> int:
    raw = os.environ.get(ENV_EXECUTING_FANOUT_STALE_AFTER_S, "").strip()
    if not raw:
        return DEFAULT_EXECUTING_FANOUT_STALE_AFTER_S
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_EXECUTING_FANOUT_STALE_AFTER_S


def _child_issue_exists(
    repo: str,
    *,
    title: str,
    labels: list[str],
    parent_repo: str,
    parent_issue_number: int,
) -> bool:
    bundle_labels = [label for label in labels if label.startswith("agent:bundle:")]
    if not bundle_labels:
        return False
    cmd = [
        "gh",
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        "all",
        "--search",
        f'"{title}" in:title',
        "--json",
        "title,url,body",
        "--limit",
        "20",
    ]
    for label in bundle_labels:
        cmd.extend(["--label", label])
    rows = gh_json(cmd, default=[])
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("title") == title
        and _child_issue_body_matches_parent(
            row.get("body"),
            parent_repo=parent_repo,
            parent_issue_number=parent_issue_number,
        )
        for row in rows
    )


def _child_issue_body_matches_parent(
    body: object,
    *,
    parent_repo: str,
    parent_issue_number: int,
) -> bool:
    text = str(body or "")
    if not text:
        return False
    parent_url = f"https://github.com/{parent_repo}/issues/{parent_issue_number}"
    parent_ref = f"{parent_repo}#{parent_issue_number}"
    return parent_url in text or parent_ref in text


def _clear_completed_fanout_marker(parent_repo: str, parent_issue_number: int) -> None:
    path = _completed_fanout_path(parent_repo, parent_issue_number)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[BATMAN-COMPLETED-FANOUT-CLEAR-WARN] {path}: {exc}", file=sys.stderr)


def _has_completed_fanout_marker(parent_repo: str, parent_issue_number: int) -> bool:
    if not parent_repo or parent_issue_number <= 0:
        return False
    return _completed_fanout_path(parent_repo, parent_issue_number).exists()


def _has_pending_approval_label(parent_issue: dict) -> bool:
    """Check the parent-issue JSON (from gh search) for the pending label.

    Robust to either flat string entries or ``{"name": "..."}`` dicts -     gh's two issue-list endpoints return different shapes.
    """
    for raw in parent_issue.get("labels") or []:
        name = raw.get("name") if isinstance(raw, dict) else raw
        if name == PLAN_PENDING_APPROVAL:
            return True
    return False


def _save_pending_envelope(
    parent_repo: str,
    parent_issue_number: int,
    envelope: ApprovalEnvelope,
    *,
    firing_id: str,
) -> None:
    import json

    _PENDING_APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    path = _pending_approval_path(parent_repo, parent_issue_number)
    payload = {
        "channel_id": envelope.channel,
        "message_ts": envelope.message_ts,
        "posted_at": datetime.now(UTC).isoformat(),
        "firing_id": firing_id,
        "parent_repo": parent_repo,
        "parent_issue": parent_issue_number,
        "bundle_slug": envelope.plan.bundle_slug,
    }
    try:
        path.write_text(json.dumps(payload, sort_keys=True))
    except OSError as exc:
        print(f"[BATMAN-PENDING-SAVE-WARN] {path}: {exc}", file=sys.stderr)


def _load_pending_envelope(
    parent_repo: str,
    parent_issue_number: int,
    *,
    plan,
) -> ApprovalEnvelope | None:
    import json

    path = _pending_approval_path(parent_repo, parent_issue_number)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"[BATMAN-PENDING-LOAD-WARN] {path}: {exc}", file=sys.stderr)
        return None
    channel = data.get("channel_id") or ""
    ts = data.get("message_ts") or ""
    if not channel or not ts:
        return None
    # Aged state: re-draft so an abandoned plan post doesn't hold a
    # parent issue hostage indefinitely. Default 24h matches the
    # operator-friendly outer bound for "how long do I expect Batman
    # to wait before assuming I gave up on this plan?".
    max_age_hours = int(os.environ.get("ALFRED_BATMAN_APPROVAL_MAX_AGE_HOURS", "24"))
    try:
        posted_at = datetime.fromisoformat(data.get("posted_at") or "")
        age_h = (datetime.now(UTC) - posted_at).total_seconds() / 3600.0
        if age_h > max_age_hours:
            print(
                f"[BATMAN-PENDING-AGED-OUT] {path}: age={age_h:.1f}h > "
                f"max={max_age_hours}h; re-drafting.",
                file=sys.stderr,
            )
            return None
    except (ValueError, TypeError):
        # Malformed posted_at: treat as fresh; the firing will still
        # converge on resolution or operator action.
        pass
    return ApprovalEnvelope(channel=channel, message_ts=ts, plan=plan)


def _clear_pending_envelope(parent_repo: str, parent_issue_number: int) -> None:
    path = _pending_approval_path(parent_repo, parent_issue_number)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[BATMAN-PENDING-CLEAR-WARN] {path}: {exc}", file=sys.stderr)


def _set_pending_approval_label(parent_repo: str, parent_issue_number: int) -> None:
    try:
        gh_issue_edit(
            parent_repo,
            parent_issue_number,
            add_labels=[PLAN_PENDING_APPROVAL],
        )
    except Exception as exc:
        print(f"[BATMAN-LABEL-ADD-WARN] {PLAN_PENDING_APPROVAL}: {exc}", file=sys.stderr)


def _unset_pending_approval_label(parent_repo: str, parent_issue_number: int) -> None:
    try:
        gh_issue_edit(
            parent_repo,
            parent_issue_number,
            remove_labels=[PLAN_PENDING_APPROVAL],
        )
    except Exception as exc:
        print(f"[BATMAN-LABEL-REMOVE-WARN] {PLAN_PENDING_APPROVAL}: {exc}", file=sys.stderr)


def _finalize_parent_after_child_fanout(parent_repo: str, parent_issue_number: int) -> bool:
    """Mark a fully-fanned-out parent as done and close it best-effort.

    Batman already filed every child issue at this point. Leaving the
    parent open without a blocker lets the next firing pick the same
    `agent:large-feature` issue again and duplicate the child fan-out.
    The `agent:done` label is the durable pickup blocker; GitHub close is
    a best-effort convenience for the operator.
    """
    if not parent_repo or parent_issue_number <= 0:
        return False
    label_failure = ""
    try:
        ensure_labels(parent_repo, LIFECYCLE_LABELS)
        ok = gh_issue_edit(parent_repo, parent_issue_number, add_labels=[label_constants.DONE])
    except Exception as exc:
        ok = False
        label_failure = str(exc)
    if ok:
        print(f"[BATMAN-PARENT-DONE] parent={parent_repo}#{parent_issue_number}")
    else:
        detail = label_failure or f"could not add {label_constants.DONE}"
        print(
            f"[BATMAN-PARENT-DONE-WARN] parent={parent_repo}#{parent_issue_number}: {detail}",
            file=sys.stderr,
        )
        return False

    close_ok, detail = _close_parent_issue(parent_repo, parent_issue_number)
    if close_ok:
        print(f"[BATMAN-PARENT-CLOSED] {detail}")
        return True
    print(
        f"[BATMAN-PARENT-CLOSE-WARN] parent={parent_repo}#{parent_issue_number}: {detail}",
        file=sys.stderr,
    )
    return True


def _close_parent_issue(parent_repo: str, parent_issue_number: int) -> tuple[bool, str]:
    """Close Batman's configured parent issue without using queue allowlists."""
    if is_dry_run():
        dry_run_log("gh", f"would close Batman parent {parent_repo}#{parent_issue_number}")
        return True, f"{parent_repo}#{parent_issue_number} close simulated"
    res = run(
        ["gh", "issue", "close", str(parent_issue_number), "-R", parent_repo],
        timeout=30,
    )
    if res.returncode != 0:
        return False, (res.stderr or res.stdout or "gh issue close failed").strip()
    return True, f"{parent_repo}#{parent_issue_number} closed"


def _empty_result_reason(*, reason: str):
    """Build a no-op ``ExecuteResult`` for report-only paths."""
    from batman import ExecuteResult  # local import keeps the runner header clean

    return ExecuteResult(executed=False, reason=reason)


def main() -> int:
    if doctor_mode():
        print("[BATMAN-DOCTOR-OK]")
        return 0

    if not is_agent_enabled(CODENAME, default=False):
        print(
            f"[BATMAN-SKIP] {CODENAME} not enabled in fleet file; "
            f"run `alfred enable {CODENAME}` to opt in.",
            file=sys.stderr,
        )
        return 0

    # Pick a single parent issue from BATMAN_PARENT_REPO and run
    # plan -> approve -> execute -> report. With the scan path removed,
    # a fully-qualified parent repo is enough; GH_ORG is no longer required.
    lifecycle_config = BatmanLifecycleConfig.from_env()
    if not lifecycle_config.parent_repo:
        print("[BATMAN-NOOP] BATMAN_PARENT_REPO is not configured")
        return 0

    spec = PreflightSpec(
        agent=CODENAME,
        env_vars=["ALFRED_HOME", "WORKSPACE_ROOT"],
        bins=["gh", "git"],
        require_gh_auth=True,
    )
    try:
        preflight(spec)
    except Exception as e:
        print(f"[BATMAN-PREFLIGHT-FAIL] {e}", file=sys.stderr)
        return 0

    with_lock(CODENAME)

    parents = _list_parent_repo_large_features(lifecycle_config.parent_repo)
    parent_issue = _pick_parent_issue(parents, picker=lifecycle_config.picker)
    if parent_issue is None:
        print(
            f"[BATMAN-NOOP] no eligible {LARGE_FEATURE_LABEL} issues in "
            f"{lifecycle_config.parent_repo}"
        )
        return 0
    return _run_lifecycle(
        config=lifecycle_config,
        parent_issue=parent_issue,
        firing_id=_firing_id(),
    )


if __name__ == "__main__":
    sys.exit(main())
