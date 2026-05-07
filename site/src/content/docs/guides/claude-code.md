---
title: Claude Code
description: Install, Pro vs Max sizing, two-account swap, troubleshooting.
---

Alfred-OS runs most agents as a `claude -p` subprocess. The framework is the harness, Claude Code is the default brain. Codex can be enabled as an optional review engine.

Full guide at [`docs/CLAUDE_CODE.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/CLAUDE_CODE.md). Highlights:

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

## Pro vs Max

| Tier | Approx weekly turns | Use case |
|---|---|---|
| Pro ($20/mo) | ~1500 | Validate the framework, occasional agent runs |
| Max ($100-$200/mo) | ~5000-10000+ | Continuous fleet, 6+ codenames at 20-min cadences |

A typical Lucius firing on a small backend issue burns 30-80 turns. Lucius alone running every 20 minutes against an active issue queue averages 2000-3500 turns/day. Add Bane, Drake, Ra's, Nightwing and you exceed Pro quota in a day.

Recommendation: Pro to validate, Max once you've got 2+ daily codenames. The two-account swap pattern below also lets you split spend.

## Two-account swap (`hermes-claude`)

Two Anthropic accounts? `bin/hermes-claude` symlinks `~/.claude` to either `~/.claude-primary/` or `~/.claude-secondary/` so the cron-spawned `claude` uses whichever you point at.

```sh
hermes-claude status      # which account is active
hermes-claude primary     # symlink to primary
hermes-claude secondary   # symlink to secondary
hermes-claude swap        # toggle
```

Typical use: run on `primary`, hit the cap (Slack alert from `set_global_block`), `hermes-claude swap`, fleet resumes on `secondary`'s quota.

## CLAUDE_BIN

If `claude` isn't on the PATH that `launchd` inherits, set the absolute path in `~/.alfredrc`:

```sh
CLAUDE_BIN=/Users/you/.local/share/fnm/aliases/default/bin/claude
```

## Optional Codex

Set `CODEX_BIN` if `codex` is not on the `launchd` PATH. `codex_invoke()` defaults to a read-only sandbox and writes artifacts under `$HERMES_HOME/state/codex/`.

```sh
CODEX_BIN=$HOME/.local/bin/codex
CODEX_MODEL=gpt-5.4
CODEX_SANDBOX=read-only
CODEX_APPROVAL_POLICY=never
```

`deploy.sh` links an interactive-shell `codex` binary into `~/.local/bin/codex` when one exists. Rendered launchd plists include `~/.local/bin` in PATH.

## Cost mental model

The Anthropic subscription model does not pass through token costs. The fleet is bounded by the weekly turn quota, not USD-per-token. Spend caps in `SpendState` are runaway-loop safety rails, not bill-tracking.

A Max-subscription fleet shipping 10-20 PRs a day costs $100-200/mo flat. Same as if you only used Claude Code interactively for an hour a day.

## Troubleshooting

Full list at [`docs/CLAUDE_CODE.md#troubleshooting`](https://github.com/luminik-io/alfred-os/blob/main/docs/CLAUDE_CODE.md#troubleshooting). Most common:

- `claude: command not found` from `launchd`: set `CLAUDE_BIN`.
- `codex: command not found` from `launchd`: rerun `deploy.sh` after installing Codex, or set `CODEX_BIN`.
- `error_rate_limit` immediately on every firing: cap blown, swap accounts or wait.
- `error_max_turns` on every firing of one agent: tighten scope or widen the budget in that agent's runner.
