---
title: Hermes integration
description: Optional Hermes setup for operators who want a chat gateway, Kanban, MCP, skills, memory, or dashboards around Alfred.
---

Alfred does not require Hermes. The core runtime is local Python,
host-scheduler units, GitHub CLI, git worktrees, Slack delivery, and local model
CLIs.

Hermes is useful when you want an operator layer around the engineering fleet:
chat gateway, cron prompts, persistent goals, Kanban, MCP tools, skills, memory,
or dashboards. In that setup Hermes observes, reports, or creates handoffs,
while Alfred owns the engineering agent state machine and scheduler units.

Use this page only if you already run Hermes or explicitly want Hermes features.
It is not part of the Alfred quick start.

| Layer | Owns |
|---|---|
| Alfred | scheduler units, role runners, worktrees, issue claims, PR loops, Slack reports |
| Hermes | chat gateway, cron prompts, Kanban cards, MCP tools, skills, memory, dashboards |

## Current Hermes overlap

Recent Hermes releases include persistent `/goal`, a SQLite-backed Kanban board,
worker profiles, gateway notifications, cron, MCP, skills, memory, and Codex
runtime support. Hermes Desktop adds a native Mac view over a Hermes host via
SSH. That is real overlap with Alfred's orchestration and visibility story.

The difference is the source of truth:

- Alfred: GitHub issue/PR lifecycle, scheduled launchd/systemd agent firings,
  isolated repo worktrees, and Slack firing reports.
- Hermes: chat and phone control, profile-based workers, Kanban/task views,
  skills, memory, and dashboards.

`ALFRED_HOME` is Alfred's runtime root. It defaults to `~/.alfred`.
Keep Hermes configuration in Hermes-owned files and pass Alfred paths through
`ALFRED_HOME`.

## Minimal shared env

```sh
ALFRED_HOME="$HOME/.alfred"
WORKSPACE_ROOT="$HOME/code"
GH_ORG="your-github-org"
ACP_ARGS="--acp --stdio"
AWS_PROFILE_FOR_HERMES="hermes-alfred"
```

Prefer AWS profiles over direct `AWS_ACCESS_KEY_ID` and
`AWS_SECRET_ACCESS_KEY` values. Direct keys can override profile-backed calls
and cause stale-signature failures.

## Scheduling rule

Use one scheduler for each role.

Good:

- Alfred's host scheduler fires engineering agents.
- Hermes cron posts daily summaries.
- Hermes cron runs read-only commands like `alfred status` or `alfred shipped`.
- Hermes creates a GitHub issue/label for Alfred to pick up.

Risky:

- Alfred's host scheduler and Hermes cron both fire the same feature-dev runner.
- Hermes shells into an Alfred worktree while a runner owns the issue claim.
- Hermes mutates `ALFRED_HOME/state` directly.
- Hermes Kanban marks a task done while the corresponding Alfred PR is still
  in flight.

## Recommended bridge

The clean bridge is additive:

1. expose read-only Alfred status through `alfred mcp serve`
2. export sanitized Alfred events for dashboards or memory ingestion
3. let Hermes create GitHub issues/labels, then let Alfred execute them
4. link Hermes Kanban cards to GitHub issues/PRs instead of treating Kanban as
   Alfred's source of truth

## Skills and MCP

Alfred ships no skills by default. Keep reviewed skills under
`~/.hermes/skills` or your Claude Code skills directory, and pin third-party
skill sources the same way you pin code dependencies.

For MCP:

```sh
hermes mcp list
hermes mcp test <server-name>
```

If Hermes reports `StdioServerParameters is not defined`, update the local MCP
Python package in the Hermes virtualenv:

```sh
hermes update
~/.hermes/hermes-agent/venv/bin/pip install -U mcp
```

## Full guide

See [docs/HERMES.md](https://github.com/luminik-io/alfred-os/blob/main/docs/HERMES.md)
for the full install order, skill bundle recipe, observability checks, and
public-repo safety rules.
