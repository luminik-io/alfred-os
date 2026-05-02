#!/usr/bin/env python3
"""Daily morning brief - sums up yesterday's agent activity. Posts to Slack.

Reads spend files from ${HERMES_HOME}/state/<agent>/spend-YYYY-MM-DD.json
for every agent in ALFRED_MORNING_BRIEF_AGENTS, plus PRs labeled
agent:authored across ALFRED_MORNING_BRIEF_REPOS.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (  # noqa: E402
    GH_ORG,
    PreflightFailed, PreflightSpec,
    STATE_ROOT, doctor_mode, gh_json, preflight, slack_post,
)

AGENT = os.environ.get("AGENT_CODENAME", "morning-brief")
AGENTS = [
    a.strip()
    for a in os.environ.get("ALFRED_MORNING_BRIEF_AGENTS", "").split(",")
    if a.strip()
]
WATCH_REPOS = [
    r.strip()
    for r in os.environ.get("ALFRED_MORNING_BRIEF_REPOS", "").split(",")
    if r.strip()
]
PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["gh"],
    require_gh_auth=True,
)


def yesterday_str() -> str:
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_spend(agent: str, day: str) -> dict:
    p = STATE_ROOT / agent / f"spend-{day}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def yesterday_prs() -> list[dict]:
    """PRs opened yesterday by agents (label agent:authored)."""
    yday = yesterday_str()
    out = []
    for repo in WATCH_REPOS:
        prs = gh_json([
            "gh", "pr", "list", "-R", f"{GH_ORG}/{repo}",
            "--state", "all", "--label", "agent:authored",
            "--json", "number,title,state,mergedAt,createdAt", "--limit", "20",
        ], default=[])
        for pr in prs:
            if pr["createdAt"].startswith(yday):
                pr["repo"] = repo
                out.append(pr)
    return out


def main() -> int:
    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not AGENTS or not WATCH_REPOS:
        print(f"[{AGENT.upper()}-IDLE] no agents/repos configured "
              "(set ALFRED_MORNING_BRIEF_AGENTS and ALFRED_MORNING_BRIEF_REPOS)")
        return 0

    yday = yesterday_str()
    today = today_str()

    lines = [f"*Engineering team - overnight ({yday})*\n"]
    total_turns = 0
    total_successes = 0
    total_failures = 0

    for agent in AGENTS:
        s = load_spend(agent, yday)
        if not s:
            continue
        firings = s.get("firings_today", 0)
        turns = s.get("turns_today", 0)
        cost = s.get("cost_usd_today", 0)
        succ = s.get("successes_today", 0) + s.get("reviews_posted", 0) + s.get("fixes_landed", 0) + s.get("triaged_today", 0)
        fail = s.get("failures_today", 0) + s.get("failures", 0)
        total_turns += turns
        total_successes += succ
        total_failures += fail
        lines.append(f"- *{agent}*: firings={firings}, turns={turns}, ok={succ}, fail={fail} (cost-eq ${cost:.2f})")

    prs = yesterday_prs()
    if prs:
        lines.append(f"\n*PRs opened yesterday ({len(prs)})*")
        for pr in prs[:10]:
            url = f"https://github.com/{GH_ORG}/{pr['repo']}/pull/{pr['number']}"
            state = "merged" if pr.get("mergedAt") else pr.get("state", "OPEN").lower()
            lines.append(f"- {url} [{state}] {pr['title'][:80]}")
    else:
        lines.append("\n*PRs opened yesterday*: none")

    awaiting = []
    for repo in WATCH_REPOS:
        prs = gh_json([
            "gh", "pr", "list", "-R", f"{GH_ORG}/{repo}",
            "--state", "open", "--label", "agent:authored",
            "--json", "number,title,createdAt", "--limit", "20",
        ], default=[])
        for pr in prs:
            pr["repo"] = repo
            awaiting.append(pr)
    if awaiting:
        lines.append(f"\n*Awaiting your merge ({len(awaiting)})*")
        for pr in awaiting[:10]:
            url = f"https://github.com/{GH_ORG}/{pr['repo']}/pull/{pr['number']}"
            lines.append(f"- {url} - {pr['title'][:80]}")

    today_total = 0
    today_succ = 0
    for agent in AGENTS:
        s = load_spend(agent, today)
        today_total += s.get("turns_today", 0)
        today_succ += s.get("successes_today", 0) + s.get("reviews_posted", 0) + s.get("fixes_landed", 0)
    lines.append(f"\n*Today so far*: {today_total} turns, {today_succ} ok")

    msg = "\n".join(lines)
    print(msg)
    slack_post(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
