---
title: Slack
description: Create the app, mint the webhook, store it, post your first message.
---

Alfred posts simple agent reports via an incoming webhook. `slack_post()`
resolves the URL via env -> 30-day disk cache -> AWS Secrets Manager, so
steady-state firings don't pay an AWS round-trip every time. Agents that use
`lib/slack_format.py` can also post Block Kit firing threads with an optional
Slack bot token.

Full guide at [`docs/SLACK_SETUP.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_SETUP.md). Highlights:

## 1. Create the app

https://api.slack.com/apps → **Create New App** → **From scratch** → name it (e.g. `<yourstartup>-agents`) → pick the workspace.

## 2. Add Incoming Webhooks

App settings → **Features → Incoming Webhooks** → toggle on → **Add New Webhook to Workspace** → pick the channel → **Allow**.

Copy the URL. It's a secret. Anyone with it can post to your channel.

## 3. Test it

```sh
curl -X POST -H 'Content-Type: application/json' \
  --data '{"text":"hello from Alfred setup"}' \
  'https://hooks.slack.com/services/T.../B.../...'
```

Should appear in the channel within a second.

## 4. Store it

Three options:

### Option A: Env var (simplest)

```sh
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...' >> ~/.alfredrc
exec $SHELL
```

### Option B: AWS Secrets Manager (recommended for prod)

```sh
aws --profile <admin> secretsmanager create-secret \
  --name alfred/slack-webhook \
  --description "Slack incoming webhook for the agent fleet" \
  --secret-string 'https://hooks.slack.com/services/T.../B.../...' \
  --region us-east-1
```

The framework's default secret ID is `alfred/slack-webhook`. See [AWS setup](/guides/aws/) for the IAM policy your scheduled-agent identity needs.

### Option C: Both

Set `SLACK_WEBHOOK_URL` only to override the AWS-stored value (e.g. testing a rotation).

## 5. Verify in Python

```python
import sys
sys.path.insert(0, "lib")
from agent_runner import slack_post
slack_post("Alfred setup test", severity="info")
```

You should see the message in your channel.

## Severity routing

`slack_post(text, severity="info" | "warn" | "alert")`. See [Severity routing](/concepts/severity-routing/).

## Optional: bot token (`xoxb-`)

Required for `firing_thread_root`, `firing_thread_reply`, and
`firing_thread_close` in `lib/slack_format.py`.

```sh
SLACK_BOT_TOKEN=xoxb-...
SLACK_HOME_CHANNEL=alfred
```

Or store the token in AWS Secrets Manager at `alfred/slack-bot-token` and leave
`SLACK_BOT_TOKEN` unset. See [Slack setup → Optional: bot token](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_SETUP.md#optional-bot-token-xoxb-).

## Optional: app-level token (`xapp-1-`)

Required only for Socket Mode (slash commands, button clicks). See [Slack setup → Optional: app-level token](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_SETUP.md#optional-app-level-token-xapp-1-).

## Optional: plan-mode approval gate

If you want the operator to approve every Batman plan in Slack (instead of
the file-polling fallback), wire up `lib/slack_approval.py`. It reuses the
bot token resolved above, posts the plan, and polls reactions on that one
message until the configured operator reacts with `:white_check_mark:` (or
`:x:` to reject).

The plan thread is also the amendment surface. The configured operator can
reply in plain English before reacting. Alfred acknowledges newly captured
plan replies in-thread, then carries those replies as operator amendments when
the plan is approved. Repo add/remove replies update execution scope before
child issues or worktrees are created. Use the thread for changes such as
"remove mobile", "make this read-only", "add an empty state", or "split this
into two PRs".

Structured replies work too:

```text
acceptance: the PR body links back to the original GitHub issue
test: add coverage for the plan-thread parser
add repo: my-org/mobile
remove repo: my-org/site
question: should this wait for a clearer spec?
```

```sh
# the only Slack user whose reactions count
export ALFRED_OPERATOR_SLACK_USER_ID=U0123ABCDEF

# enable the AWS Secrets Manager resolver if you store the bot token there
export ALFRED_SECRETS_BACKEND=aws
```

Required Slack scopes (in addition to `chat:write`): `reactions:read`,
`channels:read`, `groups:read`. Add `channels:history` and `groups:history`
when you want Alfred to capture operator replies from approval threads.
Install the optional `[slack]` extra (`pip install 'alfred-os[slack]'`).

Full walkthrough at [`docs/SLACK_APPROVAL.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/SLACK_APPROVAL.md):
app manifest snippet, env var reference, fallback strategy ordering,
operator-only check semantics, and the
[`agent:plan-pending-approval`](/concepts/state-machine/) label transition
the gate drives.

## Rotating

If you accidentally paste the URL somewhere it shouldn't be:

1. https://api.slack.com/apps → your app → **Incoming Webhooks** → trash icon on the compromised URL.
2. Add a new webhook to the same channel.
3. Update wherever you stored it (env var or AWS).
4. `rm $ALFRED_HOME/state/slack-webhook.cache` so the next firing re-fetches.
