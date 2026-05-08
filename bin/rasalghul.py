#!/usr/bin/env python3
"""Ras al Ghul - PR review agent. Read-only review delegated to claude -p."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    GH_ORG,
    WORKSPACE,
    WORKSPACE_ROOT,
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    claude_invoke,
    doctor_mode,
    gh_json,
    gh_pr_comment,
    is_globally_blocked,
    is_repo_paused,
    optional_env_int,
    preflight,
    run,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "rasalghul")
LAUNCHD_LABEL = os.environ.get("LAUNCHD_LABEL", f"my.fleet.{AGENT}")

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["claude", "gh", "git"],
    require_gh_auth=True,
)

REVIEW_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_RASALGHUL_REPOS", "").split(",") if r.strip()
]

# Specs / docs PRs are markdown-heavy; line count != review effort.
# Operator can name the docs-style repos to get a higher diff cap; default cap
# applies to everything else.
SPECS_REPOS = {
    r.strip() for r in os.environ.get("ALFRED_RASALGHUL_SPECS_REPOS", "").split(",") if r.strip()
}
DIFF_LINE_CAP_DEFAULT = int(os.environ.get("ALFRED_RASALGHUL_DIFF_CAP", "4000"))
DIFF_LINE_CAP_SPECS = int(os.environ.get("ALFRED_RASALGHUL_DIFF_CAP_SPECS", "8000"))

DAILY_TURN_CAP = int(os.environ.get("ALFRED_RASALGHUL_TURN_CAP", "800"))
DAILY_REVIEW_CAP = int(os.environ.get("ALFRED_RASALGHUL_REVIEW_CAP", "30"))
REVIEW_AUTHOR_PREFIX = f"{AGENT.title()} - review"


def _extract_section(text: str, header: str) -> list[str]:
    """Pull the bullet items under a markdown ## header. Returns [] if section absent."""
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped == header
            continue
        if not in_section:
            continue
        if stripped.startswith("- "):
            item = stripped[2:].strip()
            if item.lower() in ("none.", "(or write none.)", "(or none.)"):
                continue
            if item.startswith("(") and item.endswith(")"):
                continue
            out.append(item)
    return out


