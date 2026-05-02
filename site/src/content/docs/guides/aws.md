---
title: AWS
description: IAM-per-agent, Secrets Manager naming, scoped policy templates.
---

Pennyworth uses AWS for two optional things: **Secrets Manager** (Slack webhook, Sentry tokens, third-party API keys) and **per-agent IAM** (one scoped IAM identity per cron-spawned agent).

If you don't need either, skip this — put `SLACK_WEBHOOK_URL` in `~/.pennyworthrc` directly. The framework runs fine without an AWS account.

The full guide lives at [`docs/AWS_SETUP.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/AWS_SETUP.md). The highlights:

## Why per-agent IAM

The operator's SSO has admin everywhere. If a cron-spawned agent inherited that, a runaway prompt could in principle trigger any AWS action. Per-agent IAM caps blast radius:

- `huntress-cron` — read-only on staging E2E test secrets + the Slack webhook.
- `oracle-cron` — read-only on ECS, ALB, CloudWatch logs/metrics. No `secretsmanager:*`.
- `gordon-cron` — read-only on ECS describe + the Sentry token.
- `alfred-host` — read-only on `alfred/*` secrets.

The agent's prompt invokes `aws` with `env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN AWS_PROFILE=<agent>-cron aws ...` so the operator's ambient SSO can't leak through.

## Create a scoped IAM user

```sh
AWS_ADMIN_PROFILE="<your-admin>"

aws --profile "$AWS_ADMIN_PROFILE" iam create-user \
  --user-name huntress-cron \
  --tags Key=purpose,Value=pennyworth-agent

aws --profile "$AWS_ADMIN_PROFILE" iam create-access-key \
  --user-name huntress-cron --output json > /tmp/keys.json

# Copy AccessKeyId + SecretAccessKey into ~/.aws/credentials, then:
shred -u /tmp/keys.json
```

`~/.aws/credentials` entry:

```ini
[huntress-cron]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-east-1
```

## Attach a scoped inline policy

```sh
aws --profile "$AWS_ADMIN_PROFILE" iam put-user-policy \
  --user-name huntress-cron \
  --policy-name huntress-cron-secrets-readonly \
  --policy-document file:///tmp/policy.json
```

Policy templates for Slack reader, ECS read-only, CloudWatch logs etc. live in [`docs/AWS_SETUP.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/AWS_SETUP.md#2-attach-a-scoped-inline-policy).

## Secret naming convention

| Path | What it holds |
|---|---|
| `alfred/slack-webhook` | Slack incoming webhook URL |
| `alfred/slack-bot-token` | Slack `xoxb-` token (when ready) |
| `alfred/slack-app-token` | Slack `xapp-1-` token for Socket Mode |
| `alfred/sentry-dsn-agents` | Sentry DSN agent runners post events to |
| `alfred/sentry-api-token` | Sentry API token for query operations |

Note the secret ID prefix `alfred/` is a convention from the reference fleet — adjust to your fleet's naming if you prefer (e.g. `myfleet/slack-webhook`). Override `SLACK_WEBHOOK_SECRET_ID` in `~/.pennyworthrc` to match.

## Key rotation

Every 90 days minimum. Mint new key → update `~/.aws/credentials` → verify with `aws sts get-caller-identity` → delete old key. See the runbook in [`docs/AWS_SETUP.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/AWS_SETUP.md#5-rotate-keys).

## Troubleshooting

- `AccessDeniedException` on a specific action → policy doesn't grant it. Check the error for the exact ARN + action.
- Operator's SSO env vars override the agent's profile → run under launchd (no operator env inherited) or strip env at the top of the runner.
- `AccessDeniedException` on a secret you can clearly read → resource pattern needs `*` suffix to match the 6-char secret ID suffix (`alfred/slack-webhook-NmY0Gv`).
