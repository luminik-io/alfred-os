# Bootstrap

End-to-end setup for consuming alfred-os as the framework for your own launchd-managed Claude Code fleet on a fresh Mac. Plan ~60 minutes the first time, most of it on AWS IAM and the Anthropic Claude Code CLI.

The fleet runs on a single always-on macOS host. A Mac Mini works. An old laptop with the lid open works. Not a server-class deployment.

> Want the faster path? [`INSTALL.md`](INSTALL.md) is the from-zero TL;DR (~30 minutes, mostly automated by `install.sh`). Use BOOTSTRAP for per-agent IAM, hermes-agent integration, and troubleshooting.

> Rendered docs: [https://luminik-io.github.io/alfred-os](https://luminik-io.github.io/alfred-os).

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| macOS 13 or later | `launchd` per-user agents and modern `launchctl bootstrap` semantics | n/a |
| Python 3.11+ | The agent runners and `agent_runner.py` library | `brew install python@3.11` |
| Node via fnm | Frontend pre-push checks; `claude` CLI lives under fnm by default in this repo | `brew install fnm && fnm install --lts` |
| `git` 2.40+ | Worktree commands the agents lean on | `brew install git` |
| `gh` (GitHub CLI) | Every agent uses `gh issue` / `gh pr` | `brew install gh && gh auth login` |
| `aws` (AWS CLI v2) | AWS-touching agents and the Slack webhook fetch | `brew install awscli` |
| `claude` (Anthropic Claude Code CLI) | The actual code-writing engine | See [Anthropic docs](https://docs.anthropic.com/claude/docs/claude-code) |
| Anthropic Claude Pro or Max subscription | Pays for agent turns; no API key required | claude.ai/upgrade |

Pro gives a few thousand turns per week against `claude -p`. Max raises the ceiling enough for a continuous Lucius cron at 20-minute cadence plus the rest of the fleet. If the subscription hits its weekly cap mid-firing the agent surfaces `error_rate_limit` and the global block trips for one hour.

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

`HERMES_HOME` is the runtime root: deployed agent binaries land in `$HERMES_HOME/bin`, the shared library in `$HERMES_HOME/lib`, state in `$HERMES_HOME/state`, per-firing worktrees in `$HERMES_HOME/worktrees`.

`WORKSPACE_ROOT` is the parent directory of your canonical product checkouts. Lucius and Bane look here for repo CLAUDE.md files and grep targets before invoking `claude -p`.

All framework paths are env-driven via `HERMES_HOME` and `WORKSPACE_ROOT`. No source edits needed.

## 2. AWS setup: one IAM user per cron

The operator's SSO chain is never used by cron. Every AWS-touching agent gets its own scoped IAM user.

Create the user (one-time, in the AWS console or via CloudFormation), then write its access keys to `~/.aws/credentials` under a named profile. Example for an agent codenamed `<your-cron-iam-user>`:

```ini
# ~/.aws/credentials
[<your-cron-iam-user>]
aws_access_key_id = AKIA...
aws_secret_access_key = ...
region = us-east-1
```

Inline IAM policy (example: read only specific secrets the agent needs):

```json
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
```

A read-only-monitoring agent's policy will look different, e.g. ECS, ALB, and CloudWatch read perms with no `secretsmanager:*`. Adjust per agent.

Each agent's prompt invokes `aws` like this so the credentials chain prefers the dedicated profile over any ambient SSO env vars:

```sh
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_SECURITY_TOKEN AWS_PROFILE=<your-cron-iam-user> aws ...
```

The `env -u` calls strip any operator SSO leakage; `AWS_PROFILE` then forces the scoped profile.

## 3. Slack webhook

Create a Slack incoming webhook for the channel where the fleet should report (we use `#your-fleet-channel`). The framework's `slack_post()` resolves the URL via env → 7-day disk cache → AWS Secrets Manager. Pick whichever you want.

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

## 4. Hermes-agent (canon + optional scheduler + ACP)

Alfred-OS-driven fleets typically use [hermes-agent](https://github.com/NousResearch/hermes-agent) for three things:

- **optional scheduler**: non-engineering departments may still use Hermes cron. The engineering fleet in this repo runs via launchd.
- **canon**: shared writing-style and voice rules pulled in by every agent prompt.
- **ACP** (Agent Control Protocol): the `#your-fleet-channel` Slack thread response surface (yes/no buttons, structured replies). Used by the personal-assistant and content departments more than engineering.

Install hermes-agent per its README, then verify:

```sh
hermes --version
```

Engineering agents in this repo run via launchd and do **not** require Hermes for their core loop. The non-engineering departments (content, sales, personal-assistant) use it as their scheduler.

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
  - my.fleet.nightowl
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
  nightowl                       ✅ ok
  lucius                         ✅ ok
  ...

doctor: 14 passed, 0 failed
```

Any failure prints the `[<AGENT>-PREFLIGHT-FAILED]` block naming each gap: missing env var, missing CLI binary, dead AWS profile, expired `gh auth`, missing repo checkout. Fix and re-run until you see 14 passed.

`HERMES_DOCTOR=1` is the env var the agents themselves check; `doctor.sh` sets it and invokes every agent. You can also run a single agent in doctor mode:

```sh
HERMES_DOCTOR=1 ~/.hermes/bin/lucius.py
# [LUCIUS-DOCTOR-OK]
```

This is also the right command after you rotate AWS keys, refresh `aws sso login`, swap Claude account via `hermes-claude swap`, or change anything in IAM policy: re-run `doctor.sh` and confirm 14 passed.

## 7. First firing: dry run

The plists ship with `RunAtLoad = false`, so deploying does not immediately fire any agent. To test a single agent without it shipping a PR:

1. Pick an agent and read its top-of-file constants. Lucius, for example, is gated by `agent:implement` issues. If no repo has one open, the firing exits `[SILENT]`.
2. Fire it by hand:
   ```sh
   launchctl kickstart -k gui/$(id -u)/my.fleet.lucius
   ```
3. Tail the logs:
   ```sh
   tail -f /tmp/my.fleet.lucius.stdout /tmp/my.fleet.lucius.stderr
   ```
4. To inspect what `claude -p` actually saw, look in `/tmp/lucius-debug-<issue>-<ts>/`. The runner persists the prompt and raw JSON result there for every Lucius run.

To verify wiring without code landing, point Lucius at a repo with no `agent:implement` issues and confirm it exits `[SILENT]`. Or label one issue with `agent:implement` and `lucius-attempt-3`. Lucius will skip it and re-label `needs:human-scope` instead of running.

To pause an agent:

```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```

To resume:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```

## 8. Troubleshooting

**`claude: command not found` in the launchd log.** The plist's `PATH` does not include the fnm-managed Node bin (or wherever your `claude` lives). Set `CLAUDE_BIN=<absolute-path>` in `~/.alfredrc`, or expose the binary through a stable directory already rendered into launchd PATH, such as `~/.local/bin`. `which claude` shows the path.

**`codex: command not found` in the launchd log.** Rerun `deploy.sh` after installing Codex. If `codex` is visible in your interactive shell, deploy links it into `~/.local/bin/codex`, which the renderer adds to launchd PATH. Otherwise set `CODEX_BIN=<absolute-path>` in `~/.alfredrc`.

**Slack posts silently fail.** The webhook cache may be stale (URL rotated) or AWS Secrets Manager may be unreachable. Run `aws secretsmanager get-secret-value --secret-id <your/webhook/path> --region us-east-1` against the agent's profile (`AWS_PROFILE=<your-cron-iam-user>`, etc.) and confirm it returns the URL. To force a refresh, delete `~/.hermes/state/slack-webhook.cache`.

**`AccessDeniedException` from AWS.** The agent is using the wrong profile. Confirm `~/.aws/credentials` has the named profile and that the agent's prompt uses `env -u ... AWS_PROFILE=<your-cron-iam-user>`. The operator's SSO env vars beat profiles in the AWS credential chain. The `env -u` strips them.

**Plist not loading.** `launchctl bootstrap` is silent on success and noisy on failure. Run it manually with the full path:
```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```
Common errors: file not executable (`chmod +x`), wrong shebang (`#!/usr/bin/env python3` requires Python on PATH), socket address conflict (label already loaded; `bootout` first).

**Claude rate limit (`error_rate_limit` or `error_budget`).** The whole fleet is now in a global block for one hour. Inspect `~/.hermes/state/global-blocked-until.json`. Wait, or delete the file to clear the block manually. Then look at why one agent burned the weekly cap. Usually a runaway loop on a too-large issue. Lucius's hard cap is 5000 turns/day; tighten if needed.

**Prompt change did not take effect.** Re-run deploy and confirm the rendered launchd job points at the deployed binary under `$HERMES_HOME/bin`:
```sh
bash deploy.sh
launchctl print gui/$(id -u)/my.fleet.lucius
```

**Spend caps tripping too early.** Each agent's cap lives in its own bin (e.g. `lucius.py` line ~122). Adjust the constant and re-deploy.

## Where to go next

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains why the fleet has this shape. Read it before designing a new codename.
