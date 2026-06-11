"""File-based go/no-go signalling for Batman plans.

Batman's plan-approval poll has a file-aware mode
(``BATMAN_APPROVAL_MODE=slack-or-file`` by default, or ``file`` for installs
without Slack): it watches
``$ALFRED_HOME/batman/approvals/{issue_num}.approved`` and
``{issue_num}.rejected`` (see ``lib.batman.wait_for_approval_file``). The
desktop client's in-app approve/decline writes the exact same files so the
operator can decide a plan without a Slack round-trip and Batman picks it up
through the real approval gate.

This module is the single place that knows that contract. ``views.py`` calls
``write_decision`` from the decision endpoint; ``lib.batman`` records the same
durable state when it consumes a marker; ``server/reader.py`` calls
``decision_for_issue`` so a decided plan reflects its state and leaves the
Needs-you queue. Keeping the path math here means the write side, read side,
and Batman's file poll can never drift.

A genuine Batman go/no-go plan is saved at ``$ALFRED_HOME/batman-plans/`` as
``{issue_num}-plan.md`` (``draft_plan`` in ``bin/batman.py``). The plan id the
reader/client carries is the file stem (e.g. ``13-plan``); the issue number is
the leading integer of that stem.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

    Mirrors ``ALFRED_HOME / "batman" / "approvals"`` in ``lib.batman``:
    the reader's ``state_root`` is ``$ALFRED_HOME/state``, so the approvals
    directory is its sibling ``batman/approvals``.
    """
    return Path(state_root).parent / "batman" / "approvals"


def decision_records_dir(state_root: Path) -> Path:
    """Directory for durable plan decision records.

    Consumable marker files are deleted after Batman observes them. The client
    still needs a stable record so a decided plan never falls back to ``draft``
    after the marker has been consumed.
    """
    return Path(state_root).parent / "batman" / "approval-decisions"


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


def decision_record_path(state_root: Path, issue_num: int) -> Path:
    """Durable decision JSON path for one issue."""
    return decision_records_dir(state_root) / f"{issue_num}.json"


def _marker_decision_for_issue(state_root: Path, issue_num: int) -> str | None:
    """Return the current consumable marker verdict, if present."""
    approved, rejected = decision_paths(state_root, issue_num)
    if approved.exists():
        return DECISION_APPROVE
    if rejected.exists():
        return DECISION_DECLINE
    return None


def recorded_decision_for_issue(state_root: Path, issue_num: int) -> str | None:
    """Return the durable decision for an issue, or ``None`` if absent."""
    path = decision_record_path(state_root, issue_num)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    decision = str(payload.get("decision") or "").strip().lower()
    return decision if decision in _SUFFIX_BY_DECISION else None


def decision_for_issue(state_root: Path, issue_num: int) -> str | None:
    """Return the recorded decision for an issue, or ``None`` if undecided.

    A live marker wins over the durable record so pending flips are reflected
    immediately. Approval wins if both markers exist, matching Batman's poll
    order.
    """
    marker = _marker_decision_for_issue(state_root, issue_num)
    if marker is not None:
        return marker
    return recorded_decision_for_issue(state_root, issue_num)


def record_decision(
    state_root: Path,
    issue_num: int,
    decision: str,
    *,
    reason: str = "",
    source: str = "Alfred client",
) -> Path:
    """Persist a non-consumable record of a Batman plan decision."""
    if decision not in _SUFFIX_BY_DECISION:
        raise ValueError(f"unknown decision: {decision!r}")
    path = decision_record_path(state_root, issue_num)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "issue_number": int(issue_num),
        "decision": decision,
        "status": "approved" if decision == DECISION_APPROVE else "declined",
        "source": source,
        "reason": reason.strip(),
        "decided_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


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
    record_decision(state_root, issue_num, decision, reason=reason, source="Alfred client")
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
    "decision_record_path",
    "decision_records_dir",
    "issue_num_from_plan_id",
    "record_decision",
    "recorded_decision_for_issue",
    "write_decision",
]
