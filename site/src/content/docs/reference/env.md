---
title: Environment variables
description: Every env var the framework reads, what defaults to what, where each is honoured.
---

The framework is env-driven so a fresh user can clone + run without editing source. Every variable listed here is honoured by `agent_runner.py`, `install.sh`, `deploy.sh`, or the rendered launchd plists.

For the operator-config template, see [`.alfredrc.example`](https://github.com/luminik-io/alfred-os/blob/main/.alfredrc.example).

## Required

| Var | Used by | Default | Notes |
|---|---|---|---|
| `GH_ORG` | `agent_runner._full_repo` (every gh helper) | (none: raises) | Bare repo slugs (`backend`) get resolved to `<GH_ORG>/<slug>`. |

## Recommended

| Var | Used by | Default | Notes |
|---|---|---|---|
| `OPERATOR_NAME` | agent prompts | (blank → "the operator") | Substituted via `load_prompt(..., extra_vars=...)`. |
| `OPERATOR_EMAIL` | agent prompts | (blank) | Same. |
| `OPERATOR_GH_HANDLE` | some prompts | (blank) | Used when distinct from `GH_ORG`. |

## Runtime paths

`ALFRED_HOME` is the Alfred runtime root.

| Var | Used by | Default |
|---|---|---|
| `ALFRED_HOME` | everything | `$HOME/.alfred` |
| `WORKSPACE_ROOT` | `agent_runner.WORKSPACE = WORKSPACE_ROOT/product` | `$HOME/code` |
| `CLAUDE_BIN` | `agent_runner.claude_invoke` | `claude` (PATH) |
| `CLAUDE_CONFIG_DIR` | `claude` auth profile selection | Set by `alfred claude` for scheduled agents |
| `CODEX_BIN` | `agent_runner.codex_invoke` | `codex` (PATH) |
| `CODEX_MODEL` | optional model override for `codex exec` | (Codex default) |
| `CODEX_SANDBOX` | `codex exec --sandbox` | `read-only` |
| `CODEX_APPROVAL_POLICY` | `codex exec -c approval_policy=...` | `never` |

## Engine routing

| Var | Used by | Default |
|---|---|---|
| `ALFRED_<CODENAME>_ENGINE` | per-agent engine choice | runner default |
| `ALFRED_ENGINE` | fleet-wide engine override | (none) |
| `ALFRED_REVIEW_ENGINE` | legacy review-agent override | (none) |
| `ALFRED_<CODENAME>_MAX_TURNS` | optional per-agent Claude turn cap | runner default |
| `ALFRED_<CODENAME>_CODEX_SANDBOX` | per-agent Codex sandbox | `CODEX_SANDBOX` |
| `ALFRED_<CODENAME>_CODEX_WRITE` | shortcut for `workspace-write` | `0` |

`bin/alfred engine status/set` persists per-agent engine choices under `$ALFRED_HOME/state/engines/<codename>`.

## Shipped summaries

| Var | Used by | Default |
|---|---|---|
| `ALFRED_SHIPPED_SUMMARY_REPOS` | `alfred shipped` / `alfred-shipped-summary.py` watched repo list | (none) |
| `ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT` | per-window GitHub query limit | `1000` |

## Claude auth

| Var | Used by | Default |
|---|---|---|
| `ALFRED_DISABLE_CLAUDE_AUTH_REPAIR` | disables automatic Claude auth refresh attempts | `0` |
| `CLAUDE_CONFIG_DIR` | `alfred claude` and launchd env | `$HOME/.claude` for primary |

## Slack

| Var | Used by | Default |
|---|---|---|
| `SLACK_WEBHOOK_URL` | `slack_post` (env-var path, highest priority) | (none) |
| `SLACK_WEBHOOK_SECRET_ID` | `slack_post` (AWS path) | `alfred/slack-webhook` |
| `SLACK_WEBHOOK_SECRET_REGION` | `slack_post` (AWS path) | `us-east-1` |
| `SLACK_BOT_TOKEN` | Block Kit / thread helpers | (none) |
| `SLACK_BOT_TOKEN_SECRET_ID` | bot token AWS path | `alfred/slack-bot-token` |
| `SLACK_BOT_TOKEN_SECRET_REGION` | bot token AWS region | `us-east-1` |
| `SLACK_HOME_CHANNEL` | default bot channel | `alfred` |
| `BATMAN_APPROVAL_CHANNEL` | legacy alias for `SLACK_HOME_CHANNEL` | (none) |

Resolution order: env -> 30-day disk cache at `$ALFRED_HOME/state/slack-webhook.cache` -> AWS Secrets Manager. First hit wins.

## AWS

| Var | Used by | Default |
|---|---|---|
| `ALFRED_HUNTRESS_AWS_PROFILE`, `ALFRED_GORDON_AWS_PROFILE` | role runners that touch AWS | (none: AWS checks disabled or use default chain) |
| `AWS_PROFILE` | set by role runners around owned `aws ...` calls | (none) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN` | stripped before `aws ...` calls in agents that need pure-profile auth | (operator's SSO leakage) |

## Doctor

| Var | Used by | Default |
|---|---|---|
| `ALFRED_DOCTOR` | every agent's `doctor_mode()` check | `0` |

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
| `ALFRED_SKIP_BREW` | skip brew install (macOS lane) | (run) |

## label-state CLI

| Var | Used by | Default |
|---|---|---|
| `LABEL_STATE_SWEEP_REPOS` | `examples/bin/label_state.py sweep-claims` (default repo set) | (must pass `--repo`) |
| `LABEL_STATE_BIN` | pre-push hook | (resolves via PATH then `$ALFRED_HOME/bin/`) |
| `LABEL_STATE_SKIP_DEDUP_CHECK` | pre-push hook | (enforce) |

## Site build

| Var | Used by | Default |
|---|---|---|
| `ALFRED_OS_PUBLISH_PAGES` | `.github/workflows/site.yml` deploy gate | (unset: build only) |
| `ALFRED_OS_SITE_URL` | `site/astro.config.mjs` | `https://alfred.luminik.io` |
| `ALFRED_OS_SITE_BASE` | same | `/` |

Override these to deploy under a custom domain (e.g. `ALFRED_OS_SITE_URL=https://alfred-os.dev` + `ALFRED_OS_SITE_BASE=/`).

## Reading the source

The path-resolution block at the top of [`lib/agent_runner.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner.py) is the canonical contract. Every other config file (`install.sh`, `_template.plist`, `agents.conf`) renders into the same env-var shape.
