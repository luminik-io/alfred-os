---
title: Environment variables
description: Every env var the framework reads, what defaults to what, where each is honoured.
---

The framework is env-driven so a fresh user can clone + run without editing source. Every variable listed here is honoured by the `agent_runner` package, `install.sh`, `deploy.sh`, or the rendered scheduler units.

For the local config template, see [`.alfredrc.example`](https://github.com/luminik-io/alfred-os/blob/main/.alfredrc.example).

## Required

| Var | Used by | Default | Notes |
|---|---|---|---|
| `GH_ORG` | `agent_runner._full_repo` (every gh helper) | (none: raises) | Bare repo slugs (`backend`) get resolved to `<GH_ORG>/<slug>`. |

## Recommended

| Var | Used by | Default | Notes |
|---|---|---|---|
| `OPERATOR_NAME` | agent prompts | blank | Optional human-readable name substituted via `load_prompt(..., extra_vars=...)`. |
| `OPERATOR_EMAIL` | agent prompts | (blank) | Same. |
| `OPERATOR_GH_HANDLE` | some prompts | (blank) | Used when distinct from `GH_ORG`. |

## Runtime paths

`ALFRED_HOME` is the Alfred runtime root.

| Var | Used by | Default |
|---|---|---|
| `ALFRED_HOME` | everything | `$HOME/.alfred` |
| `WORKSPACE_ROOT` | parent of repo checkouts; combined with `WORKSPACE_SUBDIR` by `agent_runner.WORKSPACE` | `$HOME/code` |
| `WORKSPACE_SUBDIR` | subdirectory under `WORKSPACE_ROOT`; set to an empty string for repos directly under `WORKSPACE_ROOT` | `product` |
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
| `ALFRED_<CODENAME>_MAX_TURNS` | optional per-agent Claude turn cap | runner default |
| `ALFRED_<CODENAME>_CODEX_SANDBOX` | per-agent Codex sandbox | `CODEX_SANDBOX` |
| `ALFRED_<CODENAME>_CODEX_WRITE` | shortcut for `workspace-write` | `0` |

Engine values are `claude`, `codex`, or `hybrid`. `bin/alfred engine
status/set` persists per-agent engine choices under
`$ALFRED_HOME/state/engines/<codename>`.

## Shipped summaries

| Var | Used by | Default |
|---|---|---|
| `ALFRED_SHIPPED_SUMMARY_DAILY_REPOS` | daily `alfred shipped` / `alfred-shipped-summary.py` watched repo list | `ALFRED_SHIPPED_SUMMARY_REPOS` |
| `ALFRED_SHIPPED_SUMMARY_WEEKLY_REPOS` | weekly `alfred shipped` / `alfred-shipped-summary.py` watched repo list | `ALFRED_SHIPPED_SUMMARY_REPOS` |
| `ALFRED_SHIPPED_SUMMARY_REPOS` | shared fallback watched repo list for manual or legacy config | (none) |
| `ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT` | per-window GitHub query limit | `1000` |

## Batman planning

| Var | Used by | Default |
|---|---|---|
| `BATMAN_SCAN_REPOS` | `bin/batman.py` repo scan scope for `agent:large-feature` issues | (none, no-op) |
| `BATMAN_ROLLOUT_ORDER` | `alfred-init.py` and plan parsing defaults | `backend,frontend,mobile,agents,data-acquisition` |
| `AGENT_CODENAME_CROSS_REPO_COORDINATOR` | `alfred-init.py` codename mapping | `batman` |
| `BATMAN_PARENT_REPO` | `bin/batman.py` parent-issue lifecycle path | (none) |
| `BATMAN_AUTO_EXECUTE` | `lib/batman.py` plan execution mode | `0` |
| `BATMAN_PICKER` | `lib/batman.py` parent issue selection | `oldest` |
| `BATMAN_BUNDLE_SLUG_PREFIX` | `lib/batman.py` bundle slug rendering | (blank) |
| `BATMAN_APPROVAL_TIMEOUT_S` | `lib/batman.py` Slack approval wait | `86400` |
| `BATMAN_REPORT_FEEDBACK_TIMEOUT_S` | `lib/batman.py` post-report Slack follow-up capture | `60` |
| `BATMAN_SLACK_CHANNEL` | `lib/batman.py` plan and report channel | (blank, falls back to Slack home channel) |

Batman is included in the public fleet, but execution is gated. The newer
`BATMAN_PARENT_REPO` path reads parent issues, waits for approval when required,
files child `agent:implement` issues, and reports status. With the default
`BATMAN_AUTO_EXECUTE=0`, parent issues halt after the plan; set
`approval-gate` when you want approved child filing. The legacy
`BATMAN_SCAN_REPOS` path scans configured repos, groups `agent:bundle:<slug>`
siblings, posts a rollout plan, and stops before child issue filing.

## Claude auth

| Var | Used by | Default |
|---|---|---|
| `ALFRED_DISABLE_CLAUDE_AUTH_REPAIR` | disables automatic Claude auth refresh attempts | `0` |
| `CLAUDE_CONFIG_DIR` | Claude Code auth profile selection | `$HOME/.claude` for primary |

## Slack

| Var | Used by | Default |
|---|---|---|
| `SLACK_WEBHOOK_URL` | `slack_post` (env-var path, highest priority) | (none) |
| `SLACK_WEBHOOK_SECRET_ID` | `slack_post` (AWS path) | `alfred/slack-webhook` |
| `SLACK_WEBHOOK_SECRET_REGION` | `slack_post` (AWS path) | `us-east-1` |
| `SLACK_BOT_TOKEN` | Block Kit / thread helpers | (none) |
| `SLACK_BOT_TOKEN_SECRET_ID` | bot token AWS path | `alfred/slack-bot-token` |
| `SLACK_BOT_TOKEN_SECRET_REGION` | bot token AWS region | `us-east-1` |
| `SLACK_APP_TOKEN` | Socket Mode token for the optional planning listener | (none) |
| `ALFRED_SLACK_APP_TOKEN` | Alternate Socket Mode token env var | (none) |
| `ALFRED_OPERATOR_SLACK_USER_ID` | Operator user id for reactions and trusted listener input | (required for approvals/listener) |
| `ALFRED_TRUSTED_SLACK_USER_IDS` | Extra users allowed to refine plans and drafts | approver only |
| `ALFRED_SLACK_BOT_USER_ID` | Bot user id; listener ignores its own messages | (none) |
| `SLACK_HOME_CHANNEL` | default bot channel | `alfred` |
| `BATMAN_APPROVAL_CHANNEL` | legacy alias for `SLACK_HOME_CHANNEL` | (none) |

Resolution order: env -> 30-day disk cache at `$ALFRED_HOME/state/slack-webhook.cache` -> AWS Secrets Manager. First hit wins.

## AWS

| Var | Used by | Default |
|---|---|---|
| `ALFRED_HUNTRESS_AWS_PROFILE`, `ALFRED_GORDON_AWS_PROFILE` | role runners that touch AWS | (none: AWS checks disabled or use default chain) |
| `AWS_PROFILE` | set by role runners around owned `aws ...` calls | (none) |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN` | stripped before `aws ...` calls in agents that need pure-profile auth | (admin SSO leakage) |

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

[`lib/agent_runner/paths.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner/paths.py) is the canonical path contract. Every other config file (`install.sh`, `_template.plist`, `agents.conf`) renders into the same env-var shape.
