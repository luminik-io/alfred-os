"""File-based go/no-go signalling for Batman plans.

Batman's plan-approval poll has a file fallback (``BATMAN_APPROVAL_MODE=file``):
it watches ``$ALFRED_HOME/batman/approvals/{issue_num}.approved`` and
``{issue_num}.rejected`` (see ``bin/batman.py`` ``wait_for_approval_file`` and
``lib/slack_approval.py``). The desktop client's in-app approve/decline writes
the exact same files so the operator can decide a plan without a Slack
round-trip and Batman picks it up identically to a Slack reaction.

This module is the single place that knows that contract. ``views.py`` calls
``write_decision`` from the decision endpoint; ``server/reader.py`` calls
``decision_for_issue`` so a decided plan reflects its state and leaves the
Needs-you queue. Keeping the path math here means the write side and the read
side can never drift from what Batman polls.

A genuine Batman go/no-go plan is saved at ``$ALFRED_HOME/batman-plans/`` as
``{issue_num}-plan.md`` (``draft_plan`` in ``bin/batman.py``). The plan id the
reader/client carries is the file stem (e.g. ``13-plan``); the issue number is
the leading integer of that stem.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

# Decisions, matching the file suffixes Batman polls. Strings, not an enum, so
# they serialise straight into JSON responses and plan status fields.
DECISION_APPROVE = "approve"
DECISION_DECLINE = "decline"

# Suffix Batman's file poll watches for each verdict.
_SUFFIX_BY_DECISION = {
    DECISION_APPROVE: ".approved",
    DECISION_DECLINE: ".rejected",
}

# Plan ids Batman writes look like ``{issue_num}-plan`` (``draft_plan``). The
# issue number is the leading run of digits.
_ISSUE_NUM_RE = re.compile(r"^(\d+)")


def approvals_dir(state_root: Path) -> Path:
    """Directory Batman's file-poll fallback watches.

    Mirrors ``ALFRED_HOME / "batman" / "approvals"`` in ``bin/batman.py``:
    the reader's ``state_root`` is ``$ALFRED_HOME/state``, so the approvals
    directory is its sibling ``batman/approvals``.
    """
    return Path(state_root).parent / "batman" / "approvals"


def issue_num_from_plan_id(plan_id: str) -> int | None:
    """Extract the GitHub issue number from a Batman plan id.

    Returns ``None`` when the id is not a Batman plan stem (e.g. a Slack
    follow-up or a compose draft), so callers can refuse to write a decision
    file that Batman would never poll for.
    """
    match = _ISSUE_NUM_RE.match(str(plan_id or "").strip())
    if not match:
        return None
    return int(match.group(1))


def decision_paths(state_root: Path, issue_num: int) -> tuple[Path, Path]:
    """``(approved_path, rejected_path)`` for one issue, in poll order."""
    base = approvals_dir(state_root)
    return base / f"{issue_num}.approved", base / f"{issue_num}.rejected"


def decision_for_issue(state_root: Path, issue_num: int) -> str | None:
    """Return the recorded decision for an issue, or ``None`` if undecided.

    Approval wins if (impossibly) both markers exist: an approved plan that was
    later rejected should not silently resurrect, and the firing only ever
    writes one. ``approved`` is checked first to match Batman's poll order.
    """
    approved, rejected = decision_paths(state_root, issue_num)
    if approved.exists():
        return DECISION_APPROVE
    if rejected.exists():
        return DECISION_DECLINE
    return None


def write_decision(
    state_root: Path,
    issue_num: int,
    decision: str,
    *,
    reason: str = "",
) -> Path:
    """Write the marker file Batman's file poll watches and return its path.

    ``approve`` writes ``{issue_num}.approved``; ``decline`` writes
    ``{issue_num}.rejected``. The reject body is read back by Batman as a short
    detail string (``wait_for_approval_file`` truncates to 300 chars), so we
    stamp it with the source and an optional operator reason. The write is
    atomic (temp file then replace) so Batman's poll never sees a half-written
    marker. The opposite-verdict marker is removed so a flipped decision does
    not leave a stale contradicting file for Batman to trip over.
    """
    if decision not in _SUFFIX_BY_DECISION:
        raise ValueError(f"unknown decision: {decision!r}")
    base = approvals_dir(state_root)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{issue_num}{_SUFFIX_BY_DECISION[decision]}"
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if decision == DECISION_DECLINE:
        body = f"{stamp} declined via Alfred client"
        if reason.strip():
            body += f": {reason.strip()}"
        body += "\n"
    else:
        body = ""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    # Clear a contradicting marker from an earlier decision on the same issue.
    other = DECISION_DECLINE if decision == DECISION_APPROVE else DECISION_APPROVE
    (base / f"{issue_num}{_SUFFIX_BY_DECISION[other]}").unlink(missing_ok=True)
    return path


__all__ = [
    "DECISION_APPROVE",
    "DECISION_DECLINE",
    "approvals_dir",
    "decision_for_issue",
    "decision_paths",
    "issue_num_from_plan_id",
    "write_decision",
]
