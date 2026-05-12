# AWS setup

Alfred-OS uses AWS for two optional things:

1. **Secrets Manager**: stores the Slack webhook URL and any per-fleet credentials (Sentry tokens, third-party API keys) so they don't live in shell rc files. Resolution is cached on disk for 30 days; AWS is only hit when the cache expires or is missing.
2. **Per-agent IAM**: every scheduled agent that needs AWS access gets its own scoped IAM user with a narrow inline policy. The operator's SSO chain is never used by scheduled agents.

If you don't need either, skip this doc and put `SLACK_WEBHOOK_URL` directly in `~/.alfredrc`. Alfred-OS runs fine without an AWS account.

## Why per-agent IAM

The operator's AWS SSO has admin everywhere. If a scheduled agent inherited that, a runaway prompt could in principle trigger any AWS action. Per-agent IAM caps the blast radius:

- `<your-codename>-cron`: read-only on the agent's specific secrets (e.g. test credentials, webhook URLs).
- `gordon-cron`: read-only on ECS, ALB, CloudWatch logs/metrics, plus the Sentry token secret.
- `alfred-host`: read-only on `alfred/*` secrets (catch-all for fleet-wide config).

Each agent's prompt invokes `aws` with that profile and strips any operator SSO env that might leak in:

```sh
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY \
    -u AWS_SESSION_TOKEN -u AWS_SECURITY_TOKEN \
    AWS_PROFILE=<agent>-cron aws ...
```

The strip-then-set pattern forces the AWS credential chain to use the named profile, not the operator's ambient credentials.

## 1. Create the IAM user

For each agent you want to grant AWS access, do this once (substitute `<your-codename>-cron` with your agent's IAM user name):

```sh
# Pick a profile that already has admin (your SSO chain or root).
AWS_ADMIN_PROFILE="<your-admin-profile>"

aws --profile "$AWS_ADMIN_PROFILE" iam create-user \
  --user-name <your-codename>-cron \
  --tags Key=purpose,Value=alfred-os-agent

aws --profile "$AWS_ADMIN_PROFILE" iam create-access-key \
  --user-name <your-codename>-cron \
  --output json > /tmp/<your-codename>-cron.keys.json

cat /tmp/<your-codename>-cron.keys.json
# Copy the AccessKeyId + SecretAccessKey out of this file.
shred -u /tmp/<your-codename>-cron.keys.json   # don't leave it on disk
```

Append to `~/.aws/credentials`:

```ini
[<your-codename>-cron]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-east-1
```

Confirm:

```sh
aws --profile <your-codename>-cron sts get-caller-identity
```

You should see the user ARN.

## 2. Attach a scoped inline policy

Replace the resources with the actual ARNs your agent needs. Example for an agent that reads two specific secrets:

```sh
cat > /tmp/<your-codename>-cron-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:*:secret:<your/secret/path>-*",
        "arn:aws:secretsmanager:us-east-1:*:secret:<your/webhook/path>-*"
      ]
    }
  ]
}
EOF

aws --profile "$AWS_ADMIN_PROFILE" iam put-user-policy \
  --user-name <your-codename>-cron \
  --policy-name <your-codename>-cron-secrets-readonly \
  --policy-document file:///tmp/<your-codename>-cron-policy.json
```

Common policy templates:

### Slack-webhook reader (most agents)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["secretsmanager:GetSecretValue"],
    "Resource": "arn:aws:secretsmanager:us-east-1:*:secret:alfred/slack-webhook-*"
  }]
}
```

### ECS read-only (Gordon or another monitoring codename)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:ListServices"
    ],
    "Resource": "*"
  }]
}
```

