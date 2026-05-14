#!/usr/bin/env python3
"""Hello, the smallest possible alfred-os codename agent.

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

Try it with zero host config:

    ALFRED_DRY_RUN=1 python3 examples/bin/hello.py
    python3 examples/bin/hello.py --dry-run

In dry-run nothing is posted to Slack for real; the lifecycle is narrated
to stdout and the process exits 0.
"""
from __future__ import annotations

import os
import sys

# The launchd plist sets ALFRED_HOME; bare invocation falls back to ~/.alfred.
sys.path.insert(0, (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib")
from agent_runner import (  # noqa: E402
    EventLog, PreflightFailed, PreflightSpec,
    doctor_mode, dry_run_log, is_dry_run, preflight, set_dry_run, slack_post, with_lock,
)

# Accept `--dry-run` as a CLI flag in addition to ALFRED_DRY_RUN=1. Do this
# before anything else so every downstream agent_runner seam sees the mode.
if "--dry-run" in sys.argv:
    set_dry_run(True)

AGENT = "hello"
PREFLIGHT = PreflightSpec(
    agent=AGENT,
    # Hello does no gh / aws / claude work, so just the framework env vars.
    # Real agents add bins=["claude", "gh"], require_gh_auth=True, etc.
)


def main() -> int:
    with_lock(AGENT)

    if is_dry_run():
        dry_run_log("start", f"{AGENT} dry-run firing, no LLM, no spend, no side effects")

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        # In dry-run a config gap is expected (the whole point is "run it
        # with nothing configured"). Narrate it and keep going so the rest
        # of the lifecycle still flows. A real firing still exits clean.
        if is_dry_run():
            dry_run_log("preflight", "preflight reported config gaps, continuing (dry-run)")
        else:
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
