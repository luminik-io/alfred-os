# Tutorial: your first cron-driven agent in 30 minutes

End-to-end walkthrough. By the end:

- A new codename agent `Echo` that picks the oldest open issue with a specific label, asks Claude to add a one-line summary as a comment, reports to Slack.
- That agent firing every 30 minutes via `launchd`, isolated in a per-firing git worktree, claiming the issue via the state machine before posting, with a clean `bash bin/doctor.sh` pass.
- The framework's mental model, so you can write the next codename without re-reading anything.

Assumes you've completed [`INSTALL.md`](../INSTALL.md): `bash install.sh` has run, `gh auth login` and `claude` are authenticated, `bash deploy.sh && bash bin/doctor.sh` shows `0 passed, 0 failed`.

## What we're building

Echo doesn't write code. It summarises issues. On a specific repo, find issues labeled `agent:summarise`, pick the oldest, claim it via the state machine, ask Claude for a one-line summary, post the summary as an issue comment, release the claim, report to Slack.

Same shape as Lucius, Drake, Ra's al Ghul: pick → claim → invoke claude → act → release → report. Once you can write Echo, you can write any of them.

## Step 1: pick a target repo

Echo needs a repo to operate on. For the tutorial, use any repo you own where you can label issues. Set the env var:

```sh
export ECHO_REPO_SLUG=myorg/sandbox-repo
```

Add to `~/.alfredrc` so it persists.

## Step 2: prepare a test issue

Open an issue in that repo. Add the label `agent:summarise` (create it first if needed):

```sh
gh label create agent:summarise --color "00ccff" \
  --description "Echo will summarise this issue" \
  -R "$ECHO_REPO_SLUG"

gh issue create \
  -R "$ECHO_REPO_SLUG" \
  --title "test issue for the Echo tutorial" \
  --body "This is a test issue. Echo should pick it up and post a summary." \
  --label "agent:summarise"
```

Note the issue number it returns.

## Step 3: write the agent

Save as `bin/echo.py` in your fleet repo:

```python
#!/usr/bin/env python3
"""Echo - the simplest useful alfred-os agent. Summarises issues."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (  # noqa: E402
    EventLog, PreflightFailed, PreflightSpec,
    SpendState, claim_issue, claude_invoke, doctor_mode, gh_issue_comment,
    gh_json, is_globally_blocked, preflight, release_issue, slack_post,
    with_lock,
)

AGENT = "echo"
REPO_SLUG = os.environ.get("ECHO_REPO_SLUG", "")
PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["claude", "gh"],
    require_gh_auth=True,
    env_vars=["ECHO_REPO_SLUG"],
)


def pick_issue() -> dict | None:
    """Find the oldest open issue with the agent:summarise label."""
    issues = gh_json([
        "gh", "issue", "list", "-R", REPO_SLUG,
        "--label", "agent:summarise", "--state", "open",
        "--json", "number,title,body,createdAt",
        "--limit", "20",
    ], default=[])
    if not issues:
        return None
    issues.sort(key=lambda i: i["createdAt"])
    return issues[0]


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
        print(f"[{AGENT.upper()}-IDLE] no agent:summarise issues")
        return 0

    issue_num = issue["number"]
    if not claim_issue(REPO_SLUG, issue_num,
                       codename=AGENT, firing_id=events.firing_id):
        print(f"[{AGENT.upper()}-DEDUP-SKIP] #{issue_num} already claimed")
        return 0

    prompt = f"""Summarise this issue in one short sentence.
Be concrete. Don't restate the title. Quote any concrete numbers or paths.

Title: {issue['title']}

Body:
{issue['body']}

Reply with ONLY the one-line summary, no preamble.
"""

    result = claude_invoke(
        prompt, workdir=os.path.expanduser("~"),
        allowed_tools="", agent=AGENT, max_turns=5, timeout=120,
    )
    spend.increment(firings_today=1, turns_today=result.num_turns,
                    cost_usd_today=result.cost_usd)

    if result.subtype != "success":
        release_issue(REPO_SLUG, issue_num, codename=AGENT,
                      firing_id=events.firing_id, outcome="failure")
        slack_post(f"❌ Echo failed on #{issue_num}: {result.subtype}",
                   severity="warn")
        spend.increment(failures_today=1)
        return 0

    summary = (result.result_text or "").strip()
    gh_issue_comment(REPO_SLUG, issue_num,
                     f"**Echo (auto-summary):** {summary}")
    release_issue(REPO_SLUG, issue_num, codename=AGENT,
                  firing_id=events.firing_id, outcome="success",
                  transition_to="agent:done")

    spend.increment(successes_today=1)
    slack_post(f"✅ Echo summarised {REPO_SLUG}#{issue_num}: _{summary[:120]}_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

## Step 4: register the agent in agents.conf

Add to `launchd/agents.conf`:

```
my.fleet.echo	echo.py	interval:1800	no
```

(`1800` seconds = 30 minutes. `no` because Echo doesn't compile any Java.)

## Step 5: deploy + verify

```sh
chmod +x bin/echo.py
bash deploy.sh
bash bin/doctor.sh
```

Expected output from doctor includes:

```
  echo                         ✅ ok
