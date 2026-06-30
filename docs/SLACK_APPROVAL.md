# Slack approval gate

Alfred agents can pause in plan mode and wait for an explicit go/no-go from
the configured approver before writing any code. The gate lives in
[`lib/slack_approval.py`](../lib/slack_approval.py). It posts the plan to a
Slack channel, polls reactions on that one message, and resolves only when
the configured approver reacts with an approve or reject emoji.

This guide walks through the full setup. If you already have a Slack bot
token wired up via [`docs/SLACK_SETUP.md`](SLACK_SETUP.md), skip to
[Configuring the approver](#3-configure-the-approver).

## Why reaction-based

Reactions beat reply-text on two axes that matter for autonomous fleets:

- **Unambiguous semantics.** A reaction is a discrete event. Free-text
  replies invite ambiguity ("looks good but change X"), which an agent
  cannot disambiguate without a second LLM call.
- **Configured-approver enforcement.** Each reaction carries a Slack user id,
  so the gate can reject any reaction from a teammate without parsing.
  A reply has the same author field but asking the approver to
  type "approved" every time adds friction.

If you need richer feedback than approve/reject, use the plan thread as
the planning surface. Alfred now does this for Batman plans: the reaction
remains the hard gate, while trusted thread replies are acknowledged live
with a revised execution preview and then carried into child issues on
approval. Batman understands common planning commands in those replies:

```text
acceptance: reviewer can verify the issue link in the PR body
test: add coverage for the approval thread parser
add repo: my-org/mobile
remove repo: my-org/site
question: should this wait for a clearer spec?
```

Plain-language replies are still captured as approver notes. Alfred posts
a concise plan-revision reply in-thread. On approval, repo add/remove
replies amend execution scope before children are filed; the interpreted
notes are also carried into child issues and repo-worker prompts. Replies
from trusted teammates can amend the plan, but only the configured
approver's reaction can approve or reject it. Any explicit `question:`
reply keeps the plan from executing until it is resolved.

## 1. Create the Slack app

Visit https://api.slack.com/apps and click **Create New App** ->
**From scratch**. Name it (e.g. `acme-agents`) and install it into your
workspace.

### Bot scopes

Under **OAuth & Permissions -> Scopes**, add the following **Bot Token
Scopes**:

| Scope | Why |
|---|---|
| `chat:write` | Post the plan message that the gate polls. |
| `reactions:read` | Read which users reacted with which emoji. |
| `channels:read` | Resolve channel names to channel ids. |
| `groups:read` | Same, for private channels. |
| `channels:history` | Read approver replies from public-channel plan threads. |
| `groups:history` | Read approver replies from private-channel plan threads. |
| `im:read` | Same, if the approver wants approvals in DM. |
| `users:read` | Optional. Useful for logging the approver's display name. |

The `reactions:write` scope is **not** required. The bot never reacts on
the approver's behalf.

A minimal manifest snippet to paste under **Features -> App Manifest**:

```yaml
display_information:
  name: acme-agents
features:
  bot_user:
    display_name: acme-agents
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - reactions:read
      - channels:read
      - groups:read
      - channels:history
      - groups:history
      - im:read
      - users:read
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
```

`socket_mode_enabled: false` is fine for approval-only installs because the
gate polls reactions through the Web API. Set it to `true` only when you also
run the always-on planning listener from [`SLACK_SETUP.md`](SLACK_SETUP.md).
The listener captures trusted thread replies and Slack intake drafts; it does
not make chat text an approval signal.

Install the app to the workspace and copy the **Bot User OAuth Token**
(begins with `xoxb-`).

## 2. Store the bot token

The gate resolves the token via a strategy chain: env var -> AWS Secrets
Manager (opt-in) -> on-disk cache. Pick whichever matches your ops:

### Option A: env var

```sh
echo 'SLACK_BOT_TOKEN=xoxb-...' >> $ALFRED_HOME/.env
```

### Option B: AWS Secrets Manager

```sh
aws --profile <admin> secretsmanager create-secret \
  --name alfred/slack-bot-token \
  --description "Slack bot token for the Alfred approval gate" \
  --secret-string 'xoxb-...' \
  --region us-east-1

# In your launchd plist or shell rc
export ALFRED_SECRETS_BACKEND=aws
```

The secret id is configurable via `ALFRED_SLACK_BOT_TOKEN_SECRET_ID`
(default `alfred/slack-bot-token`) and the region via
`ALFRED_SLACK_BOT_TOKEN_SECRET_REGION` (default `us-east-1`).

`boto3` ships with the standard Alfred package. If you build a stripped-down
environment by hand, install it with `pip install boto3`.

### Option C: pre-seeded disk cache

The gate also reads `$ALFRED_HOME/state/slack-bot-token.cache` as a last
resort. This path is intended for short-lived caches that AWS resolvers
populate, but it can also be pre-seeded by hand for fully offline setups.
Override with `ALFRED_SLACK_BOT_TOKEN_CACHE`.

## 3. Configure the approver

The gate accepts reactions from **exactly one** Slack user id. Find your
own id at https://app.slack.com/client (profile menu -> **Copy member
ID**); approver ids look like `U0123ABCDEF`.

```sh
export ALFRED_OPERATOR_SLACK_USER_ID=U0123ABCDEF
```

If this variable is unset the gate refuses to start; we never silently
accept any reactor.

