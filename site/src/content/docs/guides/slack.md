---
title: Slack
description: Create the app, mint the webhook, store it, post your first message.
---

Pennyworth posts agent reports to a Slack channel via an **incoming webhook**. The framework's `slack_post()` resolves the URL via env → 7-day disk cache → AWS Secrets Manager, so steady-state firings don't pay an AWS round-trip every time.

The full guide lives at [`docs/SLACK_SETUP.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/SLACK_SETUP.md). The highlights:

## 1. Create the app

https://api.slack.com/apps → **Create New App** → **From scratch** → name it (e.g. `<yourstartup>-agents`) → pick the workspace.

## 2. Add Incoming Webhooks

App settings → **Features → Incoming Webhooks** → toggle on → **Add New Webhook to Workspace** → pick the channel → **Allow**.

Copy the URL (it's a secret — anyone with it can post to your channel).

## 3. Test it

```sh
curl -X POST -H 'Content-Type: application/json' \
  --data '{"text":"hello from pennyworth setup"}' \
  'https://hooks.slack.com/services/T.../B.../...'
```

Should appear in the channel within a second.

## 4. Store it

Three options:

### Option A — Env var (simplest)

```sh
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...' >> ~/.pennyworthrc
exec $SHELL
```

### Option B — AWS Secrets Manager (recommended for prod)

```sh
aws --profile <admin> secretsmanager create-secret \
  --name alfred/slack-webhook \
  --description "Slack incoming webhook for the agent fleet" \
  --secret-string 'https://hooks.slack.com/services/T.../B.../...' \
  --region us-east-1
```

The framework's default secret ID is `alfred/slack-webhook`. See [AWS setup](/pennyworth/guides/aws/) for the IAM policy your cron-time identity needs.

### Option C — Both

Set `SLACK_WEBHOOK_URL` only when you want to override the AWS-stored value (e.g. testing a rotation).

## 5. Verify in Python

```python
import sys
sys.path.insert(0, "lib")
from agent_runner import slack_post
slack_post("pennyworth setup test", severity="info")
```

You should see the message in your channel.

## Severity routing

`slack_post(text, severity="info" | "warn" | "alert")`. See [Severity routing](/pennyworth/concepts/severity-routing/).

## Optional: bot token (`xoxb-`)

Required when you're ready for channel-topic updates and threaded-reply daily-thread routing. See [Slack setup → Optional: bot token](https://github.com/luminik-io/pennyworth/blob/main/docs/SLACK_SETUP.md#optional-bot-token-xoxb-).

## Optional: app-level token (`xapp-1-`)

Required only for Socket Mode (slash commands, button clicks). See [Slack setup → Optional: app-level token](https://github.com/luminik-io/pennyworth/blob/main/docs/SLACK_SETUP.md#optional-app-level-token-xapp-1-).

## Rotating

If you accidentally paste the URL somewhere it shouldn't be:

1. https://api.slack.com/apps → your app → **Incoming Webhooks** → trash icon on the compromised URL.
2. Add a new webhook to the same channel.
3. Update wherever you stored it (env var or AWS).
4. `rm $HERMES_HOME/state/slack-webhook.cache` so the next firing re-fetches.
