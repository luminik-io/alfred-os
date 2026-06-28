---
title: Claude Code and Codex
description: Install, Pro vs Max sizing, account routing, engine routing, troubleshooting.
---

Alfred is the scheduler and guardrail layer; Claude Code is the default engine. Codex can be enabled as an optional per-agent engine, including review-safe Codex-only or Claude-first hybrid routing.

Full guide at [`docs/CLAUDE_CODE.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/CLAUDE_CODE.md). Highlights:

Default billing posture: Alfred uses the local CLI account you have already authenticated. It does not need Anthropic or OpenAI API keys for the normal Claude Code / Codex CLI flow.

## Install

```sh
npm install -g @anthropic-ai/claude-code
```

`install.sh` handles this. Confirm with `claude --version`.

## Authenticate

```sh
claude
```

First-run opens a browser against your Anthropic account. Subsequent `claude -p` calls use the cached auth.

Keep `ANTHROPIC_API_KEY` unset if you want Claude Code to use Pro/Max subscription auth. Claude Code gives environment-variable API keys priority over subscription auth.

## Pro vs Max

| Tier | Use case |
|---|---|
| Pro | Validate the framework, occasional agent runs |
| Max 5x / 20x | Continuous fleet, multiple codenames at frequent cadences |

A typical Lucius firing on a small backend issue burns 30-80 turns. Lucius alone running every 20 minutes against an active issue queue averages 2000-3500 turns/day. The installed full fleet can exceed Pro quota quickly when several codenames run on frequent cadences.

Recommendation: Pro to validate with conservative cadences, Max once you've got 2+ daily codenames. The two-account swap pattern below also lets you split spend.

## Engine routing

Claude account routing and engine routing are different:

- `alfred claude primary|secondary|swap` chooses which local Claude Code auth directory future Claude firings use.
- `alfred engine set <codename> <claude|codex|hybrid>` chooses whether that codename uses Claude, Codex, or Claude-first fallback.

```sh
alfred engine status
alfred engine status lucius
alfred engine set rasalghul codex
alfred engine set lucius hybrid
alfred codex status
alfred codex probe
alfred auth status
```

| Mode | Behavior |
|---|---|
| `claude` | Claude Code only. |
| `codex` | Codex only. |
| `hybrid` | Claude first. Retry transient failures on Claude, and fall back to Codex only when Claude ran but produced no useful result. |

## Two-account swap (`alfred claude`)

Two Anthropic accounts? On macOS, `alfred claude` flips the launchd
`CLAUDE_CONFIG_DIR` env var so macOS launchd-spawned `claude` uses either the
primary `~/.claude/` directory or a secondary config such as
`~/.claude-secondary/`. Primary is set explicitly so older `~/.claude.json`
files cannot accidentally win Claude Code's default profile lookup.

```sh
alfred claude status      # which account is active
alfred claude primary     # set CLAUDE_CONFIG_DIR=~/.claude
alfred claude secondary   # set CLAUDE_CONFIG_DIR=~/.claude-secondary
alfred claude swap        # toggle
```

Set up the secondary account once:

```sh
mkdir -p ~/.claude-secondary
CLAUDE_CONFIG_DIR=$HOME/.claude-secondary claude
```

Typical use: run on `primary`, hit a usage cap or auth issue (Slack alert from `set_global_block`), `alfred claude swap`, fleet resumes on `secondary`'s quota.

On Linux, set `CLAUDE_CONFIG_DIR` in `~/.alfredrc` and redeploy or restart the
systemd user timers. There is no `launchctl setenv` equivalent.

## CLAUDE_BIN

If `claude` isn't on the PATH that the host scheduler inherits, set the absolute path in `~/.alfredrc`:

```sh
CLAUDE_BIN=/Users/you/.local/share/fnm/aliases/default/bin/claude
```

## Optional Codex

Set `CODEX_BIN` if `codex` is not on the host scheduler PATH. `codex_invoke()` defaults to a read-only sandbox and writes artifacts under `$ALFRED_HOME/state/codex/`.

```sh
CODEX_BIN=$HOME/.local/bin/codex
CODEX_MODEL=gpt-5.4
CODEX_SANDBOX=read-only
CODEX_APPROVAL_POLICY=never
```

`deploy.sh` links an interactive-shell `codex` binary into `~/.local/bin/codex` when one exists. Rendered scheduler units include `~/.local/bin` in PATH.

## Cost mental model

Under the default subscription-backed path, Alfred does not add token-metered charges by itself. It consumes the same Claude Code usage pool your terminal sessions consume.

If `ANTHROPIC_API_KEY` is present, Claude Code can use API billing instead of subscription auth. Anthropic usage credits can also let paid-plan users continue after included limits at standard API pricing if they choose that path. Spend caps in `SpendState` are runaway-loop safety rails, not provider bill-tracking.

## Troubleshooting

Full list at [`docs/CLAUDE_CODE.md#troubleshooting`](https://github.com/luminik-io/alfred-os/blob/main/docs/CLAUDE_CODE.md#troubleshooting). Most common:

- `claude: command not found` from a scheduled agent: set `CLAUDE_BIN`.
- `codex: command not found` from a scheduled agent: rerun `deploy.sh` after installing Codex, or set `CODEX_BIN`.
- `error_rate_limit` immediately on every firing: usage cap hit, swap accounts, wait, upgrade, or intentionally use provider-approved usage credits.
- `error_max_turns` on every firing of one agent: tighten scope or widen the budget in that agent's runner.
