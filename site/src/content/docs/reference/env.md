---
title: Environment variables
description: Every env var the framework reads, what defaults to what, where each is honoured.
---

The framework is env-driven so a fresh user can clone + run without editing source. Every variable listed here is honoured by `agent_runner.py`, `install.sh`, `deploy.sh`, or the rendered launchd plists.

For the operator-config template, see [`.alfredrc.example`](https://github.com/luminik-io/alfred-os/blob/main/.alfredrc.example).

## Required

| Var | Used by | Default | Notes |
|---|---|---|---|
| `GH_ORG` | `agent_runner._full_repo` (every gh helper) | (none — raises) | Bare repo slugs (`backend`) get resolved to `<GH_ORG>/<slug>`. |

## Recommended

| Var | Used by | Default | Notes |
|---|---|---|---|
| `OPERATOR_NAME` | agent prompts | (blank → "the operator") | Substituted via `load_prompt(..., extra_vars=...)`. |
| `OPERATOR_EMAIL` | agent prompts | (blank) | Same. |
| `OPERATOR_GH_HANDLE` | some prompts | (blank) | Used when distinct from `GH_ORG`. |

## Runtime paths

| Var | Used by | Default |
|---|---|---|
| `HERMES_HOME` | everything | `$HOME/.hermes` |
| `WORKSPACE_ROOT` | `agent_runner.WORKSPACE = WORKSPACE_ROOT/product` | `$HOME/Workspace` |
| `LUMINIK_WORKSPACE` | back-compat alias for `WORKSPACE_ROOT` | (deprecated) |
| `CLAUDE_BIN` | `agent_runner.claude_invoke` | `claude` (PATH) |

## Slack

| Var | Used by | Default |
|---|---|---|
| `SLACK_WEBHOOK_URL` | `slack_post` (env-var path, highest priority) | (none) |
| `SLACK_WEBHOOK_SECRET_ID` | `slack_post` (AWS path) | `alfred/slack-webhook` |
| `SLACK_WEBHOOK_SECRET_REGION` | `slack_post` (AWS path) | `us-east-1` |

Resolution order: env → 7-day disk cache at `$HERMES_HOME/state/slack-webhook.cache` → AWS Secrets Manager. First hit wins.

## AWS

| Var | Used by | Default |
|---|---|---|
| `AWS_PROFILE` | per-agent runner sets explicitly before `aws ...` calls | (none — must be set) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN` | stripped before `aws ...` calls in agents that need pure-profile auth | (operator's SSO leakage) |

## Doctor

| Var | Used by | Default |
|---|---|---|
| `HERMES_DOCTOR` | every agent's `doctor_mode()` check | `0` |

Set to `1` and any agent runner will emit `[<NAME>-DOCTOR-OK]` and exit 0 without side effects.

## Cleanup retention

| Var | Used by | Default |
|---|---|---|
| `ALFRED_SPEND_RETENTION_DAYS` | `agent-cleanup.py` (in consumer fleets) | `90` |
| `ALFRED_TRANSCRIPT_RETENTION_DAYS` | same | `30` |
| `ALFRED_EVENTS_RETENTION_DAYS` | same | `30` |
| `ALFRED_CLAIM_MAX_AGE_HOURS` | stale-claim sweep | `4` |

## install.sh

| Var | Used by | Default |
|---|---|---|
| `ALFRED_NONINTERACTIVE` | `install.sh` (use defaults for every prompt) | (interactive) |
| `ALFRED_SKIP_NPM` | skip Claude Code install | (run) |
| `ALFRED_SKIP_BREW` | skip brew install | (run) |
| `ALFRED_FORCE_LINUX` | override the macOS check | (refuse) |

## label-state CLI

| Var | Used by | Default |
|---|---|---|
| `LABEL_STATE_SWEEP_REPOS` | `alfred-label-state sweep-claims` (default repo set) | (must pass `--repo`) |
| `LABEL_STATE_BIN` | pre-push hook | (resolves via PATH then `$HERMES_HOME/bin/`) |
| `LABEL_STATE_SKIP_DEDUP_CHECK` | pre-push hook | (enforce) |

## Site build

| Var | Used by | Default |
|---|---|---|
| `ALFRED_OS_SITE_URL` | `site/astro.config.mjs` | `https://luminik-io.github.io` |
| `ALFRED_OS_SITE_BASE` | same | `/alfred-os` |

Override these to deploy under a custom domain (e.g. `ALFRED_OS_SITE_URL=https://alfred-os.dev` + `ALFRED_OS_SITE_BASE=/`).

## Reading the source

For ground truth, the path-resolution block at the top of [`lib/agent_runner.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner.py) is the canonical contract — every other config file (`install.sh`, `_template.plist`, `agents.conf`) renders into the same env-var shape.