Optional trusted feedback users can discuss and amend the plan in the same
thread without getting approval authority:

```sh
export ALFRED_TRUSTED_SLACK_USER_IDS=U045TEAM1,U078TEAM2
```

## 4. Check `slack-sdk`

The default Slack client wraps `slack_sdk.WebClient`. `slack-sdk` ships with
the standard Alfred package. If you build a stripped-down environment by hand,
install it directly:

```sh
pip install slack-sdk
```

The gate raises a clear `ImportError` if you try to build the default
client without `slack-sdk` installed.

## 5. Wire it into your agent

```python
from slack_approval import (
    SlackApproval,
    default_slack_client,
    operator_user_id_from_env,
)

operator = operator_user_id_from_env()
if not operator:
    raise RuntimeError("ALFRED_OPERATOR_SLACK_USER_ID must be set")

client = default_slack_client()
post = client.chat_postMessage(
    channel="your-fleet-channel",
    text=plan_text,
    unfurl_links=False,
    unfurl_media=False,
)
gate = SlackApproval(client, operator_user_id=operator)
result = gate.await_approval(
    channel=post["channel"],
    message_ts=post["ts"],
    timeout_s=86400,
)
if result.approved:
    for item in result.feedback:
        print(f"approver amendment: {item.text}")
    proceed()
elif result.rejected:
    abort_with_message(f"Configured approver rejected: see {post['ts']}")
else:
    raise RuntimeError(f"approval did not resolve: {result.verdict} ({result.detail})")
```

## Environment variable reference

| Variable | Purpose | Default |
|---|---|---|
| `ALFRED_OPERATOR_SLACK_USER_ID` | Slack user id whose reactions are the only ones that count | (required) |
| `ALFRED_TRUSTED_SLACK_USER_IDS` | Comma-separated Slack user ids whose thread replies can amend plans | configured approver only |
| `SLACK_BOT_TOKEN` | Bot token; used directly when set | unset |
| `SLACK_APP_TOKEN` / `ALFRED_SLACK_APP_TOKEN` | App-level Socket Mode token for the optional planning listener | unset |
| `ALFRED_SLACK_BOT_USER_ID` | Bot user id; listener ignores its own messages | unset |
| `ALFRED_SECRETS_BACKEND` | Set to `aws` to enable the AWS Secrets Manager resolver | unset (disabled) |
| `ALFRED_SLACK_BOT_TOKEN_SECRET_ID` | Secret id used by the AWS resolver | `alfred/slack-bot-token` |
| `ALFRED_SLACK_BOT_TOKEN_SECRET_REGION` | AWS region for the secret | `us-east-1` |
| `ALFRED_SLACK_BOT_TOKEN_CACHE` | Path to the disk-cache fallback file | `$ALFRED_HOME/state/slack-bot-token.cache` |
| `ALFRED_HOME` | Root for the on-disk cache (only used if the explicit cache path is unset) | unset |

## Fallback chain ordering

1. **`SLACK_BOT_TOKEN`** env var.
2. **AWS Secrets Manager**, gated on `ALFRED_SECRETS_BACKEND=aws`.
3. **Disk cache** at `$ALFRED_HOME/state/slack-bot-token.cache` (or the
   override path). Stale-tolerant: a possibly-rotated token is preferable
   to no token at all; the gate degrades to `transport-unavailable` if
   the API rejects it.

If every strategy returns `None`, the gate raises at startup so the
firing fails loud instead of polling forever.

## Plan-mode label transition

Agents that integrate the gate flip the issue label to
[`agent:plan-pending-approval`](../docs/STATE_MACHINE.md) before posting
the plan and clear it after the verdict resolves. See the
[issue claim state machine](../site/src/content/docs/concepts/state-machine.md)
for how this label fits the rest of the lifecycle.

## Configured-approver check semantics

- A reaction from **anyone other than** `ALFRED_OPERATOR_SLACK_USER_ID`
  is ignored. The gate keeps polling.
- Slack listener messages from anyone outside `ALFRED_OPERATOR_SLACK_USER_ID`
  and `ALFRED_TRUSTED_SLACK_USER_IDS` are ignored before any draft or feedback
  file is written.
- The check is by Slack user id, not display name; renaming the
  approver's profile does not affect approval.
- Removing a reaction does **not** rewind a verdict. Once approval (or
  rejection) is returned, the verdict is final for that polling cycle.
  Future cycles see the current reaction set fresh.

## Timeout and transport-down semantics

- `timeout_s` (default 900s = 15min) bounds the wall-clock wait.
  `APPROVAL_TIMEOUT` is returned if the configured approver does not react within
  the window. For overnight gates, pass an explicit longer value (e.g.
  `timeout_s=86400` for 24h) as the examples above do.
- After five consecutive `reactions.get` failures, the gate returns
  `APPROVAL_TRANSPORT_DOWN`. Likely causes: rotated token, deleted plan
  message, removed `reactions:read` scope, or a network outage longer
  than ~2.5 minutes.
- Both outcomes are non-approvals; agents should refuse to write code on
  either.

## Testing the gate

The whole module is built around a `SlackClient` `Protocol`, so tests
inject a fake without touching the network. See
[`tests/test_slack_approval.py`](../tests/test_slack_approval.py) for
the FakeSlackClient pattern.