doctor: 1 passed, 0 failed
```

(Or `N+1 passed` if you have other agents.)

## Step 6: fire once on demand

Don't wait 30 minutes. Force a firing:

```sh
launchctl kickstart -k "gui/$(id -u)/my.fleet.echo"
tail -f /tmp/my.fleet.echo.stdout /tmp/my.fleet.echo.stderr
```

Within ~10 seconds you should see:

```
✅ Echo summarised myorg/sandbox-repo#42: _<the one-line summary>_
```

Look at the issue in GitHub: there's a new comment from your gh user, the `agent:in-flight` label was added then replaced with `agent:done`, and three structured comments are visible in the issue: claim, release, and the actual summary.

Look at `#alfred` in Slack: there's the success message.

## Step 7: confirm dedup actually works

Force a second firing immediately:

```sh
launchctl kickstart -k "gui/$(id -u)/my.fleet.echo"
```

Output:

```
[ECHO-IDLE] no agent:summarise issues
```

The first firing transitioned the issue to `agent:done`. Removing the `agent:summarise` label is up to your prompt, but `claim_issue` would have refused either way because `agent:done` blocks claiming.

Create another test issue with the `agent:summarise` label and force-fire again. Echo picks it up, summarises, transitions, done.

## Step 8: pause + resume

```sh
launchctl bootout "gui/$(id -u)" \
  ~/Library/LaunchAgents/my.fleet.echo.plist
# … tea …
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/my.fleet.echo.plist
```

Or, when you've installed the operator-facing CLI from the reference fleet, just:

```sh
alfred pause echo
alfred resume echo
```

## What you just learned

Every framework primitive Echo uses scales up to a richer agent without changing shape:

- `with_lock(AGENT)`: host-level mutex prevents concurrent firings.
- `preflight(PREFLIGHT)`: fail loud and early on missing env / CLIs / auth.
- `doctor_mode()`: `bash bin/doctor.sh` doesn't burn turns or commit side effects.
- `is_globally_blocked()`: fleet-wide rate-limit poison pill.
- `SpendState(AGENT)`: per-agent per-day spend tracking.
- `claim_issue()` / `release_issue()`: issue claim state machine ([STATE_MACHINE.md](STATE_MACHINE.md)).
- `claude_invoke()`: structured `claude -p` invocation, parses turns/cost/session_id/result.
- `gh_issue_comment()` / `gh_pr_*()`: gh CLI wrappers.
- `slack_post(text, severity=)`: webhook post with severity routing.
- `EventLog`: per-firing JSONL audit log.

For richer agents (write code, open PRs, multi-step prompts, max-turns resume), see [`examples/bin/`](../examples/bin/) and the reference fleet at [`luminik-io/alfred`](https://github.com/luminik-io/alfred).

## Next steps

- [`STATE_MACHINE.md`](STATE_MACHINE.md) for dedup/race semantics.
- [`AWS_SETUP.md`](AWS_SETUP.md) if your agent needs Secrets Manager.
- [`SKILLS.md`](SKILLS.md) for the recommended Claude Code skills. Echo doesn't use any; richer agents do.
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the design rationale (codename pattern, plan-review gate, IAM-per-agent).

## Common stumbles

**`claim_issue` returns False on the first firing.** The `agent:in-flight` label might already be set from a previous experiment. `gh issue edit <N> -R <repo> --remove-label agent:in-flight` and try again.

**`claude_invoke` exits 0 but `result.subtype == "error_max_turns"`.** Echo uses 5 turns, which is enough for a one-line summary. If you saw this on a different prompt, raise `max_turns=`.

**Slack post returns `True` but you don't see the message.** Webhook cache may be stale. `rm $HERMES_HOME/state/slack-webhook.cache` and retry.

**`launchctl kickstart` fails with "Could not find specified service."** The plist isn't loaded. `bash deploy.sh` again. The launchctl bootstrap step is idempotent.

**Doctor passes but the agent fails on real firing.** Doctor only verifies preflight. Real firings can fail for runtime reasons (rate limit, gh API timeout, claude error). Check `/tmp/my.fleet.echo.stderr`.
