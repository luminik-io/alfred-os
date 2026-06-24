"""Distill a firing's raw event stream into an honest, scannable timeline.

The fleet brain already emits a rich per-firing event stream (``firing_started``,
``repo_picked`` / ``pr_picked``, ``llm_fallback``, ``llm_invoke_done`` /
``claude_invoke_done`` with engine + turns + subtype, ``review_posted``,
``pr_opened``, ``firing_complete`` with an ``outcome``). The Activity view used to
render only ``firing_started`` and ``firing_complete``, which told the operator
nothing. This module turns the same captured events into:

* a single **headline** the run can be summarised by (e.g. "reviewed PR #1051 -
  2 findings", "opened PR #1048", "idle - no work"),
* a **severity** (``ok`` / ``idle`` / ``error``) so the UI can stay quiet for
  no-work runs and shout for failures,
* an honest **error** classification (``authentication`` / ``rate_limit`` /
  ``timeout`` / ``budget`` / ``overloaded`` / ``failed``) sourced from the run's
  terminal ``outcome`` and the engine ``subtype`` rather than the downstream
  fallback reason string, and
* an ordered list of **steps** (pr picked, engine + turns, fallback, PR opened,
  reviewed) for the expandable detail view.

Honest-error contract (aligned with ``lib/agent_runner/result.py``): when Claude
hits an auth failure the engine classifies the subtype as
``error_authentication`` even though the raw provider text it then falls back on
can read like a rate limit. We therefore derive the run's error from the
terminal ``outcome`` / ``subtype`` (``llm-error_authentication`` ->
``authentication``), NOT from the free-text ``reason`` on the ``llm_fallback``
event. A fallback that was triggered by an auth failure reads as
"authentication", never the misleading downstream "rate_limit".

The module is pure and dependency-free so it is cheap to unit-test against
hand-written event lists, and it is the single source of truth shared by the
desktop client (which renders ``headline`` / ``severity`` / ``error`` / ``steps``
verbatim) and any future surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public shapes
# ---------------------------------------------------------------------------

# Step tone drives the dot colour in the client. ``error`` is loud, ``warn`` is
# a fallback / degraded path, ``ok`` is a real success milestone, ``muted`` is
# routine scaffolding (worktree created, engine invoked) the operator can skim.
StepTone = str  # "ok" | "warn" | "error" | "muted"

# Run severity drives whether the row is quiet (idle) or shouts (error).
Severity = str  # "ok" | "idle" | "error"


@dataclass(frozen=True)
class TimelineStep:
    """One human-readable line in the expanded run timeline."""

    kind: str
    label: str
    detail: str
    tone: StepTone
    ts: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "detail": self.detail,
            "tone": self.tone,
            "ts": self.ts,
        }


@dataclass(frozen=True)
class FiringTimeline:
    """The distilled, render-ready view of one firing."""

    headline: str
    severity: Severity
    # An honest classified error cause when the run failed, else ``None``. One
    # of: authentication, rate_limit, budget, overloaded, timeout, failed.
    error: str | None
    # The raw terminal ``outcome`` string (e.g. "pr-opened", "idle-no-pr",
    # "llm-error_authentication") for operators who want the literal value.
    outcome: str | None
    steps: list[TimelineStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "severity": self.severity,
            "error": self.error,
            "outcome": self.outcome,
            "steps": [step.to_dict() for step in self.steps],
        }


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Map an engine subtype (as carried on ``firing_complete.outcome`` =
# ``llm-<subtype>`` / ``failure-<subtype>`` and on ``llm_invoke_done.subtype``)
# to an honest, operator-facing error cause. These mirror the subtypes
# ``lib/agent_runner/result.py`` pins, so an auth failure that the provider then
# dresses up as a rate limit still reads as authentication here.
_SUBTYPE_TO_CAUSE: dict[str, str] = {
    "error_authentication": "authentication",
    "error_budget": "budget",
    "error_rate_limit": "rate_limit",
    "error_overloaded": "overloaded",
    "error_max_turns": "max_turns",
    "error_timeout": "timeout",
    "error_api": "api_error",
}

# Terminal ``outcome`` strings that are honest failures even though the firing
# "completed". Anything matching one of these prefixes/values is an error run.
_FAILURE_OUTCOME_PREFIXES: tuple[str, ...] = (
    "llm-error",
    "failure-",
    "blocked-",
    "partial-",
)
_FAILURE_OUTCOME_VALUES: frozenset[str] = frozenset(
    {
        "pr-create-failed",
        "post-failed",
        "no-commit",
        "worktree-error",
        "worktree-failed",
        "workflow-validation-failed",
        "pre-push-checks-failed",
        "monitoring-fetch-failed",
        "sentry-fetch-failed",
        "parse-error",
        "bad-triages-type",
        "regression",
        "paused_fail_streak",
        "rate-limit",
        "max-turns",
        "turn-cap",
        "global-blocked",
        "all-repos-paused",
        "daily-cap",
    }
)

# Terminal ``outcome`` strings that are routine "nothing to do" runs. These keep
# the row quiet: no error, low visual weight.
_IDLE_OUTCOME_VALUES: frozenset[str] = frozenset(
    {
        "noop",
        "idle-no-pr",
        "idle-no-comments",
        "idle-no-candidates",
        "silent-well-covered",
        "silent_no_work",
        "dedup_skip",
        "already-implemented",
        "empty-diff",
        "diff-too-large",
        "pr-stale",
        "review-cap",
        "triage-cap",
    }
)

# Outcome -> the cause to surface when the outcome itself names a cap/limit
# rather than carrying an ``llm-<subtype>`` tail.
_OUTCOME_DIRECT_CAUSE: dict[str, str] = {
    "rate-limit": "rate_limit",
    "global-blocked": "rate_limit",
    "all-repos-paused": "rate_limit",
    "daily-cap": "budget",
    "max-turns": "max_turns",
    "turn-cap": "max_turns",
    "paused_fail_streak": "failed",
    "pre-push-checks-failed": "checks_failed",
    "workflow-validation-failed": "validation_failed",
}

_LLM_OUTCOME_RE = re.compile(r"^(?:llm|failure)-(error_[a-z_]+)$")

# How an error cause reads in the headline and step labels.
_CAUSE_LABEL: dict[str, str] = {
    "authentication": "authentication",
    "budget": "usage budget",
    "rate_limit": "rate limit",
    "overloaded": "provider overloaded",
    "max_turns": "max turns",
    "timeout": "timeout",
    "api_error": "API error",
    "checks_failed": "pre-push checks",
    "validation_failed": "workflow validation",
    "failed": "failed",
}


def classify_outcome_error(outcome: str | None, *, invoke_subtype: str | None = None) -> str | None:
    """Return an honest error cause for a terminal ``outcome``, or ``None``.

    The terminal outcome wins. ``invoke_subtype`` (from ``llm_invoke_done`` /
    ``claude_invoke_done``) is the fallback when the outcome is a generic
    ``failure-*`` without a subtype tail. We never read the ``llm_fallback``
    reason here: that is the downstream provider text, which can mislabel an
    auth failure as a rate limit.
    """
    if outcome:
        match = _LLM_OUTCOME_RE.match(outcome)
        if match:
            return _SUBTYPE_TO_CAUSE.get(match.group(1), "failed")
        if outcome in _OUTCOME_DIRECT_CAUSE:
            return _OUTCOME_DIRECT_CAUSE[outcome]
        if outcome in _IDLE_OUTCOME_VALUES:
            return None
        if outcome in _FAILURE_OUTCOME_VALUES or outcome.startswith(_FAILURE_OUTCOME_PREFIXES):
            # A real failure whose cause we cannot name more precisely.
            if invoke_subtype and invoke_subtype in _SUBTYPE_TO_CAUSE:
                return _SUBTYPE_TO_CAUSE[invoke_subtype]
            return "failed"
    if invoke_subtype and invoke_subtype in _SUBTYPE_TO_CAUSE:
        return _SUBTYPE_TO_CAUSE[invoke_subtype]
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name(event: dict) -> str:
    return str(event.get("event") or event.get("type") or "")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _repo_short(repo: Any) -> str | None:
    text = str(repo or "").strip()
    if not text:
        return None
    # "org/name" -> "name"; bare "name" stays.
    return text.rsplit("/", 1)[-1] or text


def _pr_ref(event: dict) -> str | None:
    number = event.get("number")
    n = _int(number)
    if n is not None:
        return f"PR #{n}"
    url = str(event.get("url") or "")
    match = re.search(r"/pull/(\d+)", url)
    if match:
        return f"PR #{match.group(1)}"
    return None


def _findings_phrase(p0: int | None, p1: int | None) -> str:
    total = (p0 or 0) + (p1 or 0)
    if total <= 0:
        return "no blocking findings"
    plural = "finding" if total == 1 else "findings"
    return f"{total} {plural}"


# ---------------------------------------------------------------------------
# Step builders (event name -> a render-ready step, or None to skip)
# ---------------------------------------------------------------------------


def _build_steps(events: list[dict], error_cause: str | None) -> list[TimelineStep]:
    steps: list[TimelineStep] = []
    for event in events:
        name = _name(event)
        ts = event.get("ts") if isinstance(event.get("ts"), str) else None

        if name == "firing_started":
            steps.append(TimelineStep("started", "Run started", "", "muted", ts))
        elif name in {"repo_picked", "issue_picked", "pr_picked"}:
            repo = _repo_short(event.get("repo"))
            pr = _pr_ref(event)
            if name == "pr_picked":
                comments = _int(event.get("comment_count"))
                detail = repo or ""
                if comments is not None:
                    detail = f"{detail} ({comments} comments)".strip()
                steps.append(TimelineStep("picked", f"Picked {pr or 'a PR'}", detail, "ok", ts))
            elif name == "issue_picked":
                num = _int(event.get("number"))
                label = f"Picked issue #{num}" if num is not None else "Picked an issue"
                steps.append(TimelineStep("picked", label, repo or "", "ok", ts))
            else:
                steps.append(TimelineStep("picked", "Picked repo", repo or "", "ok", ts))
        elif name == "worktree_created":
            branch = str(event.get("branch") or "").strip()
            steps.append(TimelineStep("worktree", "Worktree created", branch, "muted", ts))
        elif name == "llm_fallback":
            from_engine = str(event.get("from_engine") or "claude")
            to_engine = str(event.get("to_engine") or "codex")
            # Honest cause, not the downstream provider reason string.
            cause = _CAUSE_LABEL.get(error_cause or "") if error_cause else None
            detail = f"{from_engine} -> {to_engine}"
            if cause:
                detail = f"{detail} after {cause}"
            steps.append(TimelineStep("fallback", "Engine fallback", detail, "warn", ts))
        elif name in {"llm_invoke_done", "claude_invoke_done"}:
            engine = str(event.get("engine") or "claude")
            turns = _int(event.get("turns"))
            subtype = str(event.get("subtype") or "")
            success = event.get("success")
            detail = engine
            if turns is not None:
                detail = f"{engine} · {turns} turn{'' if turns == 1 else 's'}"
            tone: StepTone = "ok"
            label = "Engine finished"
            if success is False or subtype.startswith("error"):
                tone = "error"
                label = "Engine failed"
                if subtype:
                    detail = f"{detail} · {subtype}"
            steps.append(TimelineStep("engine", label, detail, tone, ts))
        elif name == "pre_push_checks_passed":
            steps.append(TimelineStep("checks", "Pre-push checks passed", "", "ok", ts))
        elif name == "branch_pushed":
            steps.append(TimelineStep("pushed", "Branch pushed", "", "ok", ts))
        elif name == "fix_pushed":
            sha = str(event.get("commit_sha") or "").strip()
            reviewer = str(event.get("reviewer") or "").strip()
            detail = " ".join(p for p in (sha, f"({reviewer})" if reviewer else "") if p)
            steps.append(TimelineStep("fix", "Fix pushed", detail, "ok", ts))
        elif name == "pr_opened":
            pr = _pr_ref(event)
            repo = _repo_short(event.get("repo"))
            steps.append(TimelineStep("pr", f"Opened {pr or 'a PR'}", repo or "", "ok", ts))
        elif name == "review_posted":
            pr = _pr_ref(event)
            phrase = _findings_phrase(_int(event.get("p0_count")), _int(event.get("p1_count")))
            steps.append(TimelineStep("review", f"Reviewed {pr or 'a PR'}", phrase, "ok", ts))
        elif name == "triaged":
            count = _int(event.get("count"))
            detail = f"{count} item{'' if count == 1 else 's'}" if count is not None else ""
            steps.append(TimelineStep("triage", "Triaged issues", detail, "ok", ts))
        elif name == "firing_complete":
            outcome = str(event.get("outcome") or "")
            if error_cause:
                label = f"Failed · {_CAUSE_LABEL.get(error_cause, error_cause)}"
                steps.append(TimelineStep("complete", label, outcome, "error", ts))
            else:
                steps.append(TimelineStep("complete", "Run complete", outcome, "muted", ts))
    return steps


# ---------------------------------------------------------------------------
# Headline derivation
# ---------------------------------------------------------------------------


def _headline(events: list[dict], outcome: str | None, error_cause: str | None) -> str:
    """A single scannable sentence for the collapsed row."""
    # Keep every event of a kind, in order, rather than last-write-wins: a
    # single firing can open or review more than one PR, and collapsing to the
    # last event would hide the earlier (often higher-signal) milestone.
    by_name: dict[str, list[dict]] = {}
    for event in events:
        by_name.setdefault(_name(event), []).append(event)

    def first(name: str) -> dict | None:
        items = by_name.get(name)
        return items[0] if items else None

    if error_cause:
        label = _CAUSE_LABEL.get(error_cause, error_cause)
        # Anchor the failure to what it was working on, when we know.
        pr = None
        for key in ("pr_opened", "pr_picked", "review_posted"):
            event = first(key)
            if event is not None:
                pr = _pr_ref(event)
                if pr:
                    break
        if pr:
            return f"Failed on {pr} · {label}"
        return f"Failed · {label}"

    opened = by_name.get("pr_opened", [])
    if opened:
        if len(opened) > 1:
            return f"Opened {len(opened)} PRs"
        return f"Opened {_pr_ref(opened[0]) or 'a PR'}"
    reviews = by_name.get("review_posted", [])
    if reviews:
        # Sum findings across every review so a clean second review never masks
        # blocking findings from the first.
        p0 = sum(_int(ev.get("p0_count")) or 0 for ev in reviews)
        p1 = sum(_int(ev.get("p1_count")) or 0 for ev in reviews)
        phrase = _findings_phrase(p0, p1)
        if len(reviews) > 1:
            return f"Reviewed {len(reviews)} PRs · {phrase}"
        return f"Reviewed {_pr_ref(reviews[0]) or 'a PR'} · {phrase}"
    if "fix_pushed" in by_name:
        return "Pushed a review fix"
    if "triaged" in by_name:
        count = _int((first("triaged") or {}).get("count"))
        return (
            f"Triaged {count} issue{'' if count == 1 else 's'}"
            if count is not None
            else "Triaged issues"
        )

    if outcome in _IDLE_OUTCOME_VALUES:
        return "Idle · no work"
    if outcome:
        # A completed run we have no richer milestone for: show the outcome
        # in plain words rather than a raw token.
        return outcome.replace("-", " ").replace("_", " ").capitalize()

    # No terminal event yet (running) or an unparseable run.
    if "firing_started" in by_name and "firing_complete" not in by_name:
        return "Running"
    return "No summary captured"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def derive_timeline(events: list[dict]) -> FiringTimeline:
    """Distill ``events`` (raw JSONL dicts, in order) into a render-ready timeline."""
    safe_events = [e for e in events if isinstance(e, dict)]

    outcome: str | None = None
    invoke_subtype: str | None = None
    for event in safe_events:
        name = _name(event)
        if name == "firing_complete":
            outcome = str(event.get("outcome") or "") or outcome
        elif name in {"llm_invoke_done", "claude_invoke_done"}:
            sub = str(event.get("subtype") or "")
            if sub:
                invoke_subtype = sub

    error_cause = classify_outcome_error(outcome, invoke_subtype=invoke_subtype)

    if error_cause:
        severity: Severity = "error"
    elif outcome in _IDLE_OUTCOME_VALUES:
        severity = "idle"
    elif outcome:
        severity = "ok"
    else:
        severity = "idle"

    headline = _headline(safe_events, outcome, error_cause)
    steps = _build_steps(safe_events, error_cause)

    return FiringTimeline(
        headline=headline,
        severity=severity,
        error=error_cause,
        outcome=outcome or None,
        steps=steps,
    )


__all__ = [
    "FiringTimeline",
    "TimelineStep",
    "classify_outcome_error",
    "derive_timeline",
]
