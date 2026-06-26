"""Post fleet progress back to the Slack thread that filed an issue.

When the Slack issue bridge converts an approved planning draft into a
labeled GitHub issue (``slack_issue_bridge.py``), the originating thread
goes quiet: the fleet picks the issue up, opens a PR, CI runs, the PR
merges -- all invisible to the person who asked for the work in Slack.

This module closes that loop. It keeps a small per-thread tracker record::

    {channel, thread_ts, repo, issue_number, last_state, ...}

and a sweep (``alfred slack-thread-sync``, plus an optional idle-loop hook
in the listener) that, for each tracked thread, queries the issue and its
linked PR through ``gh`` and posts **only the delta** since the last sweep.
No new GitHub state is created here -- the module is strictly read-only on
the fleet side and write-only into the Slack thread it already owns.

SAFETY MODEL
============

* Read-only on GitHub. The only ``gh`` calls are ``issue view`` and
  ``pr list``/``pr view`` (all read verbs). It never edits a label, claims
  an issue, comments on GitHub, or runs code.
* Trust-scoped. A tracker record is only ever created from the bridge's
  own conversion path, which is already gated on a trusted user and the
  explicit-approval bridge. The sweep never reacts to arbitrary input.
* Idempotent. Each thread advances through an ordered lifecycle and a
  state is posted at most once; re-running the sweep with no GitHub change
  posts nothing.

The actual ``gh`` invocation is injected (``issue_state_fetcher``) so tests
exercise the full delta machinery without the network.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# Ordered lifecycle. A thread only ever moves forward through these states;
# the index is used to compute which states are newly reached since the last
# sweep so we never re-announce or announce out of order.
STATE_FILED = "filed"
STATE_CLAIMED = "claimed"
STATE_PR_OPEN = "pr_open"
STATE_CI_PASS = "ci_pass"
STATE_CI_FAIL = "ci_fail"
STATE_MERGED = "merged"
STATE_CLOSED = "closed"

# Terminal states: once reached, the tracker stops sweeping the thread.
_TERMINAL_STATES = frozenset({STATE_MERGED, STATE_CLOSED})

# Forward-progress ordering for the "happy path" states. ci_fail and ci_pass
# are siblings at the same rank (a PR can flip between them), handled below.
_STATE_RANK: dict[str, int] = {
    STATE_FILED: 0,
    STATE_CLAIMED: 1,
    STATE_PR_OPEN: 2,
    STATE_CI_PASS: 3,
    STATE_CI_FAIL: 3,
    STATE_MERGED: 4,
    STATE_CLOSED: 4,
}


class IssueStateFetcher(Protocol):
    """Fetches the current GitHub state for one tracked issue.

    Returns a normalized :class:`IssueProgress` (or ``None`` when the issue
    cannot be read, e.g. transient ``gh`` failure -- the sweep then leaves
    the tracker untouched and tries again next pass).
    """

    def __call__(self, *, repo: str, issue_number: int) -> IssueProgress | None: ...


@dataclass(frozen=True)
class IssueProgress:
    """Normalized read-only snapshot of an issue and its linked PR."""

    issue_state: str = "OPEN"  # OPEN / CLOSED
    claimed_by: str = ""  # codename from the latest unreleased claim comment
    pr_url: str = ""
    pr_number: int | None = None
    pr_state: str = ""  # OPEN / MERGED / CLOSED
    ci_status: str = ""  # PASS / FAIL / PENDING / ""


@dataclass
class ThreadStatusRecord:
    """Persisted per-thread tracker for fleet progress posting."""

    channel: str
    thread_ts: str
    repo: str
    issue_number: int
    issue_url: str = ""
    title: str = ""
    last_state: str = STATE_FILED
    posted_states: list[str] = field(default_factory=list)
    pr_url: str = ""
    claimed_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.last_state in _TERMINAL_STATES


def default_status_root() -> Path:
    home = (os.environ.get("ALFRED_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state" / "slack-thread-status"
    return Path.home() / ".alfred" / "state" / "slack-thread-status"


class SlackThreadStatusTracker:
    """Persist issue->thread links and post fleet progress deltas.

    The tracker owns a directory of small JSON records, one per tracked
    thread. :meth:`register_issue_thread` is called by the bridge wiring on
    conversion; :meth:`sweep` walks every active record, fetches read-only
    GitHub state, and posts the newly-reached lifecycle states to the thread.
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        poster: Any | None = None,
        fetcher: IssueStateFetcher | None = None,
    ) -> None:
        self.root = root or default_status_root()
        self.poster = poster
        self.fetcher = fetcher

    # -- registration -----------------------------------------------------

    def register_issue_thread(
        self,
        *,
        channel: str,
        thread_ts: str,
        repo: str,
        issue_number: int,
        issue_url: str = "",
        title: str = "",
    ) -> ThreadStatusRecord | None:
        """Record a thread->issue link so the sweep can post progress.

        Idempotent: a second call for the same ``(channel, thread_ts)`` keeps
        the original ``filed`` post-state but refreshes the issue metadata.
        Returns ``None`` when required identity fields are missing.
        """
        channel = (channel or "").strip()
        thread_ts = (thread_ts or "").strip()
        repo = (repo or "").strip()
        if not channel or not thread_ts or not repo or not _is_repo_slug(repo):
            return None
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            return None
        if issue_number <= 0:
            return None

        now = _utc_now()
        existing = self._load(self._path(channel, thread_ts))
        if existing is not None:
            existing.repo = repo
            existing.issue_number = issue_number
            existing.issue_url = issue_url or existing.issue_url
            existing.title = title or existing.title
            existing.updated_at = now
            self._save(existing)
            return existing

        record = ThreadStatusRecord(
            channel=channel,
            thread_ts=thread_ts,
            repo=repo,
            issue_number=issue_number,
            issue_url=issue_url,
            title=title,
            last_state=STATE_FILED,
            posted_states=[STATE_FILED],
            created_at=now,
            updated_at=now,
        )
        self._save(record)
        return record

    # -- sweep ------------------------------------------------------------

    def sweep(self, *, fetcher: IssueStateFetcher | None = None) -> list[dict[str, Any]]:
        """Advance every active tracked thread and post the delta.

        Returns one summary dict per record touched, with the list of states
        newly posted this pass (empty when nothing changed). Records in a
        terminal state are skipped. A read failure leaves the record as-is.
        """
        active_fetcher = fetcher or self.fetcher
        if active_fetcher is None:
            raise ValueError("a fetcher is required to sweep thread status")
        results: list[dict[str, Any]] = []
        for record in self._load_all():
            if record.is_terminal:
                continue
            try:
                progress = active_fetcher(repo=record.repo, issue_number=record.issue_number)
            except Exception:
                progress = None
            if progress is None:
                results.append(self._summary(record, posted=[]))
                continue
            posted = self._advance(record, progress)
            results.append(self._summary(record, posted=posted))
        return results

    def _advance(self, record: ThreadStatusRecord, progress: IssueProgress) -> list[str]:
        """Post any newly-reached states for ``record`` given ``progress``.

        Returns the ordered list of states posted this pass. Mutates and
        persists the record only when something was posted.
        """
        reached = _reached_states(record, progress)
        new_states = [state for state in reached if state not in record.posted_states]
        if not new_states:
            return []

        record.pr_url = progress.pr_url or record.pr_url
        record.claimed_by = progress.claimed_by or record.claimed_by

        posted: list[str] = []
        for state in new_states:
            text = self._render(record, state, progress)
            if not text:
                # No message body for this state (e.g. unknown); still mark
                # it reached so we never wedge the lifecycle.
                record.posted_states.append(state)
                continue
            if self._post(record, text):
                record.posted_states.append(state)
                posted.append(state)
            else:
                # Posting failed (transport). Stop here and retry next sweep
                # so ordering and at-most-once delivery are preserved.
                break

        if record.posted_states:
            record.last_state = _highest_state(record.posted_states)
        record.updated_at = _utc_now()
        self._save(record)
        return posted

    def _render(self, record: ThreadStatusRecord, state: str, progress: IssueProgress) -> str:
        return render_status_update(record, state, progress)

    def _post(self, record: ThreadStatusRecord, text: str) -> bool:
        if self.poster is None or not text.strip():
            return False
        try:
            self.poster.chat_postMessage(
                channel=record.channel,
                thread_ts=record.thread_ts,
                text=text,
            )
        except Exception:
            return False
        return True

    # -- persistence ------------------------------------------------------

    def _summary(self, record: ThreadStatusRecord, *, posted: list[str]) -> dict[str, Any]:
        return {
            "channel": record.channel,
            "thread_ts": record.thread_ts,
            "repo": record.repo,
            "issue_number": record.issue_number,
            "last_state": record.last_state,
            "posted": list(posted),
        }

    def _path(self, channel: str, thread_ts: str) -> Path:
        return self.root / f"{_safe_key(channel, thread_ts)}.json"

    def _save(self, record: ThreadStatusRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.channel, record.thread_ts)
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(
            json.dumps(asdict(record), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def _load(self, path: Path) -> ThreadStatusRecord | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return _record_from_dict(raw)

    def _load_all(self) -> list[ThreadStatusRecord]:
        if not self.root.exists():
            return []
        out: list[ThreadStatusRecord] = []
        for path in sorted(self.root.glob("*.json")):
            record = self._load(path)
            if record is not None:
                out.append(record)
        return out


# ---------------------------------------------------------------------------
# Delta computation (pure)
# ---------------------------------------------------------------------------


def _reached_states(record: ThreadStatusRecord, progress: IssueProgress) -> list[str]:
    """Return the ordered lifecycle states proven reached by ``progress``.

    Always includes ``filed`` (the issue exists by construction). Higher
    states are added only when the corresponding GitHub signal is present.
    The result is a superset that is intersected with already-posted states
    by the caller to find the delta.
    """
    states = [STATE_FILED]
    pr_state = (progress.pr_state or "").upper()
    pr_present = bool(progress.pr_url or progress.pr_number is not None or pr_state)

    if progress.claimed_by or pr_present or pr_state == "MERGED":
        states.append(STATE_CLAIMED)
    if pr_present:
        states.append(STATE_PR_OPEN)

    ci = (progress.ci_status or "").upper()
    if ci == "PASS":
        states.append(STATE_CI_PASS)
    elif ci == "FAIL":
        states.append(STATE_CI_FAIL)

    if pr_state == "MERGED":
        states.append(STATE_MERGED)
    elif (progress.issue_state or "").upper() == "CLOSED" and pr_state != "OPEN":
        states.append(STATE_CLOSED)
    return states


def _highest_state(states: Iterable[str]) -> str:
    best = STATE_FILED
    best_rank = -1
    for state in states:
        rank = _STATE_RANK.get(state, -1)
        # Prefer a terminal/merged signal, then highest rank, with ci_fail
        # losing to ci_pass at equal rank so a recovered PR reads as passing.
        if rank > best_rank or (rank == best_rank and state == STATE_CI_PASS):
            best = state
            best_rank = rank
    return best


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_status_update(
    record: ThreadStatusRecord,
    state: str,
    progress: IssueProgress,
) -> str:
    """Render the Slack message body for a newly-reached lifecycle state."""
    issue_ref = _issue_ref(record)
    if state == STATE_CLAIMED:
        who = progress.claimed_by or record.claimed_by
        who_part = f" by *{who}*" if who else ""
        return f"*Issue claimed*{who_part}\n\nThe fleet picked up {issue_ref} and is working it."
    if state == STATE_PR_OPEN:
        pr = progress.pr_url or record.pr_url
        pr_part = f"\n\n*PR:* {pr}" if pr else ""
        return f"*Pull request opened* for {issue_ref}{pr_part}"
    if state == STATE_CI_PASS:
        pr = progress.pr_url or record.pr_url
        pr_part = f" ({pr})" if pr else ""
        return f"*CI is green* on the {issue_ref} PR{pr_part}. Awaiting review and merge."
    if state == STATE_CI_FAIL:
        pr = progress.pr_url or record.pr_url
        pr_part = f" ({pr})" if pr else ""
        return (
            f"*CI is failing* on the {issue_ref} PR{pr_part}. "
            "The fleet will keep iterating; nothing merges until checks pass."
        )
    if state == STATE_MERGED:
        pr = progress.pr_url or record.pr_url
        pr_part = f"\n\n*Merged PR:* {pr}" if pr else ""
        return f"*Merged* - {issue_ref} shipped.{pr_part}"
    if state == STATE_CLOSED:
        return f"*Closed* - {issue_ref} was closed without a merged PR."
    return ""


def _issue_ref(record: ThreadStatusRecord) -> str:
    if record.issue_url:
        return record.issue_url
    return f"{record.repo}#{record.issue_number}"


# ---------------------------------------------------------------------------
# Default gh-backed fetcher (read-only)
# ---------------------------------------------------------------------------


def default_issue_state_fetcher(
    *,
    repo: str,
    issue_number: int,
    gh_json: Callable[..., Any] | None = None,
) -> IssueProgress | None:
    """Read issue + linked-PR + CI state via ``gh`` (read-only).

    Uses ``gh issue view`` for the issue/claim state and ``gh pr list`` to
    find the agent-authored PR that references the issue, then ``gh pr view``
    for the merge + checks rollup. Returns ``None`` on any read failure so
    the sweep leaves the tracker untouched and retries later.
    """
    runner = gh_json or _default_gh_json
    issue = runner(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "-R",
            repo,
            "--json",
            "state,labels,comments,number",
        ],
        default=None,
    )
    if not isinstance(issue, dict):
        return None

    issue_state = str(issue.get("state") or "OPEN").upper()
    claimed_by = _latest_claimant(issue.get("comments") or [])

    pr = _find_linked_pr(repo, issue_number, runner)
    if pr is None:
        return IssueProgress(
            issue_state=issue_state,
            claimed_by=claimed_by,
        )

    pr_state = str(pr.get("state") or "").upper()
    return IssueProgress(
        issue_state=issue_state,
        claimed_by=claimed_by,
        pr_url=str(pr.get("url") or ""),
        pr_number=_safe_int(pr.get("number")),
        pr_state=pr_state,
        ci_status=_ci_status_from_pr(pr),
    )


def _find_linked_pr(repo: str, issue_number: int, runner: Callable[..., Any]) -> dict | None:
    """Return the most relevant PR referencing ``#issue_number`` in ``repo``.

    Searches both open and merged/closed PRs so a thread still reports the
    final ``merged`` state even after the PR closed. Re-validates the exact
    issue token to avoid ``#12`` matching ``#1234``.
    """
    prs = runner(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "all",
            "--search",
            f'"#{issue_number}" in:title,body',
            "--json",
            "number,url,state,title,body,statusCheckRollup",
            "--limit",
            "20",
        ],
        default=[],
    )
    if not isinstance(prs, list):
        return None
    candidates = [pr for pr in prs if _pr_references_issue(pr, issue_number)]
    if not candidates:
        return None
    # Prefer a merged PR, then an open one, then the most recent by number.
    merged = [pr for pr in candidates if str(pr.get("state") or "").upper() == "MERGED"]
    if merged:
        return max(merged, key=lambda pr: _safe_int(pr.get("number")) or 0)
    open_prs = [pr for pr in candidates if str(pr.get("state") or "").upper() == "OPEN"]
    if open_prs:
        return max(open_prs, key=lambda pr: _safe_int(pr.get("number")) or 0)
    return max(candidates, key=lambda pr: _safe_int(pr.get("number")) or 0)


