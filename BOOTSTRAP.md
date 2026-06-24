# Bootstrap

End-to-end setup for consuming Alfred as the framework for your own Claude Code fleet on a fresh host. Plan ~60 minutes the first time, most of it on AWS IAM and the Anthropic Claude Code CLI.

The fleet runs on a single always-on host. A Mac Mini works. An old laptop with the lid open works. A Debian/Ubuntu box works. Not a server-class deployment.

> Want the faster path? [`INSTALL.md`](INSTALL.md) is the from-zero TL;DR (~30 minutes, mostly automated by `install.sh`). Use BOOTSTRAP for per-agent IAM, Slack, first fleet configuration, and troubleshooting.

> This guide is written for the macOS (`launchd`) path. The Linux (`systemd --user`) path is the same shape (`install.sh`, `deploy.sh`, and the `alfred` CLI all detect the host) with apt instead of Homebrew and `systemctl --user` instead of `launchctl`. See [`docs/LINUX.md`](docs/LINUX.md) for the Linux specifics, then come back here for per-agent IAM, Slack, and prompt engineering.

> Rendered docs: [https://alfred.luminik.io](https://alfred.luminik.io).

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| macOS 13+ or Debian/Ubuntu Linux | `launchd` per-user agents (macOS) or `systemd --user` timers (Linux) for per-firing scheduling | n/a (see [`docs/LINUX.md`](docs/LINUX.md) for the Linux path) |
| Python 3.11+ | The agent runners and `agent_runner` library | `brew install python@3.11` |
| Node via fnm | Frontend pre-push checks; `claude` CLI lives under fnm by default in this repo | `brew install fnm && fnm install --lts` |
| `git` 2.40+ | Worktree commands the agents lean on | `brew install git` |
| `gh` (GitHub CLI) | Every agent uses `gh issue` / `gh pr` | `brew install gh && gh auth login` |
| `aws` (AWS CLI v2) | AWS-touching agents and the Slack webhook fetch | `brew install awscli` |
| `claude` (Anthropic Claude Code CLI) | The actual code-writing engine | See [Anthropic docs](https://docs.anthropic.com/claude/docs/claude-code) |
| Anthropic Claude Pro or Max subscription | Pays for agent turns; no API key required | claude.ai/upgrade |

Pro gives a few thousand turns per week against `claude -p`. Max raises the ceiling enough for a continuous Lucius launchd cadence plus the rest of the fleet. If the subscription hits its weekly cap mid-firing the agent surfaces `error_rate_limit` and the global block trips for one hour.

## 1. Clone and pick paths

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
```

Two environment variables drive every path. Set them in `~/.zshrc` (or your shell rc):

```sh
export ALFRED_HOME="$HOME/.alfred"
export WORKSPACE_ROOT="$HOME/code"   # parent dir of your forked product repos
```

`ALFRED_HOME` is the runtime root: deployed agent binaries land in `$ALFRED_HOME/bin`, the shared library in `$ALFRED_HOME/lib`, state in `$ALFRED_HOME/state`, per-firing worktrees in `$ALFRED_HOME/worktrees`.

Alfred core runs standalone. Optional companion features such as skills, MCP,
external memory, or dashboarding are layered on only if you choose to add them.

`WORKSPACE_ROOT` is the parent directory of your canonical product checkouts. Lucius and Bane look here for repo CLAUDE.md files and grep targets before invoking `claude -p`.

All framework paths are env-driven via `ALFRED_HOME` and `WORKSPACE_ROOT`. No source edits needed.

## 2. AWS setup: one IAM user per scheduled agent

The operator's SSO chain is never used by scheduled agents. Every AWS-touching agent gets its own scoped IAM user.

Create the user (one-time, in the AWS console or via CloudFormation), then write its access keys to `~/.aws/credentials` under a named profile. Example for an agent codenamed `<your-codename>-cron`:

```ini
# ~/.aws/credentials
[<your-codename>-cron]
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
    -u AWS_SECURITY_TOKEN AWS_PROFILE=<your-codename>-cron aws ...
```

The `env -u` calls strip any operator SSO leakage; `AWS_PROFILE` then forces the scoped profile.

## 3. Slack webhook

Create a Slack incoming webhook for the channel where the fleet should report (we use `#your-fleet-channel`). The framework's `slack_post()` resolves the URL via env -> 30-day disk cache -> AWS Secrets Manager. Pick whichever you want.

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

The runtime caches the webhook at `$ALFRED_HOME/state/slack-webhook.cache` with a 30-day TTL, so a slow Secrets Manager call does not stall every Slack post. See `slack_post()` in [`lib/agent_runner/`](lib/agent_runner/__init__.py) and the full walkthrough in [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md) (which also covers the optional bot-token + app-level-token paths).

## 4. Configure the fleet

Run the public wizard. It chooses which agents are enabled, which repos they watch, their codenames, schedules, Slack config, and optional AWS profile settings.

```sh
./bin/alfred-init.py
```

The wizard writes `launchd/agents.conf`, updates `~/.alfredrc`, runs `bash deploy.sh`, and runs `bash bin/doctor.sh`.

For a one-repo solo-builder fleet, you can drive the same setup without
interactive choices:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos your-org/your-repo \
  --slack-webhook skip
```

This configures Drake, Lucius, Ras al Ghul, and agent-cleanup; seeds starter
prompts into `~/.alfred/prompts/`; and creates the GitHub labels the runners
expect. Add Slack later by re-running the wizard or setting
`SLACK_WEBHOOK_URL` in `~/.alfredrc`.

The repo owner must match `GH_ORG`; the runtime stores bare repo names and
builds `GH_ORG/repo` during agent firings.

If you only want a framework-only deploy with no scheduled agents yet, skip the wizard and run:

```sh
bash deploy.sh
```

Expected output:

```
[deploy] copying lib/
[deploy] copying bin/ (Python + shell)
[deploy] copying scheduler units
  - my.fleet.lucius
  - my.fleet.nightwing
  - my.fleet.rasalghul
  - my.fleet.bane
[deploy] active jobs:
-	0	my.fleet.lucius
-	0	my.fleet.nightwing
...
```

The script copies the whole `lib/` tree (top-level modules plus the
`agent_runner/`, `connectors/`, `fleet_brain/`, `memory/`, and `server/`
subpackages) to `$ALFRED_HOME/lib` and every regular file in `bin/` to
`$ALFRED_HOME/bin`. When `launchd/agents.conf` exists, it
renders host scheduler units from that manifest: launchd plists on macOS,
systemd user services/timers on Linux.

## 5. Verify the host with `doctor.sh`

Before firing any agent, sanity-check that every agent's preflight passes on this host:

```sh
bash bin/doctor.sh
```

Expected output:

```
doctor: checking configured agents
        ALFRED_HOME=/Users/<you>/.alfred
        WORKSPACE_ROOT=/Users/<you>/code

  drake                          ✅ ok
  lucius                         ✅ ok
  rasalghul                      ✅ ok

doctor: 3 passed, 0 failed
```

Any failure prints the `[<AGENT>-PREFLIGHT-FAILED]` block naming each gap: missing env var, missing CLI binary, dead AWS profile, expired `gh auth`, missing repo checkout. Fix and re-run until every configured agent passes. A framework-only deploy with no `agents.conf` reports `0 passed, 0 failed`.

`ALFRED_DOCTOR=1` is the env var the agents themselves check; `doctor.sh` sets it and invokes every agent. You can also run a single agent in doctor mode:

```sh
ALFRED_DOCTOR=1 ~/.alfred/bin/lucius.py
# [LUCIUS-DOCTOR-OK]
```

This is also the right command after you rotate AWS keys, refresh `aws sso login`, swap Claude account via `alfred claude swap`, or change anything in IAM policy: re-run `doctor.sh` and confirm every configured agent passes.

## 6. First firing: dry run

The plists ship with `RunAtLoad = false`, so deploying does not immediately fire any agent. To test a single agent without it shipping a PR:

1. Pick an agent and read its top-of-file constants. Lucius, for example, is gated by `agent:implement` issues. If no repo has one open, the firing exits `[SILENT]`.
2. Fire it by hand:
   ```sh
   alfred run lucius --force
   ```
3. Tail the logs:
   ```sh
   tail -f /tmp/my.fleet.lucius.stdout /tmp/my.fleet.lucius.stderr
   ```
4. To inspect what `claude -p` actually saw, look in `/tmp/lucius-debug-<issue>-<ts>/`. The runner persists the prompt and raw JSON result there for every Lucius run.

To verify wiring without code landing, point Lucius at a repo with no `agent:implement` issues and confirm it exits `[SILENT]`. Or label one issue with `agent:implement` and `lucius-attempt-3`. Lucius will skip it and re-label `needs:human-scope` instead of running.

To pause an agent:

```sh
alfred pause lucius
```

To resume:

```sh
alfred resume lucius
```

## 8. Troubleshooting

**`claude: command not found` in the scheduler log.** The rendered unit's `PATH` does not include the fnm-managed Node bin (or wherever your `claude` lives). Set `CLAUDE_BIN=<absolute-path>` in `~/.alfredrc`, or expose the binary through a stable directory already rendered into scheduler PATH, such as `~/.local/bin`. `which claude` shows the path.

**`codex: command not found` in the scheduler log.** Rerun `deploy.sh` after installing Codex. If `codex` is visible in your interactive shell, deploy links it into `~/.local/bin/codex`, which the renderer adds to scheduler PATH. Otherwise set `CODEX_BIN=<absolute-path>` in `~/.alfredrc`.

**Slack posts silently fail.** The webhook cache may be stale (URL rotated) or AWS Secrets Manager may be unreachable. Run `aws secretsmanager get-secret-value --secret-id <your/webhook/path> --region us-east-1` against the agent's profile (`AWS_PROFILE=<your-codename>-cron`, etc.) and confirm it returns the URL. To force a refresh, delete `~/.alfred/state/slack-webhook.cache`.

**`AccessDeniedException` from AWS.** The agent is using the wrong profile. Confirm `~/.aws/credentials` has the named profile and that the agent's prompt uses `env -u ... AWS_PROFILE=<your-codename>-cron`. The operator's SSO env vars beat profiles in the AWS credential chain. The `env -u` strips them.

**Plist not loading.** `launchctl bootstrap` is silent on success and noisy on failure. Run it manually with the full path:
```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/my.fleet.lucius.plist
```
Common errors: file not executable (`chmod +x`), wrong shebang (`#!/usr/bin/env python3` requires Python on PATH), socket address conflict (label already loaded; `bootout` first).

**Claude rate limit (`error_rate_limit` or `error_budget`).** The whole fleet is now in a global block for one hour. Inspect `~/.alfred/state/global-blocked-until.json`. Wait, or delete the file to clear the block manually. Then look at why one agent burned the weekly cap. Usually a runaway loop on a too-large issue. Lucius's hard cap is 5000 turns/day; tighten if needed.

**Prompt change did not take effect.** Re-run deploy and confirm the rendered launchd job points at the deployed binary under `$ALFRED_HOME/bin`:
```sh
bash deploy.sh
launchctl print gui/$(id -u)/my.fleet.lucius
```

**Spend caps tripping too early.** Each agent's cap lives in its own bin (e.g. `lucius.py` line ~122). Adjust the constant and re-deploy.

## Where to go next

[`ARCHITECTURE.md`](ARCHITECTURE.md) explains why the fleet has this shape. Read it before designing a new codename.
