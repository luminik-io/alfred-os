#!/usr/bin/env python3
"""Hello — the smallest possible alfred-os codename agent.

Demonstrates the canonical pattern every launchd-managed agent follows:

    1. Resolve agent_runner from $ALFRED_HOME/lib (set by the launchd plist)
    2. Acquire a per-agent mutex with with_lock()
    3. Run preflight() and exit clean on missing host config
    4. Short-circuit when ALFRED_DOCTOR=1 (so doctor.sh can exercise the
       agent without doing real work)
    5. Open an EventLog for this firing
    6. Do whatever this codename does
    7. Slack-post a one-line summary

Copy this file, rename to bin/<your-codename>.py, replace the body of
main(), and add an entry to your launchd/agents.conf:

    my.fleet.hello   hello.py   interval:3600   no
"""
from __future__ import annotations

import os
import sys

# The launchd plist sets ALFRED_HOME; bare invocation falls back to ~/.alfred.
sys.path.insert(0, (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib")
from agent_runner import (  # noqa: E402
    EventLog, PreflightFailed, PreflightSpec,
    doctor_mode, preflight, slack_post, with_lock,
)

AGENT = "hello"
PREFLIGHT = PreflightSpec(
    agent=AGENT,
    # Hello does no gh / aws / claude work, so just the framework env vars.
    # Real agents add bins=["claude", "gh"], require_gh_auth=True, etc.
)


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        # doctor.sh exercises every agent up to here; emit the OK sentinel
        # and exit before doing real work.
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    events = EventLog(agent=AGENT)
    events.emit("firing_started")

    # The body of a real agent goes here. Useful patterns:
    #
    # - Pick an issue:           gh_json(["gh", "issue", "list", ...])
    # - Open a worktree:         make_worktree(local_repo, AGENT, target)
    # - Invoke claude:           claude_invoke(prompt, workdir=wt, ...)
    # - Open a PR:               gh_pr_create(repo_slug, title=..., body_file=...)
    # - Track spend:             SpendState(AGENT).increment(turns_today=N)
    # - Trip the global block:   set_global_block(hours=1, reason="hello-rate-limit")

    msg = f"👋 Hello from alfred-os at {events.firing_id}"
    print(msg)
    slack_post(msg)
    events.emit("firing_complete", message=msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
