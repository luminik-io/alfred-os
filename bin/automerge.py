#!/usr/bin/env python3
"""automerge - auto-squash agent:authored PRs that are clean.

Criteria for merge:
1. Label includes 'agent:authored'.
2. PR is at least MIN_AGE_SECONDS old (window for human intercept).
3. CI status is success (or no CI configured).
4. Most recent review-agent comment ends with 'Ship-ready: yes'.
5. No unresolved P0 reviewer comments.
6. No CHANGES_REQUESTED human review.
7. No UNRESOLVED review threads from known reviewers - every bot/agent/human
   inline thread must be marked Resolved before automerge takes it.

Out of scope: any PR not labeled agent:authored. Human PRs untouched.

Configuration:
  ALFRED_AUTOMERGE_REPOS     comma-separated repo slugs to watch
  ALFRED_AUTOMERGE_REVIEW_AGENT  codename of review agent (default: rasalghul)
                                 - PR comments starting with "<Codename> - review"
                                 are checked for "Ship-ready: yes"
  ALFRED_AUTOMERGE_FIX_AGENT codename of fix agent (default: nightwing)
                                 - replies starting with "<Codename>: fixed in ..."
                                 mark a P0 comment as resolved
  ALFRED_AUTOMERGE_MIN_AGE_MIN  minimum PR age before auto-merge (default: 30)
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    GH_ORG,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    doctor_mode,
    gh_issue_edit,
    gh_json,
    is_repo_paused,
    preflight,
    run,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "automerge")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

REVIEW_AGENT = os.environ.get("ALFRED_AUTOMERGE_REVIEW_AGENT", "rasalghul").title()
FIX_AGENT = os.environ.get("ALFRED_AUTOMERGE_FIX_AGENT", "nightwing").title()

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["gh"],
    require_gh_auth=True,
)
WATCH_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_AUTOMERGE_REPOS", "").split(",") if r.strip()
]
MIN_AGE_SECONDS = int(os.environ.get("ALFRED_AUTOMERGE_MIN_AGE_MIN", "30")) * 60

P0_KEYWORDS = re.compile(r"\b(P0|blocking|critical|🛑|⛔)", re.IGNORECASE)
SHIP_READY_YES = re.compile(r"^Ship-ready:\s*yes\b", re.IGNORECASE | re.MULTILINE)
REVIEWED_HEAD_SHA = re.compile(
    r"^Reviewed-head-sha:\s*([0-9a-f]{7,40})\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Bot reviewer logins whose unresolved review threads block merge.
KNOWN_REVIEWER_LOGINS_LOWER = {
    "coderabbitai[bot]",
    "coderabbitai",
    "chatgpt-codex-connector[bot]",
    "chatgpt-codex-connector",
}
KNOWN_REVIEWER_LOGIN_SUBSTRINGS = ("codex", "chatgpt")

REVIEW_HEADER = f"{REVIEW_AGENT} - review"
REVIEW_P0_PREFIX = f"{REVIEW_AGENT} P0:"
REVIEW_P1_PREFIX = f"{REVIEW_AGENT} P1:"
FIX_REPLY_RE = re.compile(rf"{FIX_AGENT}.*fixed in", re.IGNORECASE)


def _is_reviewer_login(login: str, pr_author: str) -> bool:
    if not login:
        return False
    low = login.lower()
    if low == (pr_author or "").lower():
        return False  # PR author replying to themselves doesn't count
    if low in KNOWN_REVIEWER_LOGINS_LOWER:
        return True
    for sub in KNOWN_REVIEWER_LOGIN_SUBSTRINGS:
        if sub in low:
            return True
    if low.endswith("[bot]"):
        return True
    return True


def unresolved_reviewer_threads(repo: str, pr_num: int, pr_author: str) -> list[str]:
    """Return human-readable summaries of unresolved review threads.

    Uses GraphQL because the REST inline-comments endpoint has no
    isResolved field. Returns [] when every reviewer thread is resolved.
    """
    query = (
        "query($owner:String!,$name:String!,$num:Int!){"
        " repository(owner:$owner,name:$name){"
        "  pullRequest(number:$num){"
        "   reviewThreads(first:100){"
        "    nodes{isResolved isOutdated path "
        "     comments(first:5){nodes{author{login} body}}"
        "    }"
        "   }"
        "  }"
        " }"
        "}"
    )
    threads = gh_json(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={GH_ORG}",
            "-F",
            f"name={repo}",
            "-F",
            f"num={pr_num}",
            "--jq",
            ".data.repository.pullRequest.reviewThreads.nodes",
        ],
        default=[],
    )
    blocking: list[str] = []
    for t in threads or []:
        if t.get("isResolved"):
            continue
        comments = ((t.get("comments") or {}).get("nodes")) or []
        if not comments:
            continue
        first = comments[0]
        login = ((first.get("author") or {}).get("login")) or ""
        if not _is_reviewer_login(login, pr_author):
            continue
        body = (first.get("body") or "").strip().splitlines()
        snippet = body[0][:120] if body else ""
        path = t.get("path") or "?"
        blocking.append(f"{login} @ {path}: {snippet}")
    return blocking


_CLOSES_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#(\d+)")


def linked_issue_numbers(pr: dict) -> list[int]:
    """Pull issue numbers referenced via Closes/Fixes/Resolves in the PR title or body."""
    text = (pr.get("title") or "") + "\n" + (pr.get("body") or "")
    return [int(m) for m in _CLOSES_RE.findall(text)]


def _parse_github_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def reviewed_head_sha(review_body: str) -> str | None:
    match = REVIEWED_HEAD_SHA.search(review_body or "")
    return match.group(1).lower() if match else None


def current_pr_head_oid(repo: str, pr_num: int) -> str | None:
    view = gh_json(
        ["gh", "pr", "view", str(pr_num), "-R", f"{GH_ORG}/{repo}", "--json", "headRefOid"],
        default={},
    )
    head = view.get("headRefOid") if isinstance(view, dict) else None
    return str(head).strip().lower() if head else None


def latest_commit_timestamp(repo: str, pr_num: int) -> datetime | None:
    view = gh_json(
        ["gh", "pr", "view", str(pr_num), "-R", f"{GH_ORG}/{repo}", "--json", "commits"],
        default={},
    )
    commits = view.get("commits", []) if isinstance(view, dict) else []
    if not commits:
        return None
    latest: datetime | None = None
    for commit in commits:
        ts = _parse_github_timestamp(commit.get("committedDate") or commit.get("authoredDate"))
        if ts and (latest is None or ts > latest):
            latest = ts
    return latest


def candidates() -> list[tuple[str, dict]]:
    out = []
    for repo in WATCH_REPOS:
        if is_repo_paused(repo):
            continue
        prs = gh_json(
            [
                "gh",
                "pr",
                "list",
                "-R",
                f"{GH_ORG}/{repo}",
                "--state",
                "open",
                "--label",
                "agent:authored",
                "--json",
                "number,title,body,createdAt,reviewDecision,statusCheckRollup,labels,isDraft,author",
                "--limit",
                "30",
            ],
            default=[],
        )
        for pr in prs:
            if pr.get("isDraft"):
                continue
            if pr.get("reviewDecision") == "CHANGES_REQUESTED":
                continue
            label_names = [lbl["name"] for lbl in pr.get("labels", [])]
            if "do-not-review" in label_names or "do-not-merge" in label_names:
                continue
            try:
                created = datetime.strptime(pr["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
                age = (datetime.now(UTC) - created).total_seconds()
            except ValueError:
                continue
            if age < MIN_AGE_SECONDS:
                continue
            checks = pr.get("statusCheckRollup", []) or []
            ci_red = any(
                (c.get("conclusion") or "").upper()
                in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
                for c in checks
            )
            if ci_red:
                continue
            ci_pending = any(
                (c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
                for c in checks
            )
            if ci_pending:
                continue
            out.append((repo, pr))
    return out


def is_mergeable(
    repo: str,
    pr_num: int,
    pr_author: str = "",
    latest_commit_at: datetime | None = None,
    head_oid: str | None = None,
) -> tuple[bool, str]:
    """Verify Ship-ready=yes from latest review, no unresolved P0,
    and no unresolved reviewer threads."""
    unresolved = unresolved_reviewer_threads(repo, pr_num, pr_author)
    if unresolved:
        first = unresolved[0]
        more = f" (+{len(unresolved) - 1} more)" if len(unresolved) > 1 else ""
        return False, f"unresolved review thread - {first}{more}"

    issue_comments = gh_json(
        [
            "gh",
            "api",
            f"/repos/{GH_ORG}/{repo}/issues/{pr_num}/comments",
            "--paginate",
        ],
        default=[],
    )
    inline_comments = gh_json(
        [
            "gh",
            "api",
            f"/repos/{GH_ORG}/{repo}/pulls/{pr_num}/comments",
            "--paginate",
        ],
        default=[],
    )

    # Latest review-agent main review (NOT the P0/P1 split sub-comments)
    review_main = [c for c in issue_comments if c.get("body", "").startswith(REVIEW_HEADER)]
    if not review_main:
        return False, f"no {REVIEW_AGENT} review yet"
    latest = review_main[-1]
    if not SHIP_READY_YES.search(latest.get("body", "")):
        return False, f"{REVIEW_AGENT} did not say Ship-ready: yes"
    reviewed_sha = reviewed_head_sha(latest.get("body", ""))
    current_head = (head_oid or current_pr_head_oid(repo, pr_num) or "").lower()
    if not current_head:
        return False, "could not verify current PR head SHA"
    if not reviewed_sha:
        return False, f"{REVIEW_AGENT} review did not record head SHA"
    if reviewed_sha != current_head:
        return False, "ship-ready review is for an older PR head"

    review_created = _parse_github_timestamp(latest.get("created_at") or latest.get("createdAt"))
    commit_created = latest_commit_at or latest_commit_timestamp(repo, pr_num)
    if commit_created is None:
        return False, "could not verify latest commit timestamp"
    if review_created is None:
        return False, "could not verify review timestamp"
    if review_created < commit_created:
        return False, "ship-ready review is older than latest commit"

    # Track which P0 comment ids the fix agent already addressed
    fix_replied_ids = set()
    for c in issue_comments + inline_comments:
        body = c.get("body", "")
        if FIX_REPLY_RE.search(body):
            m = re.search(r"comment\s+(\d+)", body)
            if m:
                fix_replied_ids.add(int(m.group(1)))

    for c in issue_comments + inline_comments:
        body = c.get("body", "")
        user = (c.get("user") or {}).get("login", "")
        is_reviewer = (
            user == "coderabbitai[bot]"
            or "codex" in user.lower()
            or "chatgpt" in user.lower()
            or body.startswith(REVIEW_P0_PREFIX)
            or body.startswith(REVIEW_HEADER)
        )
        if not is_reviewer:
            continue
        # Only P0 — auto-merge ignores P1+ (the fix agent handles those over time)
        is_p0 = "P0" in body and not body.startswith(REVIEW_P1_PREFIX)
        if not is_p0:
            continue
        if c.get("id") in fix_replied_ids:
            continue
        # The review-agent main review with "Blockers (P0)" then "None." is OK
        if body.startswith(REVIEW_HEADER):
            section = re.search(r"## Blockers \(P0\)\s*\n(.*?)(?=\n## |\Z)", body, re.DOTALL)
            if section and section.group(1).strip().lower() in (
                "- none.",
                "none.",
                "- (or write none.)",
            ):
                continue
        return False, f"unresolved P0 in comment {c.get('id')} from {user}"

    return True, "all clean"


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not WATCH_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_AUTOMERGE_REPOS)")
        return 0

    spend = SpendState(AGENT)

    cands = candidates()
    if not cands:
        print(f"[{AGENT.upper()}-IDLE] no candidates")
        return 0

    merged = []
    skipped = []
    for repo, pr in cands:
        pr_num = pr["number"]
        pr_author = ((pr.get("author") or {}).get("login")) or ""
        ok, reason = is_mergeable(repo, pr_num, pr_author=pr_author)
        if not ok:
            skipped.append((repo, pr_num, reason))
            continue

        # Squash-merge
        res = run(
            [
                "gh",
                "pr",
                "merge",
                str(pr_num),
                "-R",
                f"{GH_ORG}/{repo}",
                "--squash",
                "--delete-branch",
            ],
            timeout=60,
        )
        if res.returncode == 0:
            merged.append((repo, pr_num, pr["title"]))
            # Close out the lifecycle on every issue this PR resolves.
            for issue_num in linked_issue_numbers(pr):
                try:
                    gh_issue_edit(
                        repo,
                        issue_num,
                        add_labels=["agent:done"],
                        remove_labels=["agent:pr-open", "agent:in-flight", "agent:implement"],
                    )
                except Exception as e:
                    print(
                        f"[{AGENT}] {repo}#{issue_num}: label transition failed: {e}",
                        file=sys.stderr,
                    )
        else:
            skipped.append((repo, pr_num, f"merge failed: {res.stderr.strip()[:200]}"))

    spend.increment(firings_today=1)

    if not merged and not skipped:
        print(f"[{AGENT.upper()}-IDLE]")
        return 0

    lines = []
    if merged:
        spend.increment(merged_today=len(merged))
        lines.append(f"✅ Auto-merged {len(merged)} PR(s):")
        for repo, num, title in merged:
            lines.append(f"  - https://github.com/{GH_ORG}/{repo}/pull/{num} - {title[:80]}")
    if skipped:
        lines.append(f"\n⏸️ Skipped {len(skipped)} candidate(s):")
        for repo, num, reason in skipped:
            lines.append(f"  - https://github.com/{GH_ORG}/{repo}/pull/{num}: {reason}")

    msg = "\n".join(lines)
    print(msg)
    if merged:  # only post to Slack on actual merges
        slack_post(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
