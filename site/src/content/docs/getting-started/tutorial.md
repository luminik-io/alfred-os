---
title: Your first agent
description: Build Echo, a working alfred-os agent, end-to-end in 30 minutes.
---

By the end you'll have a codename agent **Echo** that picks the oldest open issue with a specific label, asks Claude for a one-line summary, posts it as an issue comment, and reports to Slack. Fires every 30 minutes via `launchd`, isolated in a per-firing git worktree, claiming the issue via the [state machine](/alfred-os/concepts/state-machine/) before posting.

Condensed companion to [`docs/TUTORIAL.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/TUTORIAL.md). Full agent source at [`examples/bin/echo_summarise.py`](https://github.com/luminik-io/alfred-os/blob/main/examples/bin/echo_summarise.py); copy-paste-ready.

## Prerequisites

You've completed [Install](/alfred-os/getting-started/install/). `bash bin/doctor.sh` shows `0 passed, 0 failed`. `gh auth login` and `claude` are authenticated.

## 1. Pick a target repo

```sh
echo 'ECHO_REPO_SLUG=myorg/sandbox-repo' >> ~/.alfredrc
exec $SHELL
```

## 2. Create a test issue

```sh
gh label create agent:summarise --color "00ccff" \
  --description "Echo will summarise this issue" \
  -R "$ECHO_REPO_SLUG"

gh issue create -R "$ECHO_REPO_SLUG" \
  --title "test issue for the Echo tutorial" \
  --body "Echo should pick this up and post a one-line summary." \
  --label "agent:summarise"
```

## 3. Drop in the example agent

```sh
cp examples/bin/echo_summarise.py bin/echo.py
chmod +x bin/echo.py
```

## 4. Register in `launchd/agents.conf`

Append:

```
my.fleet.echo	echo.py	interval:1800	no
```

## 5. Deploy + verify

```sh
bash deploy.sh
bash bin/doctor.sh
```

Doctor should now report `1 passed, 0 failed` (or `N+1`).

## 6. Force a firing

Don't wait 30 minutes:

```sh
launchctl kickstart -k "gui/$(id -u)/my.fleet.echo"
tail -f /tmp/my.fleet.echo.std{out,err}
```

Within ~10 seconds:

```
Echo summarised myorg/sandbox-repo#42: <one-line summary>
```

Look at the issue on GitHub:

- A new comment from your gh user.
- The `agent:in-flight` label briefly appeared, then was replaced with `agent:done`.
- Three structured comments: claim, release, and the actual summary.

Check your configured fleet channel in Slack: the success message is there.

## 7. Confirm dedup actually works

Force a second firing immediately:

```sh
launchctl kickstart -k "gui/$(id -u)/my.fleet.echo"
```

Output: `[ECHO-IDLE] no agent:summarise issues`. The first firing transitioned the issue to `agent:done`, which blocks future claims.

## What you just learned

Every framework primitive Echo uses scales up to a richer agent without changing shape:

- `with_lock(AGENT)`: host-level mutex prevents concurrent firings of the same codename.
- `preflight(PREFLIGHT)`: fail loud and early on missing env / CLIs / auth.
- `doctor_mode()`: `bash bin/doctor.sh` doesn't burn turns or commit side effects.
- `is_globally_blocked()`: fleet-wide rate-limit poison pill.
- `SpendState(AGENT)`: per-agent per-day spend tracking.
- `claim_issue()` / `release_issue()`: [issue claim state machine](/alfred-os/concepts/state-machine/).
- `claude_invoke()`: structured `claude -p` invocation, parses turns/cost/session_id/result.
- `gh_issue_comment()`: gh CLI wrapper.
- `slack_post(text, severity=)`: webhook post with [severity routing](/alfred-os/concepts/severity-routing/).
- `EventLog`: per-firing JSONL audit log.

For richer agents (write code, open PRs, multi-step prompts, max-turns resume), see the shipped runners under `bin/` and the examples in `examples/bin/`.

## Next

- [Issue claim state machine](/alfred-os/concepts/state-machine/): what `claim_issue` actually does
- [Slack setup](/alfred-os/guides/slack/): wire your channel
- [agent_runner API reference](/alfred-os/reference/agent-runner/): every primitive available