def _pr_references_issue(pr: dict, issue_number: int) -> bool:
    token = f"#{issue_number}"
    haystack = f" {pr.get('title', '')} {pr.get('body', '') or ''} "
    idx = haystack.find(token)
    while idx >= 0:
        after = haystack[idx + len(token) : idx + len(token) + 1]
        if not after.isdigit():
            return True
        idx = haystack.find(token, idx + 1)
    return False


def _ci_status_from_pr(pr: dict) -> str:
    """Summarize the PR's check rollup to PASS / FAIL / PENDING / ""."""
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return ""
    saw_pending = False
    saw_fail = False
    saw_any = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        saw_any = True
        # gh returns either a CheckRun (status/conclusion) or a
        # StatusContext (state). Normalize both.
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or "").upper()
        if status and status != "COMPLETED":
            saw_pending = True
            continue
        verdict = conclusion or state
        if verdict in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            continue
        if verdict in {"PENDING", "EXPECTED", "", "QUEUED", "IN_PROGRESS"}:
            saw_pending = True
            continue
        # FAILURE / ERROR / CANCELLED / TIMED_OUT / ACTION_REQUIRED / etc.
        saw_fail = True
    if not saw_any:
        return ""
    if saw_fail:
        return "FAIL"
    if saw_pending:
        return "PENDING"
    return "PASS"


