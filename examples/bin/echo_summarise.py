#!/usr/bin/env python3
"""Echo — issue summariser. Reference codename agent showing the full
alfred-os lifecycle: pick → claim → invoke claude → act → release →
report.

This is the agent built end-to-end in docs/TUTORIAL.md. Copy to
bin/<your-codename>.py in your fleet repo, rename, edit, register in
launchd/agents.conf.

What it does:
    1. Picks the oldest open issue carrying the `agent:summarise` label
       in the repo named by ECHO_REPO_SLUG.
    2. Claims it via the state machine (claim_issue) — refuses if
       another agent is already working it.
    3. Asks `claude -p` for a one-line summary.
    4. Posts the summary as an issue comment.
    5. Releases the claim with transition_to=agent:done.
    6. Reports success/failure to Slack with severity routing.

Compared to bin/hello.py (which is the absolute minimum), Echo
demonstrates: gh CLI integration, claude_invoke result handling, the
issue claim state machine, and severity-aware Slack reporting.

Required env (preflight will fail loud if missing):
    GH_ORG              — your fleet's GitHub org/user
    ECHO_REPO_SLUG      — <org>/<repo> Echo operates against
    HERMES_HOME         — set by the launchd plist
    WORKSPACE_ROOT      — set by the launchd plist

Cron suggestion: every 30 minutes.
    my.fleet.echo    echo_summarise.py    interval:1800    no
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    EventLog,
    PreflightFailed,
    PreflightSpec,
    SpendState,
    claim_issue,
    claude_invoke,
    doctor_mode,
    gh_issue_comment,
    gh_json,
    is_globally_blocked,
    preflight,
    release_issue,
    slack_post,
    with_lock,
)

AGENT = "echo"
REPO_SLUG = os.environ.get("ECHO_REPO_SLUG", "")

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["claude", "gh"],
    require_gh_auth=True,
    env_vars=["ECHO_REPO_SLUG", "GH_ORG"],
)


def pick_issue() -> dict | None:
    """Find the oldest open issue with the agent:summarise label."""
    issues = gh_json(
        [
            "gh",
            "issue",
            "list",
            "-R",
            REPO_SLUG,
            "--label",
            "agent:summarise",
            "--state",
            "open",
            "--json",
            "number,title,body,createdAt,labels",
            "--limit",
            "20",
        ],
        default=[],
    )
    if not issues:
        return None
    issues.sort(key=lambda i: i["createdAt"])
    for issue in issues:
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        # Defensive: claim_issue would refuse these too, but skip early to
        # avoid touching the gh API needlessly.
        if labels & {
            "agent:in-flight",
            "agent:pr-open",
            "do-not-pickup",
            "needs:human-scope",
            "agent:done",
        }:
            continue
        return issue
    return None


def build_prompt(issue: dict) -> str:
    return f"""Summarise this GitHub issue in one short sentence.

Be concrete: name files, paths, error messages, or numbers if the issue
mentions them. Do not restate the title verbatim. No preamble.

Title: {issue["title"]}

Body:
{issue["body"] or "(no body)"}

Reply with ONLY the one-line summary. No quotes around it.
"""


def main() -> int:
    with_lock(AGENT)
    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0
    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    if blocked := is_globally_blocked():
        print(f"[{AGENT.upper()}-GLOBAL-BLOCKED] {blocked}")
        return 0

    spend = SpendState(AGENT)

    issue = pick_issue()
    if issue is None:
        events.emit("firing_complete", outcome="silent_no_work")
        print(f"[{AGENT.upper()}-IDLE] no agent:summarise issues")
        return 0

    issue_num = issue["number"]
    if not claim_issue(REPO_SLUG, issue_num, codename=AGENT, firing_id=events.firing_id):
        events.emit("dedup_skip", repo=REPO_SLUG, number=issue_num)
        print(f"[{AGENT.upper()}-DEDUP-SKIP] #{issue_num} already claimed / blocked")
        return 0

    events.emit("issue_picked", repo=REPO_SLUG, number=issue_num)
    prompt = build_prompt(issue)

    result = claude_invoke(
        prompt,
        workdir=os.path.expanduser("~"),
        allowed_tools="",  # no tools — pure text
        agent=AGENT,
        max_turns=5,
        timeout=120,
    )
    spend.increment(
        firings_today=1,
        turns_today=result.num_turns,
        cost_usd_today=result.cost_usd,
    )

    if result.subtype != "success":
        release_issue(
            REPO_SLUG,
            issue_num,
            codename=AGENT,
            firing_id=events.firing_id,
            outcome=f"failure-{result.subtype}",
        )
        spend.increment(failures_today=1, consecutive_failures=1)
        slack_post(
            f"Echo failed on {REPO_SLUG}#{issue_num}: subtype={result.subtype}",
            severity="warn",
        )
        events.emit("firing_complete", outcome="failure", subtype=result.subtype)
        return 0

    summary = (result.result_text or "").strip()
    if not summary:
        release_issue(
            REPO_SLUG,
            issue_num,
            codename=AGENT,
            firing_id=events.firing_id,
            outcome="empty-output",
        )
        spend.increment(failures_today=1, consecutive_failures=1)
        slack_post(
            f"Echo got an empty response from claude on {REPO_SLUG}#{issue_num}",
            severity="warn",
        )
        return 0

    gh_issue_comment(REPO_SLUG, issue_num, f"**Echo (auto-summary):** {summary}")
    release_issue(
        REPO_SLUG,
        issue_num,
        codename=AGENT,
        firing_id=events.firing_id,
        outcome="success",
        transition_to="agent:done",
    )

    spend.set(consecutive_failures=0)
    spend.increment(successes_today=1)
    slack_post(f"Echo summarised {REPO_SLUG}#{issue_num}: _{summary[:120]}_")
    events.emit(
        "firing_complete",
        outcome="success",
        turns=result.num_turns,
        cost_usd=result.cost_usd,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
