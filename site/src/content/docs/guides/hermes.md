---
title: Hermes integration
description: Optional Hermes setup for operators who want MCP, skills, gbrain, canon, or a chat gateway around Alfred.
---

Alfred does not require Hermes. The core runtime is local Python, launchd,
GitHub CLI, git worktrees, Slack delivery, and local model CLIs.

Hermes is useful when you want an operator layer around the engineering fleet:
chat gateway, cron prompts, ACP dispatch, MCP tools, gbrain, canon, or
dashboards. In that setup Hermes observes or dispatches, while Alfred owns
the engineering agent state machine and launchd jobs.

| Layer | Owns |
|---|---|
| Alfred | launchd, role runners, worktrees, issue claims, PR loops, Slack reports |
| Hermes | chat gateway, cron prompts, ACP, MCP tools, skills, memory, dashboards |

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

- Alfred launchd fires engineering agents.
- Hermes cron posts daily summaries.
- Hermes cron runs read-only commands like `alfred status` or `alfred shipped`.

Risky:

- launchd and Hermes cron both fire the same feature-dev runner.
- Hermes shells into an Alfred worktree while a runner owns the issue claim.
- Hermes mutates `ALFRED_HOME/state` directly.

## Skills and MCP

Alfred ships no skills by default. Keep reviewed skills under
`~/.hermes/skills` or your Claude Code skills directory, and pin third-party
skill sources the same way you pin code dependencies.

For MCP:

```sh
hermes mcp list
hermes mcp test gbrain
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