def _latest_claimant(comments: list) -> str:
    """Return the codename of the latest unreleased claim, if any.

    Parses the same ``<!-- agent-claim: codename=X firing_id=Y ... -->``
    comment trail the fleet's claim/release state machine writes. A claim is
    "unreleased" when no later release comment names the same firing id.
    """
    claim_prefix = "<!-- agent-claim:"
    release_prefix = "<!-- agent-release:"
    released: set[str] = set()
    claims: list[tuple[str, str]] = []  # (codename, firing_id)
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = str(comment.get("body") or "").strip()
        if body.startswith(claim_prefix):
            meta = _parse_claim_kv(body, claim_prefix)
            claims.append((meta.get("codename", ""), meta.get("firing_id", "")))
        elif body.startswith(release_prefix):
            meta = _parse_claim_kv(body, release_prefix)
            released.add(meta.get("firing_id", ""))
    for codename, firing_id in reversed(claims):
        if firing_id not in released and codename:
            return codename
    return ""


def _parse_claim_kv(body: str, prefix: str) -> dict[str, str]:
    payload = body.strip()
    if payload.startswith(prefix):
        payload = payload[len(prefix) :]
    if payload.endswith("-->"):
        payload = payload[:-3]
    out: dict[str, str] = {}
    for part in payload.split():
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _default_gh_json(cmd: list[str], default: Any = None) -> Any:
    """Lazy bridge to ``agent_runner.process.gh_json`` (read-only ``gh``)."""
    try:
        from agent_runner.process import gh_json
    except Exception:
        return default
    return gh_json(cmd, default=default)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_from_dict(raw: dict[str, Any]) -> ThreadStatusRecord | None:
    if not isinstance(raw, dict):
        return None
    issue_number = _safe_int(raw.get("issue_number"))
    if issue_number is None:
        return None
    posted = raw.get("posted_states")
    posted_states = [str(item) for item in posted] if isinstance(posted, list) else []
    return ThreadStatusRecord(
        channel=str(raw.get("channel") or ""),
        thread_ts=str(raw.get("thread_ts") or ""),
        repo=str(raw.get("repo") or ""),
        issue_number=issue_number,
        issue_url=str(raw.get("issue_url") or ""),
        title=str(raw.get("title") or ""),
        last_state=str(raw.get("last_state") or STATE_FILED),
        posted_states=posted_states or [STATE_FILED],
        pr_url=str(raw.get("pr_url") or ""),
        claimed_by=str(raw.get("claimed_by") or ""),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _is_repo_slug(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.strip()))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_key(channel: str, thread_ts: str) -> str:
    raw = f"{channel}-{thread_ts}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "thread"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "STATE_CI_FAIL",
    "STATE_CI_PASS",
    "STATE_CLAIMED",
    "STATE_CLOSED",
    "STATE_FILED",
    "STATE_MERGED",
    "STATE_PR_OPEN",
    "IssueProgress",
    "IssueStateFetcher",
    "SlackThreadStatusTracker",
    "ThreadStatusRecord",
    "default_issue_state_fetcher",
    "default_status_root",
    "render_status_update",
]
