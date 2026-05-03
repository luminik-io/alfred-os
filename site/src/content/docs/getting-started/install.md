---
title: Install
description: Fresh-machine setup for alfred-os in about 30 minutes.
---

This page condenses [`INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/INSTALL.md). For the full doc with every troubleshooting case, read it on GitHub.

## TL;DR

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub
claude                            # Claude Code first-run auth
bash deploy.sh && bash bin/doctor.sh
```

You should see `0 passed, 0 failed` from doctor on a clean install. The framework is ready; you haven't pointed any codename agents at it yet.

## What `install.sh` does

Idempotent (safe to re-run). On a fresh Mac:

1. Verifies macOS. Linux support is on the roadmap; see [Linux](/alfred-os/guides/linux/).
2. Installs Homebrew if missing.
3. `brew install`s `python@3.11`, `git`, `gh`, `jq`, `awscli`, `node`, `uv`.
4. `npm install -g @anthropic-ai/claude-code`.
5. Creates `$HERMES_HOME` (default `~/.hermes`) and `$WORKSPACE_ROOT` (default `~/code`).
6. Drops `~/.alfredrc` from the template, prompts for `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`.
7. Appends a source-line to your shell rc so every new shell loads `~/.alfredrc`.
8. Reports auth status for `gh`, `aws`, `claude`.

What it does **not** do (deliberately):

- Authenticate `gh` / `aws` / `claude`. Interactive flows you should see.
- Create AWS IAM users, secrets, or Slack webhooks. One-time human decisions.
- Run `deploy.sh`. That side-effects `launchd`; you should know what's about to load.

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

## After install

Point alfred-os at your fleet's Slack channel and (optionally) AWS:

- [Slack setup](/alfred-os/guides/slack/): create the app, mint the webhook.
- [AWS setup](/alfred-os/guides/aws/): IAM-per-agent, Secrets Manager.
- [Claude Code](/alfred-os/guides/claude-code/): Pro vs Max sizing, two-account swap.

Then write your first codename agent:

- [Tutorial: your first agent in 30 minutes](/alfred-os/getting-started/tutorial/): builds Echo end-to-end.

## Troubleshooting

Full list in [`INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/INSTALL.md#troubleshooting-installsh) on GitHub. The most common:

- **"Refusing to install on non-macOS host"**: alfred-os's scheduling layer is `launchd`. Linux requires the systemd port (on the roadmap).
- **`claude: command not found` from launchd**: the plist's PATH doesn't include the npm global bin. Set `CLAUDE_BIN` in `~/.alfredrc`.
- **`gh auth login` browser doesn't open**: use the device-code flow: `gh auth login --hostname github.com --git-protocol https --web`.
