#!/usr/bin/env python3
"""``batman``, multi-repo feature coordinator.

Picks the oldest open ``agent:large-feature`` issue across the
configured product repos. When the issue carries an
``agent:bundle:<slug>`` label, every sibling issue sharing that label
is pulled in and the resulting bundle is treated as the atomic unit.

Bundle primitives live in ``lib/batman.py`` so the parsing /
claim-rollback / plan-shape logic stays unit-testable. This file is the
runner skeleton: preflight, find a bundle, post a plan summary, exit.
The full execution chain (worktrees + Claude invocation + cross-repo
PR chaining + operator approval gate) is intentionally NOT in alfred-os
yet. Fleets with extra coordination requirements can layer those
site-specific extensions on top.

Wiring:

  - Reads ``GH_ORG`` from the environment.
  - Reads ``BATMAN_SCAN_REPOS`` (comma-separated) for the search scope.
    Defaults to "no scan" when unset, so a fresh install with nothing
    configured is a no-op rather than a crash.
  - Posts a plan summary via the ``slack_format`` thread root when a
    bot token is configured, falling back to the legacy webhook
    ``slack_post`` otherwise.
  - Honours the fleet enable file: if ``batman`` is not enabled there,
    the runner exits early with a one-line stderr note.

Skeleton implementation of the Batman bundle workflow. Skeleton-only, the
operator can extend the per-repo execution chain to taste.
"""

from __future__ import annotations

import os
import sys
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
    GH_ORG,
    GH_REPO_TO_LOCAL,
    LIFECYCLE_LABELS,
    STATE_ROOT,
    EventLog,
    PreflightSpec,
    agent_engine,
    doctor_mode,
    ensure_labels,
    gh_issue_comment,
    gh_issue_edit,
    gh_json,
    is_agent_enabled,
    preflight,
    slack_post,
    with_lock,
)
from batman import (  # noqa: E402
    APPROVAL_MODE_FILE,
    BUNDLE_LABEL_PREFIX,
    EXEC_GATE_DISABLED,
    EXEC_NO_CHILDREN,
    LARGE_FEATURE_LABEL,
    ApprovalEnvelope,
    BatmanLifecycle,
    BatmanLifecycleConfig,
    Bundle,
    SlackReporter,
    list_issues_by_bundle_label,
    parse_plan_from_bundle,
)
from dependencies import sort_issues_by_dependencies  # noqa: E402
from labels import PLAN_PENDING_APPROVAL  # noqa: E402
from slack_format import firing_thread_root  # noqa: E402

CODENAME = os.environ.get("AGENT_CODENAME", "batman")
BATMAN_ENGINE = agent_engine(CODENAME, default="hybrid")
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


