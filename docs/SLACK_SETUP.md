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
- *(Optional)* an **app-level token** (`xapp-1-…`) for Socket Mode if you want Alfred to receive planning messages and thread replies.

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

## Optional: Slack planning listener

Required only if you want Alfred to receive Slack events via Socket Mode. The
planning listener lets trusted users DM or mention Alfred with rough work, and
lets Alfred capture replies in registered plan/report threads. Chat edits
drafts and follow-up context only; implementation still needs the normal
approval gate.

1. Same Slack app → **Settings → Basic Information**.
2. Scroll to **App-Level Tokens** → **Generate Token and Scopes**.
3. Name it (e.g. `socket-mode`), add scope `connections:write`.
4. Copy the token (starts with `xapp-1-`).
5. Store at `alfred/slack-app-token` in AWS the same way as above.

Then enable Socket Mode and events:

1. **Settings → Socket Mode** → toggle **Enable Socket Mode** on.
2. **Features → Event Subscriptions** → toggle **Enable Events** on.
3. Under **Subscribe to bot events**, add:
   - `app_mention`
   - `message.im`
   - `message.channels` if you want public-channel thread replies
   - `message.groups` if you want private-channel thread replies
4. Under **OAuth & Permissions**, add the matching read scopes:
   - `app_mentions:read`
   - `im:history`
   - `channels:history` for public-channel replies
   - `groups:history` for private-channel replies
5. Reinstall the app after changing scopes.

Runtime env:

```sh
SLACK_APP_TOKEN=xapp-1-...
SLACK_BOT_TOKEN=xoxb-...
ALFRED_OPERATOR_SLACK_USER_ID=U0123ABCDEF
ALFRED_TRUSTED_SLACK_USER_IDS=U045TEAM1,U078TEAM2
ALFRED_SLACK_BOT_USER_ID=U0BOTUSERID
```

`ALFRED_OPERATOR_SLACK_USER_ID` is the only user who can approve execution or
change the trusted collaborator list. `ALFRED_TRUSTED_SLACK_USER_IDS` is a
static comma-separated allowlist for collaborators who can discuss plans, revise
drafts, and create planning requests. You can also add local collaborators from
Slack with `trust <@user>` or from the desktop client's Setup tab; those live in
`$ALFRED_HOME/state/slack-trust/trusted-users.json` and are picked up without
restarting the listener.

Run it:

```sh
alfred slack-listener run
```

For a local smoke test without posting:

```sh
alfred slack-listener once payload.json --trusted-user U0123ABCDEF --no-post
```

Safety model:

- Only `ALFRED_OPERATOR_SLACK_USER_ID` and `ALFRED_TRUSTED_SLACK_USER_IDS`
  are allowed to create drafts or amend registered threads.
- Local users added through `trust <@user>` or the desktop client have the same
  planning rights as env-trusted users, but cannot approve execution unless
  they are also the operator.
- If no trusted users are configured, the listener ignores every event.
- Alfred only treats a thread as actionable after it registered the root
  message in `$ALFRED_HOME/state/slack-threads/`.
- Direct Slack intake writes local draft JSON under
  `$ALFRED_HOME/state/planning-drafts/`; it does not file GitHub issues.
- Replies in the draft thread revise that same JSON draft, issue body, spec
  body, readiness score, and revision history.
- Thread feedback is stored under
  `$ALFRED_HOME/state/slack-threads/feedback/` for the next plan or PR pass.
- When memory is enabled, draft creation and revisions recall advisory planning
  memory using `ALFRED_MEMORY_PROVIDERS`; memory never overrides the current
  Slack thread or readiness gates.

## Optional: Slack issue bridge

The bridge is the wire that turns an *approved* planning draft into a labeled
GitHub issue the autonomous fleet (Lucius / Batman) picks up. It is **off by
default**. When enabled, a trusted user can approve a draft directly in its
thread, and Alfred files one issue carrying the pickup label.

