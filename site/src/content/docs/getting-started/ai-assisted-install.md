---
title: AI-assisted install
description: Use Claude Code, Codex, or another local coding assistant to install Alfred safely.
---

This page mirrors the full GitHub guide:
[`docs/AI_ASSISTED_INSTALL.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/AI_ASSISTED_INSTALL.md).

Use this path when you want Claude Code, Codex, or another local coding
assistant to drive the terminal setup. Alfred supports one repo and multi-repo
workspaces; the assistant needs an explicit repo list either way.

## The Shape

1. Run a side-effect-safe dry-run example.
2. Install prerequisites.
3. Let the human complete browser auth.
4. Configure explicit repos with the full fleet.
5. Run doctor and show the final state.

Do not let the assistant guess repo names, Slack webhooks, AWS profiles, or
which repos should receive scheduled agents.

For checkout layout, read [Workspace patterns](/getting-started/workspace-patterns/).

## Copy-Paste Prompt

```text
Please install Alfred for a local agent fleet.

Values:
- GH_ORG=<your-github-org-or-user>
- REPOS=<comma-separated-repos-owned-by-GH_ORG>
- SPECS_REPO=<optional-specs-repo-or-blank>
- OPERATOR_NAME=<your-name>
- OPERATOR_EMAIL=<your-email>
- SLACK_WEBHOOK=skip
- INSTALL_DIR=~/code/alfred-os
- WORKSPACE_ROOT=~/code

Rules:
- Do not invent secrets, tokens, webhooks, AWS profiles, or repo names.
- Do not enable every discovered repo. Configure only the repos listed in REPOS.
- Keep Slack skipped unless I paste a webhook.
- Keep AWS optional; do not create IAM users or profiles during this install.
- Keep ANTHROPIC_API_KEY and OPENAI_API_KEY unset unless I explicitly ask for API billing.
- Use the full engineering fleet: Drake, Batman, Lucius, Ra's al Ghul, Bane,
  Nightwing, Robin, Huntress, Gordon, automerge, cleanup, memory harvest,
  memory auto-promote, code-map refresh, briefs, recaps, shipped summaries, and
  fleet doctor where available.
- Keep Batman configured even for a one-repo install. It will only act when
  cross-repo or parent-plan work exists and remains runner-gated until
  `alfred enable batman`.
- If SPECS_REPO is set, clone it under the workspace for context, but do not assign Lucius/Nightwing write loops to it unless I explicitly ask.
- Before running any command that loads scheduled agents, show me the command and ask for confirmation.
- If an interactive browser auth step is needed, stop and tell me exactly what to run.
- At the end, show the Alfred repo path, ~/.alfredrc Alfred block, `alfred agents`, `alfred auth status`, and doctor output.
```

Then have the assistant follow the command sequence in the full guide.

If `SPECS_REPO` is set, the full guide tells the assistant to clone it under
`$WORKSPACE_ROOT/product/` for planning context and to leave it out of `--repos`
unless you explicitly want Alfred to operate on specs issues or PRs.

## Installer vs Engine

Claude Code or Codex can be the assistant that installs Alfred. Separately,
Alfred can use Claude Code or Codex as an engine for scheduled agents.

Codex is optional. If Codex is not installed or authenticated, Alfred can still
run Claude-backed agents.

Check readiness with:

```sh
alfred auth status
alfred codex status
alfred codex probe
```

## Safer First Run

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents all \
  --repos "$REPOS" \
  --slack-webhook skip
```

This seeds prompts, creates labels, writes scheduler config, deploys the full
fleet, and runs doctor. `--repos` can be one repo or a comma-separated list.
It does not create AWS profiles or Slack apps.

For multi-repo:

```sh
export REPOS="my-org/api,my-org/web,my-org/mobile"
./bin/alfred-init.py --non-interactive --agents all --repos "$REPOS" --slack-webhook skip
```

Public Batman is multi-repo aware. The parent-issue path posts a rollout plan to
Slack, waits for approval when required, files scoped child issues, and reports
status. The older scan path still drafts plans only. Set `BATMAN_AUTO_EXECUTE=0`
to make every parent-issue plan wait for approval; the default
`approval-gate` reads Slack reactions; set `1` to skip the gate entirely (not
recommended for fresh installs). See
[docs/BATMAN.md](https://github.com/luminik-io/alfred-os/blob/main/docs/BATMAN.md).