def _scan_repos() -> list[str]:
    """Comma-separated list of local repo names Batman searches.

    Empty when unset, a fresh install opts out of cross-repo
    discovery until the operator wires it explicitly. This keeps the
    skeleton runner from blasting ``gh search`` against nothing
    sensible.
    """
    raw = (os.environ.get("BATMAN_SCAN_REPOS") or "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _repo_arg_for_scan_token(token: str) -> str | None:
    """Return the ``owner/repo`` value passed to ``gh search --repo``."""
    token = token.strip()
    if not token:
        return None
    if "/" in token:
        return token
    repo_slug = next(
        (
            github_repo
            for github_repo, local_repo in GH_REPO_TO_LOCAL.items()
            if token in {github_repo, local_repo}
        ),
        token,
    )
    return f"{GH_ORG}/{repo_slug}" if GH_ORG else None


def _scan_repo_args() -> list[str]:
    repo_args = [_repo_arg_for_scan_token(repo) for repo in _scan_repos()]
    return [repo for repo in repo_args if repo]


def _has_batman_pickup_blocker(label_names: set[str] | frozenset[str]) -> bool:
    """Batman owns large-feature and bundle labels; block only hard gates."""
    labels = set(label_names)
    return bool(
        (labels & BATMAN_PICKUP_BLOCKING_LABELS) or label_constants.agent_pr_open_labels(labels)
    )


def _list_large_features() -> list[dict]:
    """Cross-repo search for open ``agent:large-feature`` issues.

    Filters out issues that look claimed (``agent:in-flight``) or
    blocked (``do-not-pickup``). Returns ``[]`` on missing GH_ORG or
    empty ``BATMAN_SCAN_REPOS`` so a half-configured fleet exits
    cleanly instead of org-wide blasting issues into the picker.

    The post-search filter (URL prefix match) keeps results scoped to
    the operator-configured repos even though ``gh search`` only takes
    ``--owner``, not ``--repo`` per call. Hits in repos outside the
    scan list are dropped silently.
    """
    if not GH_ORG:
        return []
    repo_args = _scan_repo_args()
    if not repo_args:
        return []
    cmd = ["gh", "search", "issues"]
    for repo in repo_args:
        cmd.extend(["--repo", repo])
    cmd.extend(
        [
            "--label",
            LARGE_FEATURE_LABEL,
            "--state",
            "open",
            "--json",
            "number,title,url,labels,createdAt,body",
            "--limit",
            "20",
        ]
    )
    rows = gh_json(cmd, default=[])
    if not isinstance(rows, list):
        return []
    allowed_prefixes = tuple(f"https://github.com/{repo}/" for repo in repo_args)
    eligible: list[dict] = []
    for r in rows:
        url = r.get("url") or ""
        if not url.startswith(allowed_prefixes):
            continue
        labels = {label.get("name") for label in r.get("labels", []) if isinstance(label, dict)}
        if _has_batman_pickup_blocker(labels):
            continue
        eligible.append(r)
    return eligible


def _bundle_label(issue: dict) -> str | None:
    for label in issue.get("labels", []):
        if isinstance(label, dict):
            name = label.get("name") or ""
            if name.startswith(BUNDLE_LABEL_PREFIX):
                return name
    return None


def _bundle_for_issue(issue: dict) -> Bundle:
    """Resolve the full bundle for an issue.

    If the issue carries an ``agent:bundle:<slug>`` label, every
    sibling sharing that label is pulled in. Otherwise it's a bundle
    of one, the issue itself.
    """
    label = _bundle_label(issue)
    if not label:
        return Bundle(issues=[issue], bundle_label=None)
    siblings = list_issues_by_bundle_label(label, allowed_repos=_scan_repo_args())
    by_url = {s.get("url"): s for s in siblings if s.get("url")}
    by_url[issue.get("url")] = issue  # always include the trigger issue
    return Bundle(issues=sort_issues_by_dependencies(by_url.values()), bundle_label=label)


def _firing_id() -> str:
    import secrets

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _repo_slug_from_issue_url(url: str) -> str | None:
    parts = (url or "").split("/")
    if len(parts) < 5 or parts[2] != "github.com":
        return None
    return f"{parts[3]}/{parts[4]}"


def _block_legacy_plan_for_scope_resolution(issue: dict, plan) -> None:
    """Stop the legacy plan-only path before it posts a guessed rollout."""
    repo = _repo_slug_from_issue_url(issue.get("url") or "")
    number = int(issue.get("number") or 0)
    notes = "\n".join(f"- {note}" for note in plan.parse_notes) or (
        "- The issue body is missing enough structure for Batman to plan safely."
    )
    body = (
        "Batman could not safely draft this as an execution plan yet.\n\n"
        f"{notes}\n\n"
        "Please update the issue with:\n"
        "- `Affected Repos: repo-a, repo-b` or a `## Affected Repos` section\n"
        "- `## Acceptance Criteria` with `### <repo>` subsections for each repo\n"
        "- any rollout ordering constraints if one repo must land before another\n\n"
        "I moved this to `needs:human-scope` so it will not be picked up again "
        "until the scope is explicit."
    )
    if repo and number:
        gh_issue_comment(repo, number, body)
        gh_issue_edit(repo, number, add_labels=["needs:human-scope"])
    slack_post(
        f"[BATMAN-NEEDS-SCOPE] issue #{number}: affected repos / acceptance "
        "criteria are missing, so no plan was posted. Moved to needs:human-scope.",
        severity="warn",
    )


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
    """
    # Build the lifecycle. Imports here are deferred so the lifecycle
    # module is only loaded when the new path is active; legacy fleets
    # that never set BATMAN_PARENT_REPO never pay for the optional
    # slack_approval / slack_sdk dependency.
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
    # into being. Never let an event-log hiccup break the firing.
    try:
        EventLog(agent=CODENAME, firing_id=firing_id).emit(
            "plan_created",
            issue=parent_issue_number,
            affected_repos=list(plan.affected_repos),
            bundle=plan.bundle_slug,
            children=len(plan.children),
            detail=(
                f"{parent_repo}#{parent_issue_number} "
                f"({', '.join(plan.affected_repos) or 'no repos'})"
            ),
        )
    except OSError as exc:
        # Only absorb event-log I/O failures here. An UnknownEventType /
        # EventPayloadError from agent_events is a closed-set programmer error
        # (a misspelled event name), so it must crash loudly rather than be
        # swallowed and printed.
        print(f"[BATMAN-EVENT-LOG] plan_created emit skipped: {exc}", file=sys.stderr)

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
        return 0

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
        return 0

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
            return 0
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
            return 0
        print(f"[BATMAN-APPROVED] elapsed={verdict.elapsed_s:.0f}s")
        # Approval landed: clear the pending state before execute so the
        # next firing doesn't think we're still waiting.
        _clear_pending_envelope(parent_repo, parent_issue_number)
        _unset_pending_approval_label(parent_repo, parent_issue_number)

    result = lifecycle.execute(plan)
    print(
        f"[BATMAN-EXECUTE-DONE] reason={result.reason} "
        f"filed={len(result.created_issue_urls)} failed={len(result.failed_repos)}"
    )
    lifecycle.report(plan, result)
    return 0


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


def _pending_approval_path(parent_repo: str, parent_issue_number: int) -> Path:
    safe = parent_repo.replace("/", "__")
    return _PENDING_APPROVAL_DIR / f"{safe}__{parent_issue_number}.json"


def _has_pending_approval_label(parent_issue: dict) -> bool:
    """Check the parent-issue JSON (from gh search) for the pending label.

    Robust to either flat string entries or ``{"name": "..."}`` dicts —
    gh's two issue-list endpoints return different shapes.
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

    spec = PreflightSpec(
        agent=CODENAME,
        env_vars=["ALFRED_HOME", "WORKSPACE_ROOT", "GH_ORG"],
        bins=["gh", "git"],
        require_gh_auth=True,
    )
    try:
        preflight(spec)
    except Exception as e:
        print(f"[BATMAN-PREFLIGHT-FAIL] {e}", file=sys.stderr)
        return 0

    with_lock(CODENAME)

    # New (lifecycle) path: pick a single parent issue from BATMAN_PARENT_REPO
    # and run plan -> approve -> execute -> report. The lifecycle path is
    # the one new operators should reach for; the legacy cross-repo
    # bundle scan stays for fleets that already use agent:bundle:<slug>
    # labels across multiple repos.
    lifecycle_config = BatmanLifecycleConfig.from_env()
    if lifecycle_config.parent_repo:
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

    # Legacy path: cross-repo bundle scan, plan-only output.
    issues = _list_large_features()
    if not issues:
        print("[BATMAN-NOOP] no eligible agent:large-feature issues")
        return 0
    # Oldest first.
    issues.sort(key=lambda i: i.get("createdAt", ""))
    bundle = _bundle_for_issue(issues[0])
    plan = parse_plan_from_bundle(bundle)

    firing_id = _firing_id()
    primary = bundle.primary_issue
    if plan.needs_scope_resolution:
        _block_legacy_plan_for_scope_resolution(primary, plan)
        print(
            f"[BATMAN-NEEDS-SCOPE] firing_id={firing_id} bundle={bundle.slug}; "
            "plan scope is not explicit"
        )
        return 0

    summary = (
        f"plan drafted for {bundle.slug} "
        f"({len(bundle.issues)} issue(s), {len(plan.affected_repos)} repo(s))"
    )
    body = (
        f"*Alfred plan ready* · <{primary.get('url')}|{primary.get('title')}>\n"
        f"*Bundle:* `{bundle.slug}`\n"
        f"*Scope:* {', '.join(plan.affected_repos) or '(none)'}\n"
        f"*Rollout:* {' -> '.join(plan.affected_repos) or '(default)'}\n"
        f"*Engine:* `{BATMAN_ENGINE}`\n\n"
        f"*Next step:* reply in this thread to steer the plan, or approve only if it is right.\n"
        f"*Replies Alfred understands:*\n"
        f"- `change:` adjust behavior or scope\n"
        f"- `acceptance:` add a done condition\n"
        f"- `test:` require a verification step\n"
        f"- `add repo:` or `remove repo:` change execution scope\n"
        f"- `question:` blocks execution until answered\n\n"
        f"*Approval gate:* :white_check_mark: starts this exact scope; :x: stops it.\n"
    )
    # Try the bot-token thread root first; fall back to the
    # webhook surface so the operator gets *some* visibility on
    # fleets without a bot token configured.
    handle = firing_thread_root(
        codename=CODENAME,
        firing_id=firing_id,
        summary_one_liner=summary,
        severity="info",
        body=body,
    )
    if handle is None:
        slack_post(f"[BATMAN-PLAN-DRAFTED] {summary}\n{body}", severity="info")

    print(f"[BATMAN-PLAN-DRAFTED] firing_id={firing_id} bundle={bundle.slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