### CloudWatch Logs read (monitoring codename)

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:GetLogEvents",
      "logs:FilterLogEvents"
    ],
    "Resource": "arn:aws:logs:*:*:log-group:/ecs/your-service-*"
  }]
}
```

## 3. Store secrets

Recommended naming convention: `<scope>/<purpose>` with a small handful of stable scopes.

| Path | What it holds |
|---|---|
| `alfred/slack-webhook` | Slack incoming webhook URL |
| `alfred/slack-bot-token` | Slack `xoxb-` token (when ready) |
| `alfred/slack-app-token` | Slack `xapp-1-` token for Socket Mode |
| `alfred/sentry-dsn-agents` | Sentry DSN agent runners post events to |
| `alfred/sentry-api-token` | Sentry API token Gordon uses to query top issues |
| `alfred/iam-<user>-key` | Backup of long-term access keys (key-rotation safety net) |

Create:

```sh
aws --profile "$AWS_ADMIN_PROFILE" secretsmanager create-secret \
  --name alfred/slack-webhook \
  --description "Slack incoming webhook for the agent fleet" \
  --secret-string 'https://hooks.slack.com/services/T.../B.../...........' \
  --region us-east-1
```

Update:

```sh
aws --profile "$AWS_ADMIN_PROFILE" secretsmanager update-secret \
  --secret-id alfred/slack-webhook \
  --secret-string '<new-url>' \
  --region us-east-1

# Force the agents to re-fetch on next firing
rm -f "$HERMES_HOME/state/slack-webhook.cache"
```

Read (verifies `<agent>-cron`'s policy works):

```sh
aws --profile <your-codename>-cron secretsmanager get-secret-value \
  --secret-id alfred/slack-webhook \
  --region us-east-1 \
  --query SecretString --output text
```

## 4. Configure the launchd plist

The rendered plists do not have an `AWS_PROFILE` column. `alfred-init` writes role-specific profile variables such as `ALFRED_HUNTRESS_AWS_PROFILE` and `ALFRED_GORDON_AWS_PROFILE` into `~/.alfredrc`; `bin/agent-launch` loads that file at firing time; the stable role runner then sets `AWS_PROFILE` only around the AWS calls it owns.

For per-agent profiles, the cleanest pattern is to set `AWS_PROFILE` inside the agent's Python runner before any `subprocess.run(["aws", ...])` call:

```python
# In your agent's stable role runner, such as bin/huntress.py:
import os
os.environ["AWS_PROFILE"] = "<your-codename>-cron"
# Strip any leakage from the operator's session:
for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
         "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
    os.environ.pop(k, None)
```

This is what the shipped `bin/gordon.py` runner does when AWS monitoring is enabled.

## 5. Rotate keys

Every 90 days minimum. The flow:

```sh
# 1. Mint a new key
aws --profile "$AWS_ADMIN_PROFILE" iam create-access-key \
  --user-name <your-codename>-cron --output json > /tmp/new.json

# 2. Update ~/.aws/credentials with the new key/secret

# 3. Verify the agent works with the new key
aws --profile <your-codename>-cron sts get-caller-identity

# 4. Delete the old key by ID
aws --profile "$AWS_ADMIN_PROFILE" iam delete-access-key \
  --user-name <your-codename>-cron --access-key-id <old-AKIA-id>

shred -u /tmp/new.json
```

If anything in steps 2-3 goes wrong, the old key is still active. Roll back. Once step 4 completes, the rotation is committed.

## Troubleshooting

**`AccessDeniedException` from a specific action.**
The agent's IAM policy doesn't grant that action on that resource. Check the error message for the exact ARN and action; widen the policy minimally.

**`InvalidClientTokenId: The security token included in the request is invalid.`**
The keys in `~/.aws/credentials` are wrong, expired, or swapped between profiles. Verify with `aws --profile <name> sts get-caller-identity`.

**Operator's SSO env vars are overriding the agent's profile.**
The AWS credential chain prefers env vars over `~/.aws/credentials`. Either run the agent under launchd (no operator env inherited) or strip env vars at the top of the agent runner (see step 4).

**`AccessDeniedException` on `secretsmanager:GetSecretValue` for a secret you can clearly read.**
Check the resource pattern. Secrets get a 6-character suffix on creation (`alfred/slack-webhook-NmY0Gv`), so the policy resource pattern must end with `*` to match. `arn:…:secret:alfred/slack-webhook` (no trailing `*`) won't match.

**`alfred-host` IAM read-only and you need `CreateSecret`.**
That's by design. Use your admin SSO profile to create/update; the scheduled-agent IAM user only reads.