**What it does and does not do.** The bridge only runs `gh issue create` with
the configured pickup label. It never runs code, opens worktrees, pushes
branches, or spawns an agent. The created issue then enters the normal queue
and is claimed through every existing gate (claim-lock, spend caps, review,
Batman's multi-repo approval). The bridge reuses that safety machinery instead
of bypassing it.

**Five gates are all required** before an issue is created:

1. The bridge is explicitly enabled with `ALFRED_BRIDGE_ENABLED=1`.
2. A **trusted** Slack user (`ALFRED_OPERATOR_SLACK_USER_ID` /
   `ALFRED_TRUSTED_SLACK_USER_IDS`). A non-trusted user can never trigger it.
3. An **explicit approval token** in a registered draft thread: a configured
   phrase (default `ship it` / `create issue` / `file issue` / `/ship`) or a
   `:white_check_mark:` reaction on the draft. Ambiguous prose is never treated
   as approval -- it just refines the draft as before.
4. A **ready draft**: the saved readiness report has no blocking findings and
   meets `ALFRED_BRIDGE_MIN_READINESS_SCORE` (default `80`).
5. Every target repo is present in the `ALFRED_BRIDGE_REPOS` allowlist.

Enable it by setting these env vars on the listener process:

```sh
ALFRED_BRIDGE_ENABLED=1
# Allowlist of owner/repo slugs the bridge may file against. A draft repo
# outside this list is refused. Required: an empty allowlist files nowhere.
ALFRED_BRIDGE_REPOS=acme-org/api,acme-org/web
# Pickup label the fleet watches for (must match pick_issue()). Default:
ALFRED_BRIDGE_LABEL=agent:implement
# Optional override of the approval phrase list (comma/semicolon separated).
# Keep these action-oriented; avoid casual words like "go".
ALFRED_BRIDGE_APPROVAL_PHRASES=ship it, create issue, file issue, /ship
# Optional minimum saved readiness score before filing. Default: 80.
ALFRED_BRIDGE_MIN_READINESS_SCORE=80
```

To let approval reactions work, also subscribe the Slack app to the
`reaction_added` bot event (under **Event Subscriptions → Subscribe to bot
events**) and add the `reactions:read` OAuth scope.

Safety model:

- Disabled by default: with `ALFRED_BRIDGE_ENABLED` unset, an explicit approval
  is acknowledged but creates nothing.
- An empty `ALFRED_BRIDGE_REPOS` refuses to file anywhere.
- A draft whose repo is not in the allowlist is refused; nothing is created.
- A draft below the readiness threshold is refused with the questions to answer
  next; nothing is created.
- Idempotent: a draft can only be converted once. A second approval reports the
  existing issue instead of creating a duplicate.
- The bridge only creates an issue; it never executes code. The fleet still
  claims the issue through every existing gate before any change ships.

### Running the listener as a service

The bridge runs inside the existing planning listener process, so the bridge
env vars belong in the same launchd plist that runs `alfred slack-listener
run`. The framework's `launchd/_template.plist` renders one job per agent from
`launchd/agents.conf`; add the bridge variables to that job's
`EnvironmentVariables` block (alongside `SLACK_APP_TOKEN`,
`ALFRED_OPERATOR_SLACK_USER_ID`, etc.), since launchd does not interpolate env
vars inside a plist. For a quick local run:

```sh
ALFRED_BRIDGE_ENABLED=1 \
ALFRED_BRIDGE_REPOS=acme-org/api \
alfred slack-listener run
```

## Optional: trusted control commands

When the planning listener is running, a trusted user can also drive the fleet
from chat by **leading a message with a known verb**. These are handled by
`lib/slack_control.py`, separately from planning intake:

| Command | What it does |
|---|---|
| `status` | Fleet health from `alfred status --json` (loaded agents, pauses, locks). |
| `runs` | Recent firings per agent (last-fired plus today's counts). |
| `plans` | Local planning inbox: Batman plans, Slack planning drafts, and captured follow-ups. |
| `plan <id>` | Inspect one local plan or follow-up. Use `plans` to find the id. |
| `draft <id>` | Convert a captured follow-up into a local planning draft. |
| `handled <id>` | Operator-only. Archive a captured follow-up without creating a draft. |
| `memory` / `memories` | Show pending memory candidates and suggested promotions. |
| `remember [repo:] <lesson>` / `memory remember ...` | Queue a reviewable memory candidate from Slack. |
| `memory promote <id>` | Operator-only. Promote a candidate into future recall. |
| `memory reject <id>` | Operator-only. Reject a noisy candidate. |
| `memory harvest` | Preview repeated-failure lessons from the reliability governor. |
| `memory harvest now` | Operator-only. Queue harvested lessons as reviewable candidates. |
| `memory redis` | Check the optional Redis Agent Memory Server bridge. |
| `memory sync` | Preview reviewed-lesson sync to Redis AMS. |
| `memory sync now` | Operator-only. Write reviewed lessons to Redis AMS. |
| `pause <codename>` | Stop scheduled firings for one agent (or `all`). |
| `resume <codename>` | Reverse a pause. |
| `trusted` | Show the operator and trusted Slack users Alfred currently accepts. |
| `trust <@user>` | Operator-only. Add a local Slack collaborator without a listener restart. |
| `untrust <@user>` | Operator-only. Remove a locally trusted Slack collaborator. |
| `help` | List these commands. |

DM Alfred or @-mention it, e.g. `pause lucius` or `status`. Nothing extra to
configure beyond the planning listener; the same trusted-user gate applies.

Safety model:

- **Explicit leading verb only.** A message is a control command only when its
  first whitespace-delimited token is a known verb. Free-form prose ("can you
  pause everything later?") never triggers an action; it falls through to
  planning intake. This is the main guard against an accidental control action.
- **Trusted user only.** The listener gates trust before dispatching, and the
  handler refuses any untrusted control attempt as defense in depth. Trust-list
  mutations are stricter: only `ALFRED_OPERATOR_SLACK_USER_ID` can run
  `trust`/`untrust`.
- **No shell, ever.** `pause`/`resume` run the `alfred` CLI through an explicit
  argv with `shell=False`. The codename is validated against a strict charset
  (`[A-Za-z0-9._-]`, never leading `-`) before it reaches the argv, so it can
  never be read as a flag or inject a second command.
- **Planning actions stay local.** `plans` and `plan <id>` only read local
  state. `draft <id>` writes a local planning draft and archives the captured
  follow-up. `handled <id>` archives the follow-up. None of these commands
  files GitHub issues, starts agents, approves execution, or merges PRs.
- **Memory is reviewable.** `remember ...` and `memory remember ...` queue
  candidates only. They do not enter future prompt context until the operator
  runs `memory promote <id>`.
  `memory harvest` previews repeated-failure lessons before `memory harvest now`
  queues them. A scheduled `memory-harvest.py` job can queue the same
  reviewable candidates automatically and notify Slack only when there is
  something to review. `memory redis` is read-only, and `memory sync` defaults
  to a dry-run preview.
- **Queries are read-only.** `status`, `runs`, and `trusted` only read fleet
  state. `trust` and `untrust` only update the local trust JSON file and never
  run code, call GitHub, or approve a plan.

## Optional: in-thread fleet progress (thread-sync)

When the issue bridge converts an approved draft into a GitHub issue, the
originating Slack thread would normally go quiet while the fleet works. The
thread-sync sweep (`lib/slack_thread_status.py`, `bin/alfred-slack-thread-sync.py`)
closes that loop: for each thread that filed an issue, it reads the issue and
its linked PR read-only and posts **only the new lifecycle states** back into
the thread: claimed, PR opened, CI pass/fail, merged, closed.

It runs two ways, both calling the same tracker:

- **In the listener's idle loop**, on a cadence set by
  `ALFRED_SLACK_THREAD_SYNC_INTERVAL_S` (seconds; default 300; `0` disables the
  in-listener hook).
- **As a standalone sweep** you can run from cron or launchd:

  ```sh
  alfred slack-thread-sync            # post deltas to Slack
  alfred slack-thread-sync --json     # machine-readable summary
  alfred slack-thread-sync --dry-run  # compute deltas, post nothing
  ```

Safety model:

- **Read-only on GitHub.** The only `gh` calls are `issue view` and
  `pr list`/`pr view`. It never edits a label, claims an issue, comments on
  GitHub, or runs code. It is write-only into the thread it already owns.
- **Trust-scoped.** A tracker record is only ever created from the bridge's own
  conversion path, which is already gated on a trusted user.
- **Idempotent.** Each thread advances through an ordered lifecycle and each
  state posts at most once. A sweep with no GitHub change posts nothing.

State lives under `$ALFRED_HOME/state/slack-thread-status/` (one small JSON
record per tracked thread). Override the directory with `--state-root`.

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
