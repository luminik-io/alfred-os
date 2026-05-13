# Slack setup

Alfred posts simple agent reports via an **incoming webhook**. Agents that
use `lib/slack_format.py` can also post Block Kit firing threads via an
optional Slack bot token. This doc covers both paths.

If you already have a webhook URL, the framework needs `SLACK_WEBHOOK_URL` in your environment (or in AWS Secrets Manager; see below). Skip to [§ Wiring it up](#wiring-it-up).

## At the end

- A Slack app named `<your-fleet>-bot` in the workspace where you want the channel.
- An **incoming webhook URL** scoped to one channel (e.g. `#fleet`, `#dev-bots`, `#engineering-agents`).
- *(Optional)* a **bot token** (`xoxb-...`) for Block Kit firing threads and
  other Web API calls webhooks cannot do.
- *(Optional)* an **app-level token** (`xapp-1-…`) for Socket Mode if you want to receive interactive messages back.

The webhook URL is enough for plain `slack_post()` messages. The bot token is
used today by `firing_thread_root`, `firing_thread_reply`, and
`firing_thread_close` in `lib/slack_format.py`.

## 1. Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**.
2. Name it (e.g. `alfred-bot`, `alfred-fleet`, `<yourstartup>-agents`).
3. Pick the workspace that owns the channel you want to post to.
4. Click **Create App**.

You're now on the app's settings page. Keep this tab open.

## 2. Add Incoming Webhooks

1. Sidebar → **Features → Incoming Webhooks**.
2. Toggle **Activate Incoming Webhooks** to **On**.
3. Scroll to the bottom → **Add New Webhook to Workspace**.
4. Pick the channel. Locked once chosen; the webhook can only post to that one channel.
5. Click **Allow**.

You'll be redirected back. You'll see a new entry in the **Webhook URLs for Your Workspace** table:

```
https://hooks.slack.com/services/T.../B.../...........
```

**This URL is a secret.** Anyone with it can post arbitrary messages to your channel as the app. Treat it like a password.

## 3. Test the webhook

```sh
curl -X POST -H 'Content-Type: application/json' \
  --data '{"text":"hello from Alfred setup"}' \
  'https://hooks.slack.com/services/T.../B.../...........'
```

You should see the message in the channel within a second. If not, check the URL: the `T...`/`B...`/last segment must match exactly.

## 4. Store the webhook

You have three options. Pick one.

### Option A: Env var (simplest)

Append to `~/.alfredrc`:

```sh
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...........
```

Reload your shell (`exec $SHELL`). `slack_post()` reads this directly.

**Pros**: zero AWS dependency, easiest to rotate (edit one file).
**Cons**: lives in plaintext on the operator's home directory.

### Option B: AWS Secrets Manager (recommended for prod)

Store the URL as a secret and let `slack_post()` resolve it via the env -> cache -> AWS chain (cached at `$ALFRED_HOME/state/slack-webhook.cache` for 30 days):

```sh
aws --profile <admin-profile> secretsmanager create-secret \
  --name alfred/slack-webhook \
  --description "Slack incoming webhook for the agent fleet" \
  --secret-string 'https://hooks.slack.com/services/T.../B.../...........' \
  --region us-east-1
```

The default secret ID is `alfred/slack-webhook`. Override via `SLACK_WEBHOOK_SECRET_ID` if you keep secrets under a different prefix.

You also need an IAM identity that scheduled agents use, with `secretsmanager:GetSecretValue` on `arn:…:secret:alfred/slack-webhook-*`. See [`AWS_SETUP.md`](AWS_SETUP.md).

**Pros**: rotation is `aws secretsmanager update-secret` + `rm ~/.alfred/state/slack-webhook.cache`, value never lives in shell rc, audit logs on every fetch.
**Cons**: requires AWS account.

### Option C: Both (env var as override, AWS as default)

Set `SLACK_WEBHOOK_URL` only when you want to override the AWS-stored value (e.g. testing a webhook rotation). Leave it unset normally and let AWS resolution handle it.

## 5. Verify in Alfred

```sh
python3 - <<'PY'
import sys
sys.path.insert(0, "lib")
from agent_runner import slack_post
ok = slack_post("Alfred setup test: channel migration confirmed")
print("posted:", ok)
PY
```

You should see the message in your channel and `posted: True` on stdout.

## 6. Severity ladder (optional but recommended)

`slack_post()` accepts a `severity=` keyword: `info` (default), `warn`, `alert`. The latter two prefix and ping respectively. See [`STATE_MACHINE.md`](STATE_MACHINE.md) for the design rationale and [`agent_runner.py`](../lib/agent_runner.py) for the docstring.

Quick demo:

```python
slack_post("Lucius shipped #42", severity="info")        # plain
slack_post("Lucius hit max-turns on #42", severity="warn")   # ⚠️ prefix
slack_post("Staging deploy drifted from main", severity="alert")  # 🚨 + <!here>
```

## Optional: bot token (`xoxb-`)

Required when you want to:

- Post Block Kit firing roots and threaded replies via `lib/slack_format.py`.
- Update the channel topic from your fleet's recap script.
- React to messages programmatically.

Webhooks cannot do any of those. They're write-only, single-channel, and don't expose the full Web API. A bot token (`xoxb-…`) does.

To provision:

1. Same Slack app → **Features → OAuth & Permissions**.
2. Under **Scopes → Bot Token Scopes**, add:
   - `chat:write`: post messages
   - `channels:read`: resolve public channels when needed
   - `groups:read`: resolve private channels when needed
   - `channels:manage`: only if your fleet edits channel topics
3. Click **Install to Workspace** at the top of the page → **Allow**.
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`).
5. Store it in AWS Secrets Manager:
   ```sh
   aws --profile <admin-profile> secretsmanager create-secret \
     --name alfred/slack-bot-token \
     --description "Slack bot token for the agent fleet (chat:write + channels:manage)" \
     --secret-string 'xoxb-...' --region us-east-1
   ```

Wire the token through env or AWS:

```sh
# simplest path
SLACK_BOT_TOKEN=xoxb-...
SLACK_HOME_CHANNEL=alfred

# AWS path used when SLACK_BOT_TOKEN is unset
SLACK_BOT_TOKEN_SECRET_ID=alfred/slack-bot-token
SLACK_BOT_TOKEN_SECRET_REGION=us-east-1
```

`SLACK_HOME_CHANNEL` can be a channel name or ID. If posting by name fails in
your workspace, use the channel ID (`C...`) from Slack's channel details.

## Optional: app-level token (`xapp-1-`)

Required only if you want to receive Slack events (slash commands, interactive button clicks) via Socket Mode, a backchannel that doesn't need a public webhook.

1. Same Slack app → **Settings → Basic Information**.
2. Scroll to **App-Level Tokens** → **Generate Token and Scopes**.
3. Name it (e.g. `socket-mode`), add scope `connections:write`.
4. Copy the token (starts with `xapp-1-`).
5. Store at `alfred/slack-app-token` in AWS the same way as above.

## Rotating a webhook

Do this when you accidentally paste the URL somewhere it shouldn't be (chat, screenshot, public PR description).

1. https://api.slack.com/apps → your app → **Incoming Webhooks**.
2. Click the trash icon next to the compromised webhook → confirm.
3. Add a new webhook to the same channel → copy the URL.
4. Update wherever it's stored:

   ```sh
   # Env var path:
   sed -i '' 's|^SLACK_WEBHOOK_URL=.*|SLACK_WEBHOOK_URL=<new-url>|' ~/.alfredrc

   # AWS Secrets path:
   aws --profile <admin> secretsmanager update-secret \
     --secret-id alfred/slack-webhook \
     --secret-string '<new-url>' --region us-east-1
   rm -f $ALFRED_HOME/state/slack-webhook.cache
   ```

The next agent firing fetches the new value.

## Wiring it up

Once the webhook is stored, every agent that imports `slack_post` from `agent_runner` posts to your channel. No per-agent Slack config; the framework resolves the URL once and caches it.

Common gotchas:

- **Posts go nowhere.** Cache might be stale (URL rotated). `rm $ALFRED_HOME/state/slack-webhook.cache` and retry.
- **Posts go to the wrong channel.** A webhook is locked to a single channel at creation time. Mint a new webhook and rotate.
- **Posts come from a generic name like "incoming-webhook".** App settings → **Basic Information → Display Information** → set name + icon. Applies to all channels the app posts to.
