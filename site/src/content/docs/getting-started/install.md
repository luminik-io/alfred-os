---
title: Install
description: Fast setup for an existing dev machine, with a longer guided path for a fresh host.
---

This page condenses [`INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/INSTALL.md). Budget about 30 minutes on an already-provisioned dev machine, or 60 to 120 minutes for a fresh laptop, server, or dedicated agent box. For the full doc with every troubleshooting case, read it on GitHub.

## TL;DR

Source checkout path:

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub
claude                            # Claude Code first-run auth
./bin/alfred-init.py              # choose agents, repos, codenames, Slack
```

macOS Homebrew path, if you prefer package-manager installs:

```sh
brew tap luminik-io/alfred-os https://github.com/luminik-io/alfred-os
brew install alfred-os
alfred-install
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub
claude                            # Claude Code first-run auth
alfred-init                       # choose agents, repos, codenames, Slack
```

The Homebrew formula installs the latest tagged release and puts the operator
commands on your PATH: `alfred`, `alfred-init`, `alfred-install`,
`alfred-deploy`, and `alfred-doctor`. Use the source checkout path when you
want `main`, framework edits, or Linux.

Starter fleet for one repo or an explicit comma-separated repo list:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos your-org/api,your-org/web \
  --slack-webhook skip
```

This is the zero-guess path for a solo builder or an AI coding tool setting up
one or more explicit repos. It assumes `GH_ORG` is set, `gh auth login` has completed, and
`claude` has completed first-run auth. The repo owner must match `GH_ORG`; the
runtime agents store the bare repo name in `~/.alfredrc` and build
`GH_ORG/repo` at firing time. The command enables Drake, Lucius, Ras al Ghul,
and agent-cleanup; assigns the selected repo to each repo-operating agent;
skips Slack safely; seeds prompt templates into `~/.alfred/prompts/`; creates
standard GitHub labels on the selected repos; writes `launchd/agents.conf`, the
shared scheduler manifest; updates `~/.alfredrc`; runs deploy; and runs doctor.

For a framework-only install with no agents configured, run `bash deploy.sh &&
bash bin/doctor.sh`; doctor should report `0 passed, 0 failed`.

## Install With Claude Code or Codex

Claude Code, Codex, or another local coding assistant can drive setup if you
give it explicit values and guardrails. Use the copy-paste prompt in
[`docs/AI_ASSISTED_INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/AI_ASSISTED_INSTALL.md).

The important rules:

- start with one explicit repo, or one explicit comma-separated repo list
- use the starter fleet
- keep Slack skipped unless you paste a webhook
- do not create AWS profiles during first install
- pause for browser auth flows
- run `alfred auth status` and `doctor.sh` before trusting scheduled firings

For repo checkout layout, read [Workspace patterns](/getting-started/workspace-patterns/).

## What `install.sh` does

Idempotent (safe to re-run). It detects the host OS and picks a lane: Homebrew on macOS, apt on Debian/Ubuntu.

1. Detects the host: macOS (Homebrew) or Debian/Ubuntu Linux (apt). See [Linux](/guides/linux/) for the systemd path.
2. Installs the package-manager prerequisites: Homebrew if missing on macOS; on Linux, `apt-get install`s the base packages.
3. Installs `python@3.11`, `git`, `gh`, `jq`, `node`, `uv` (plus `awscli` on macOS; install AWS CLI v2 manually on Linux).
4. `npm install -g @anthropic-ai/claude-code`.
5. Creates `$ALFRED_HOME` (default `~/.alfred`) and `$WORKSPACE_ROOT` (default `~/code`).
6. Drops `~/.alfredrc` from the template, prompts for `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`.
7. Appends a source-line to your shell rc so every new shell loads `~/.alfredrc`.
8. Reports auth status for `gh`, `aws`, `claude`.

What it does **not** do (deliberately):

- Authenticate `gh` / `aws` / `claude`. Interactive flows you should see.
- Create AWS IAM users, secrets, or Slack webhooks. One-time human decisions.
- Choose which agents should run. Use `./bin/alfred-init.py` for that.
- Run `deploy.sh`. That side-effects the host scheduler (`launchd` on macOS,
  `systemd --user` on Linux); you should know what's about to load.
- Install an external agent gateway, memory database, MCP server, dashboard, or
  skill bundle. `ALFRED_HOME` is only Alfred's runtime root.

## Non-interactive

For automation:

```sh
ALFRED_NONINTERACTIVE=1 \
  GH_ORG=myorg \
  OPERATOR_NAME='Your Name' \
  OPERATOR_EMAIL=you@example.com \
  bash install.sh
```

Per-stage skips: `--skip-brew`, `--skip-npm`.

For `alfred-init.py`, `--agents starter` means Drake, Lucius, Ras al Ghul, and
agent-cleanup. Use `--agents all` only when you want every scheduled agent.
Use `--repos owner/repo` for one repo, or
`--repos owner/api,owner/web,owner/mobile` for multi-repo. `owner` must match
`GH_ORG`.

## After install

Point Alfred at your fleet's Slack channel and (optionally) AWS:

- [Slack setup](/guides/slack/): create the app, mint the webhook.
- [AWS setup](/guides/aws/): IAM-per-agent, Secrets Manager.
- [Claude Code and Codex](/guides/claude-code/): Pro vs Max sizing, account routing, engine routing.

Then write your first codename agent:

- [Tutorial: your first agent in 30 minutes](/getting-started/tutorial/): builds Echo end-to-end.

## Troubleshooting

Full list in [`INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/INSTALL.md#troubleshooting-installsh) on GitHub. The most common:

- **`install.sh` stops on an unsupported host**: the apt lane targets Debian/Ubuntu. Other Linux distros need their packages installed by hand; the framework itself is distro-agnostic once the prerequisites are present.
- **`claude: command not found` from a scheduled agent**: the scheduler unit's PATH doesn't include the npm global bin. Set `CLAUDE_BIN` in `~/.alfredrc`.
- **`gh auth login` browser doesn't open**: use the device-code flow: `gh auth login --hostname github.com --git-protocol https --web`.
