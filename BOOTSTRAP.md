# Bootstrap

End-to-end setup for someone consuming alfred-os as the framework for their own cron-driven Claude Code fleet on a fresh Mac. Plan for ~60 minutes the first time, most of it on AWS IAM and the Anthropic Claude Code CLI.

The fleet expects to run on a single, always-on macOS host. A Mac Mini works well; an old laptop with the lid kept open works too. This is not a server-class deployment.

> **Looking for a faster start?** [`INSTALL.md`](INSTALL.md) is the from-zero TL;DR (about 30 minutes, mostly automated by `install.sh`). Use BOOTSTRAP for the deeper operations walkthrough that includes per-agent IAM, hermes-agent integration, and troubleshooting.

> **Looking for the rendered docs?** Browse [https://luminik-io.github.io/alfred-os](https://luminik-io.github.io/alfred-os) for the same content with search, dark mode, and cross-linking.

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| macOS 13 or later | `launchd` per-user agents and modern `launchctl bootstrap` semantics | n/a |
| Python 3.11+ | The agent runners and `agent_runner.py` library | `brew install python@3.11` |
| Node via fnm | Frontend pre-push checks; `claude` CLI lives under fnm by default in this repo | `brew install fnm && fnm install --lts` |
| `git` 2.40+ | Worktree commands the agents lean on | `brew install git` |
| `gh` (GitHub CLI) | Every agent uses `gh issue` / `gh pr` | `brew install gh && gh auth login` |
| `aws` (AWS CLI v2) | Huntress, Oracle, the Slack webhook fetch | `brew install awscli` |
| `claude` (Anthropic Claude Code CLI) | The actual code-writing engine | See [Anthropic docs](https://docs.anthropic.com/claude/docs/claude-code) |
| Anthropic Claude Pro or Max subscription | Pays for the agent turns; no API key required | claude.ai/upgrade |

The Claude Pro tier gives a few thousand turns per week against `claude -p`. Max raises the ceiling enough for a continuous Lucius cron at 20-minute cadence plus the rest of the fleet. If the subscription hits its weekly cap mid-firing the agent surfaces a `error_rate_limit` and the global block trips for one hour.

## 1. Clone and pick paths

```sh
git clone https://github.com/<your-org>/<your-fleet-repo>.git ~/code/fleet
cd ~/code/fleet
```

Two environment variables drive every path. Set them in `~/.zshrc` (or your shell rc):

```sh
export HERMES_HOME="$HOME/.hermes"
export WORKSPACE_ROOT="$HOME/code"   # parent dir of your forked product repos
```

`HERMES_HOME` is the runtime root: deployed agent binaries land in `$HERMES_HOME/bin`, the shared library in `$HERMES_HOME/lib`, state in `$HERMES_HOME/state`, and per-firing worktrees in `$HERMES_HOME/worktrees`.

`WORKSPACE_ROOT` is the parent directory of your canonical product checkouts. Lucius and Bane look here for repo CLAUDE.md files and grep targets before they invoke `claude -p`.

All paths in the framework are env-driven via `HERMES_HOME` and `WORKSPACE_ROOT` — no source edits needed. (The earlier note about a `chore/oss-paths` companion PR is no longer relevant; the parametrisation has shipped.)

## 2. AWS setup - one IAM user per cron

The operator's SSO chain is never used by cron. Every AWS-touching agent gets its own scoped IAM user.

Create the user (one-time, in the AWS console or via CloudFormation), then write its access keys to `~/.aws/credentials` under a named profile. Example for Huntress:

```ini
# ~/.aws/credentials
[huntress-cron]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-east-1
```

Inline IAM policy (Huntress reads only the staging E2E test credentials):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": [
        "arn:aws:secretsmanager:us-east-1:*:secret:e2e/staging/test-user-*",
        "arn:aws:secretsmanager:us-east-1:*:secret:slack/staging/internal-webhook-url-*"
      ]
    }
  ]
}
```

Oracle's policy is similar but read-only across ECS, ALB, and CloudWatch logs/metrics - no `secretsmanager:*`. Adjust per agent.

Each agent's prompt invokes `aws` like this so the credentials chain prefers the dedicated profile over any ambient SSO env vars:

```sh
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_SECURITY_TOKEN AWS_PROFILE=huntress-cron aws ...
```

The `env -u` calls strip any operator SSO leakage; `AWS_PROFILE` then forces the scoped profile.

## 3. Slack webhook

Create a Slack incoming webhook for the channel where the fleet should report (we use `#your-fleet-channel`). The framework's `slack_post()` resolves the URL via env → 7-day disk cache → AWS Secrets Manager — pick whichever you want.