def pick_pr() -> tuple[str, dict] | tuple[None, None]:
    """Find oldest open PR not yet reviewed by this agent (and not draft, not WIP)."""
    for repo in REVIEW_REPOS:
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
                "--json",
                "number,title,headRefName,url,createdAt,labels,isDraft",
                "--limit",
                "30",
            ],
            default=[],
        )
        if not prs:
            continue
        for pr in prs:
            if pr.get("isDraft"):
                continue
            if any(t in pr["title"].lower() for t in ("wip", "[wip]")):
                continue
            label_names = [lbl["name"] for lbl in pr.get("labels", [])]
            if "do-not-review" in label_names:
                continue
            # Age > 5 min (give bot reviewers first crack)
            try:
                created = datetime.strptime(pr["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC
                )
                if (datetime.now(UTC) - created).total_seconds() < 300:
                    continue
            except ValueError:
                pass
            # Re-verify state right now
            state = gh_json(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr["number"]),
                    "-R",
                    f"{GH_ORG}/{repo}",
                    "--json",
                    "state",
                ],
                default={},
            ).get("state")
            if state != "OPEN":
                continue
            # Re-review if there are new commits since the most recent
            # review of ours. Original logic skipped any PR ever reviewed,
            # which silently dropped author-fix iterations.
            view = gh_json(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr["number"]),
                    "-R",
                    f"{GH_ORG}/{repo}",
                    "--json",
                    "comments,commits",
                ],
                default={"comments": [], "commits": []},
            )
            comments = view.get("comments", []) or []
            commits = view.get("commits", []) or []

            our_reviews = [
                c for c in comments if c.get("body", "").startswith(REVIEW_AUTHOR_PREFIX)
            ]
            if not our_reviews:
                return repo, pr

            try:
                last_review_ts = max(
                    datetime.strptime(c["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                    for c in our_reviews
                    if c.get("createdAt")
                )
            except (ValueError, KeyError):
                continue

            new_commits = False
            for c in commits:
                ts_str = c.get("committedDate") or c.get("authoredDate") or ""
                if not ts_str:
                    continue
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                except ValueError:
                    continue
                if ts > last_review_ts:
                    new_commits = True
                    break

            if new_commits:
                return repo, pr
            continue
    return None, None


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not REVIEW_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no repos configured (set ALFRED_RASALGHUL_REPOS)")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    blocked = is_globally_blocked()
    if blocked:
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}. Skipping firing.")
        events.emit("firing_complete", outcome="global-blocked")
        return 0
    spend = SpendState(AGENT)

    if spend.state["turns_today"] >= DAILY_TURN_CAP:
        msg = f"[{AGENT.upper()}-DAILY-CAP] turns={spend.state['turns_today']} >= {DAILY_TURN_CAP}. Pausing."
        print(msg)
        slack_post(msg)
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
        events.emit("firing_complete", outcome="daily-cap")
        return 0
    if spend.state.get("reviews_posted", 0) >= DAILY_REVIEW_CAP:
        print(
            f"[{AGENT.upper()}-REVIEW-CAP] {spend.state['reviews_posted']} reviews posted today. Skipping."
        )
        events.emit("firing_complete", outcome="review-cap")
        return 0

    repo, pr = pick_pr()
    if not repo:
        print(f"[{AGENT.upper()}-IDLE]")
        events.emit("firing_complete", outcome="idle-no-pr")
        return 0

    pr_num = pr["number"]
    local_path = WORKSPACE / repo
    events.emit("pr_picked", repo=f"{GH_ORG}/{repo}", number=pr_num)

    # Fetch diff + meta + prior reviewer comments
    tmp = Path(tempfile.mkdtemp(prefix=f"{AGENT}-"))
    diff_file = tmp / "diff.patch"
    diff_res = run(["gh", "pr", "diff", str(pr_num), "-R", f"{GH_ORG}/{repo}"], timeout=30)
    diff_file.write_text(diff_res.stdout)

    if diff_file.stat().st_size == 0:
        print(f"[{AGENT.upper()}-SKIP] empty diff for PR {pr_num} on {repo}")
        events.emit("firing_complete", outcome="empty-diff")
        return 0

    lines = diff_res.stdout.count("\n")
    is_specs = repo in SPECS_REPOS
    line_cap = DIFF_LINE_CAP_SPECS if is_specs else DIFF_LINE_CAP_DEFAULT
    if lines > line_cap:
        gh_pr_comment(
            repo,
            pr_num,
            f"{REVIEW_AUTHOR_PREFIX}: this PR diff is {lines} lines (cap {line_cap}). Please split for an effective review.",
        )
        events.emit("firing_complete", outcome="diff-too-large", lines=lines)
        return 0

    meta = gh_json(
        [
            "gh",
            "pr",
            "view",
            str(pr_num),
            "-R",
            f"{GH_ORG}/{repo}",
            "--json",
            "title,body,additions,deletions",
        ],
        default={},
    )
    pr_title = meta.get("title", "")
    pr_body = meta.get("body", "") or ""

    # Prior reviewer comments from known bot reviewers
    prior_comments = gh_json(
        [
            "gh",
            "api",
            f"/repos/{GH_ORG}/{repo}/pulls/{pr_num}/comments",
            "--paginate",
        ],
        default=[],
    )
    prior = [
        {
            "user": c["user"]["login"],
            "body": c["body"],
            "path": c.get("path"),
            "line": c.get("line"),
        }
        for c in prior_comments
        if c.get("user", {}).get("login", "") in ("coderabbitai[bot]",)
        or "codex" in c.get("user", {}).get("login", "").lower()
        or "chatgpt" in c.get("user", {}).get("login", "").lower()
    ]
    (tmp / "prior-reviews.json").write_text(json.dumps(prior, indent=2))

    if is_specs:
        prompt = f"""You are {AGENT.title()} reviewing a SPECS pull request (markdown documentation, not code).

PR: https://github.com/{GH_ORG}/{repo}/pull/{pr_num}
Title: {pr_title}

Body:
{pr_body}

The diff is at {tmp}/diff.patch — read it.
Working directory: {local_path} (you can grep the surrounding repo for context).
Workspace root for cross-repo grep: {WORKSPACE_ROOT}

Specs review axes (priority order):
1. Internal consistency - does spec N contradict spec M? Cross-references valid?
2. Code reality alignment - does the spec describe what code actually does? Where the spec says "the X service does Y", grep the code to confirm.
3. Vocabulary discipline - avoid stale or marketing vocab. Avoid em-dashes. Avoid fabricated numbers.
4. Definition-of-done testability - measurable acceptance criteria, or aspirational prose?
5. Scope clarity - one cohesive area, or sprawl?
6. Open questions / TODOs surfaced clearly?
7. Strategy alignment - does the new doc align with the rest?
8. Risk / what-could-go-wrong sections present where warranted.

Hard rules:
- Evidence-first. Every critical finding includes file:line and a concrete contradiction.
- Severity: P0 (blocker - shipping this would mislead engineering), P1 (fix before merge), P2 (follow-up OK), nit.
- No em-dashes. No "unlock", "leverage", "seamless", "transform", "robust". No fabricated numbers.
- You have Read/Bash/Glob/Grep ONLY. Read-only.

Output - print EXACTLY this structure to stdout, nothing else:

{REVIEW_AUTHOR_PREFIX} (specs PR)

## Blockers (P0)
- file:line - <statement> - <why>
- (or write None.)

## Should fix before merge (P1)
- ...
- (or None.)

## Worth considering (P2)
- ...
- (or None.)

## Cross-spec consistency
- (call out contradictions, or skip if clean)

## Strengths
- (1-3 only if real, otherwise omit)

Ship-ready: yes / no - <one sentence>
"""
    else:
        prompt = f"""You are {AGENT.title()}, the code review agent. Review this pull request and produce a single structured review comment.

PR: https://github.com/{GH_ORG}/{repo}/pull/{pr_num}
Title: {pr_title}

Body:
{pr_body}

The diff is at {tmp}/diff.patch - read it.
Existing bot-reviewer comments at {tmp}/prior-reviews.json - read them. DO NOT duplicate their findings.
Working directory: {local_path}.

Review axes (priority order):
1. Correctness - does it do what the title and body say? Edge cases?
2. Security - secret leaks, SQL injection, auth bypass, CSRF, CORS, rate limits, input validation, XSS, path traversal, multi-tenant isolation.
3. Data integrity - transactions, idempotency, migrations that could lose rows or drop columns.
4. Concurrency - race conditions, shared state, connection pools, transaction boundaries.
5. Failure modes - timeouts, retries, backoff, circuit breakers.
6. Observability - if this breaks at 3am, can you diagnose from logs?
7. Performance - N+1 queries, unbounded loops, full-table scans.
8. Consistency - matches existing repo patterns?
9. Test adequacy - do tests prove behavior or just exercise paths?
10. Reversibility - can this roll back cleanly?

Hard rules:
- Evidence-first. Every critical finding includes file:line and a concrete scenario that breaks.
- Severity: P0 (blocker), P1 (fix before merge), P2 (follow-up OK), nit.
- Skip findings other reviewers already flagged.
- No em-dashes. No "unlock", "leverage", "seamless", "transform". No fabricated numbers.
- If you cannot form a confident opinion in 3 read passes, say so and ask a specific clarifying question.
- You have Read/Bash/Glob/Grep ONLY. NOT writing code.

Output - print EXACTLY this structure to stdout, nothing else:

{REVIEW_AUTHOR_PREFIX}

## Blockers (P0)
- file:line - <statement> - <why>
- (or write None.)

## Should fix before merge (P1)
- ...
- (or None.)

## Worth considering (P2)
- ...
- (or None.)

## Strengths
- (1-2 only if real, otherwise omit)

Ship-ready: yes / no - <one sentence>
"""

    result = claude_invoke(
        prompt,
        workdir=local_path,
        allowed_tools="Read,Bash,Glob,Grep",
        max_turns=optional_env_int("ALFRED_RASALGHUL_MAX_TURNS", minimum=40),
        timeout=900,
    )
    spend.increment(firings_today=1, turns_today=result.num_turns, cost_usd_today=result.cost_usd)
    events.emit(
        "claude_invoke_done", turns=result.num_turns, subtype=result.subtype, success=result.success
    )

    if not result.success:
        spend.increment(failures_today=1)
        msg = (
            f"❌ {AGENT.title()}: subtype={result.subtype} turns={result.num_turns} on PR {pr_num}"
        )
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome=f"claude-{result.subtype}")
        return 0

    # Salvage off-format output instead of dropping it. Model sometimes emits
    # conversational filler before the review header; the gate-on-prefix
    # behaviour silently dropped real findings. Slice to the header if found,
    # else wrap under a synthetic recovered header so the gate-phrase still
    # appears at the top of the body.
    text = (result.result_text or "").strip()
    if not text.startswith(REVIEW_AUTHOR_PREFIX):
        idx = text.find(REVIEW_AUTHOR_PREFIX)
        if idx >= 0:
            preface = text[:idx].strip()
            text = text[idx:]
            if preface:
                print(
                    f"[{AGENT.upper()}-SALVAGED] stripped {len(preface)}-char preface",
                    file=sys.stderr,
                )
        else:
            print(
                f"[{AGENT.upper()}-SALVAGED] wrapping {len(text)}-char output under synthetic header",
                file=sys.stderr,
            )
            text = (
                f"{REVIEW_AUTHOR_PREFIX} (recovered from off-format output)\n\n"
                "## Blockers (P0)\n- (recovered review below; format-gate suppressed; verify before merge)\n\n"
                "## Should fix before merge (P1)\n- (see body)\n\n"
                "## Worth considering (P2)\n- (see body)\n\n"
                "## Recovered body\n\n" + text + "\n\n"
                "Ship-ready: no\n"
            )

    # Re-verify PR is still OPEN
    state = gh_json(
        ["gh", "pr", "view", str(pr_num), "-R", f"{GH_ORG}/{repo}", "--json", "state"], default={}
    ).get("state")
    if state != "OPEN":
        msg = f"[{AGENT.upper()}-STALE] PR {pr_num} is now {state}, not posting review."
        print(msg)
        events.emit("firing_complete", outcome="pr-stale", state=state)
        return 0

    if not gh_pr_comment(repo, pr_num, text):
        spend.increment(failures_today=1)
        msg = f"❌ {AGENT.title()}: failed to post review on PR {pr_num}"
        print(msg)
        slack_post(msg)
        events.emit("firing_complete", outcome="post-failed")
        return 0

    # Split P0/P1 findings into per-finding sub-comments so the review-to-fix
    # agent (default: Nightwing) can address each one independently — it dedups
    # by comment-id.
    p0_findings = _extract_section(text, "## Blockers (P0)")
    p1_findings = _extract_section(text, "## Should fix before merge (P1)")
    posted_split = 0
    for finding in p0_findings:
        if gh_pr_comment(repo, pr_num, f"{AGENT.title()} P0: {finding}"):
            posted_split += 1
    for finding in p1_findings:
        if gh_pr_comment(repo, pr_num, f"{AGENT.title()} P1: {finding}"):
            posted_split += 1

    spend.increment(reviews_posted=1, successes_today=1)
    events.emit(
        "review_posted",
        repo=f"{GH_ORG}/{repo}",
        number=pr_num,
        turns=result.num_turns,
        p0_count=len(p0_findings),
        p1_count=len(p1_findings),
        split_comments=posted_split,
    )
    msg = f"{AGENT.title()}: reviewed https://github.com/{GH_ORG}/{repo}/pull/{pr_num} (turns={result.num_turns}, split={posted_split} P0/P1 sub-comments)"
    print(msg)
    slack_post(msg)
    events.emit("firing_complete", outcome="review-posted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