**Simplest** (env var):

```sh
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...' >> ~/.alfredrc
```

**Recommended for prod** (AWS Secrets Manager, default secret ID `alfred/slack-webhook`):

```sh
aws --profile <admin-profile> secretsmanager create-secret \
  --name alfred/slack-webhook \
  --secret-string "https://hooks.slack.com/services/T.../B.../..." \
  --region us-east-1
```

Override `SLACK_WEBHOOK_SECRET_ID` in `~/.alfredrc` if you keep secrets under a different prefix.

The runtime caches the webhook at `$HERMES_HOME/state/slack-webhook.cache` with a 7-day TTL, so a slow Secrets Manager call does not stall every Slack post. See `slack_post()` in [`lib/agent_runner.py`](lib/agent_runner.py) and the full walkthrough in [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md) (which also covers the optional bot-token + app-level-token paths).

## 4. Hermes-agent (cron + canon + ACP)

alfred-os-driven fleets typically use [hermes-agent](https://github.com/NousResearch/hermes-agent) for three things:

- **cron** - the higher-level scheduling layer. The launchd plists in `launchd/` are the runtime, but the prompts in `agents/<dept>/prompts/` are inlined into Hermes cron entries via `hermes cron edit` so they can be re-synced from a single source.
- **canon** - shared writing-style and voice rules pulled in by every agent prompt.
- **ACP** (Agent Control Protocol) - the `#your-fleet-channel` Slack thread response surface (yes/no buttons, structured replies). Used by the personal-assistant and content departments more than engineering.

Install hermes-agent per its README, then verify:

```sh
hermes --version
hermes cron list
```

Engineering agents in this repo run via launchd and do **not** require Hermes for their core loop. The non-engineering departments (content, sales, personal-assistant) do use it as their scheduler.

## 5. Deploy

```sh
bash deploy.sh
```

Expected output:

```
[deploy] copying lib/
[deploy] copying bin/ (Python + shell)
[deploy] copying launchd plists
  - my.fleet.lucius
  - my.fleet.nightwing
  - my.fleet.rasalghul
  - my.fleet.bane
  - my.fleet.huntress
  ...
[deploy] active jobs:
-	0	my.fleet.lucius
-	0	my.fleet.nightwing
...
```

The script copies `lib/agent_runner.py` to `$HERMES_HOME/lib`, every regular file in `bin/` to `$HERMES_HOME/bin`, renders `launchd/_template.plist` for each entry in `launchd/agents.conf` and copies the result to `~/Library/LaunchAgents`, then re-loads each plist via `launchctl bootout` + `launchctl bootstrap`.

## 6. Verify the host with `doctor.sh`

Before firing any agent, sanity-check that every agent's preflight passes on this host:

```sh
bash bin/doctor.sh
```

Expected output:

```
doctor: checking agents under /Users/<you>/.hermes/bin
        HERMES_HOME=/Users/<you>/.hermes
        WORKSPACE_ROOT=/Users/<you>/Claude_Workspace

  agent-cleanup                  ✅ ok
  agent-morning-brief            ✅ ok
  my-deps-bot                    ✅ ok
  ...
  drake                          ✅ ok
  huntress                       ✅ ok
  lucius                         ✅ ok
  ...

doctor: 14 passed, 0 failed
```

Any failure prints the `[<AGENT>-PREFLIGHT-FAILED]` block naming each gap (missing env var, missing CLI binary, dead AWS profile, expired `gh auth`, missing repo checkout). Fix and re-run until you see 14 passed.

`HERMES_DOCTOR=1` is the env var the agents themselves check; `doctor.sh` sets it and invokes every agent. You can also run a single agent in doctor mode:

```sh
HERMES_DOCTOR=1 ~/.hermes/bin/lucius.py
# [LUCIUS-DOCTOR-OK]
```

This is also the right command after you rotate AWS keys, refresh `aws sso login`, swap Claude account via `hermes-claude swap`, or change anything in IAM policy: re-run `doctor.sh` and confirm 14 passed.

## 7. First firing - dry run

The plists ship with `RunAtLoad` set to `false`, so deploying does not immediately fire any agent. To test a single agent without it shipping a PR:

1. Pick an agent and read its top-of-file constants. Lucius, for example, is gated by `agent:implement` issues - if no repo has one open, the firing exits `[SILENT]`.
2. Fire it by hand:
   ```sh
   launchctl kickstart -k gui/$(id -u)/my.fleet.lucius
   ```
3. Tail the logs:
   ```sh
   tail -f /tmp/my.fleet.lucius.stdout /tmp/my.fleet.lucius.stderr
   ```
4. To inspect what `claude -p` actually saw, look in `/tmp/lucius-debug-<issue>-<ts>/`. The runner persists the prompt and raw JSON result there for every Lucius run.

If you want to verify the wiring without any code changes landing, point Lucius at a repo with no `agent:implement` issues and confirm it exits `[SILENT]`. Or label one issue with `agent:implement` and a label like `lucius-attempt-3` - Lucius will skip it and re-label `needs:human-scope` instead of running.

To pause an agent:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```

To resume:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```

## 8. Troubleshooting

**`claude: command not found` in the launchd log.** The plist's `PATH` does not include the fnm-managed Node bin (or wherever your `claude` lives). Either add it to the plist's `EnvironmentVariables` block (the template's `__PATH__` token is rendered from your shell PATH at deploy time), or set `CLAUDE_BIN=<absolute-path>` in `~/.alfredrc` so `agent_runner.claude_invoke` uses it directly. `which claude` shows the path.

**Slack posts silently fail.** The webhook cache may be stale (URL rotated) or AWS Secrets Manager may be unreachable. Run `aws secretsmanager get-secret-value --secret-id slack/staging/internal-webhook-url --region us-east-1` against the agent's profile (`AWS_PROFILE=huntress-cron`, etc.) and confirm it returns the URL. To force a refresh, delete `~/.hermes/state/slack-webhook.cache`.

**`AccessDeniedException` from AWS.** The agent is using the wrong profile. Confirm `~/.aws/credentials` has the named profile and that the agent's prompt uses `env -u ... AWS_PROFILE=<agent>-cron`. The operator's SSO env vars beat profiles in the AWS credential chain - the `env -u` strips them.

**Plist not loading.** `launchctl bootstrap` is silent on success and noisy on failure. Run it manually with the full path:
```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```
Common errors: file not executable (`chmod +x`), wrong shebang (`#!/usr/bin/env python3` requires Python on PATH), socket address conflict (label already loaded - `bootout` first).

**Claude rate limit (`error_rate_limit` or `error_budget`).** The whole fleet is now in a global block for one hour. Inspect `~/.hermes/state/global-blocked-until.json`. Wait, or delete the file to clear the block manually. Then look at why one agent burned the weekly cap - usually a runaway loop on a too-large issue. Lucius's hard cap is 5000 turns/day; tighten if needed.

**`hermes cron edit` did not take effect.** The cron runs the **inlined** prompt, not the file at `agents/.../prompts/<name>.md`. After editing a prompt you must re-sync:
```sh
hermes cron edit <cron-id> --prompt "$(cat agents/engineering/prompts/lucius-feature-dev.md)"
```

**Spend caps tripping too early.** Each agent's cap lives in its own bin (e.g. `lucius.py` line ~122). Adjust the constant and re-deploy.

## Where to go next

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains why the fleet has this shape. Read it before designing a new codename.
