---
title: Operator CLI
description: install.sh, deploy.sh, doctor.sh, and the alfred operator CLI.
---

The operator CLI covers the local fleet control surface: install, deploy,
doctor, starter setup, status, runner-gate enablement, pause/resume, manual
runs, engine selection, Claude/Codex auth checks, Claude account management, and shipped-work summaries.

## `install.sh`

Fresh-machine bootstrap. Detects macOS or Debian/Ubuntu Linux, installs CLI
deps through Homebrew or apt, npm-installs Claude Code, creates `$ALFRED_HOME`
and `$WORKSPACE_ROOT`, drops `~/.alfredrc` from the template, and appends a
source block to your shell rc.

```sh
bash install.sh [--non-interactive] [--skip-brew] [--skip-npm]
```

Homebrew installs the same script behind a wrapper:

```sh
alfred-install [--non-interactive] [--skip-brew] [--skip-npm]
```

## `deploy.sh`

Syncs `lib/` and `bin/` into `$ALFRED_HOME`. If `launchd/agents.conf` exists,
it renders host scheduler units: launchd plists on macOS, systemd user
services/timers on Linux. Without `agents.conf`, it performs a framework-only
deploy.

```sh
bash deploy.sh
```

Homebrew wrapper:

```sh
alfred-deploy
```

## `bin/alfred-init.py`

Configures the scheduled fleet after the base install.

```sh
./bin/alfred-init.py
./bin/alfred-init.py --non-interactive --agents starter --repos owner/repo --slack-webhook skip
./bin/alfred-init.py --non-interactive --agents starter --repos owner/api,owner/web --slack-webhook skip
```

Homebrew wrapper:

```sh
alfred-init
```

`--agents` accepts `starter`, `all`, or comma-separated codenames. `starter`
enables Drake, Lucius, Ras al Ghul, and agent-cleanup. `--repos` scopes every
enabled repo-operating agent to explicit repos and is required for safe
non-interactive setup when more than one repo is visible. The repo owner must
match `GH_ORG`; shipped agents store bare repo names and build `GH_ORG/repo`
when they fire.

## `bin/doctor.sh`

Runs configured Python agents under `ALFRED_DOCTOR=1`. On a fresh checkout
with no `launchd/agents.conf`, it reports `0 passed, 0 failed`.

```sh
bash bin/doctor.sh
```

Homebrew wrapper:

```sh
alfred-doctor
```

## `alfred claude`

Swap which Claude account `claude -p` uses. The `primary`, `secondary`, and
`swap` commands set launchd env on macOS and `systemd --user` manager env on
Linux. Restart already-running systemd services after switching.

```sh
alfred claude status
alfred claude primary
alfred claude secondary
alfred claude swap
alfred claude probe
```

## `alfred codex` and `alfred auth`

Check Codex CLI availability and run tiny provider-auth probes before scheduled
work starts.

```sh
alfred codex status
alfred codex probe
alfred auth status
alfred auth probe
```

## `alfred`

Fleet-control CLI. Installed into `$ALFRED_HOME/bin/alfred` by `deploy.sh` and
symlinked to `~/.local/bin/alfred`.

```sh
alfred agents
alfred enable <codename>
alfred disable <codename>
alfred enabled-agents
alfred status
alfred claude status
alfred claude swap
alfred claude probe
alfred codex status
alfred codex probe
alfred auth status
alfred engine status [codename]
alfred engine set <codename> <claude|codex|hybrid>
alfred metrics [--since 7d] [--codename <name>] [--by-day] [--json]
alfred logs <codename> [--last N] [--firing-id ID] [--show-tool-calls] [--json]
alfred shipped --period weekly
```

`alfred agents` reads `launchd/agents.conf` and shows schedule, load column,
runner-gate enablement, and role text. `enable` / `disable` update
`$ALFRED_HOME/state/fleet/enabled.txt`, which is useful for opt-in runners
such as Batman. `engine` persists per-agent Claude/Codex mode under
`$ALFRED_HOME/state/engines/<codename>`. `codex` checks the Codex CLI. `auth`
checks Claude and Codex auth surfaces. `status` reports local locks, pauses,
recent firings, and Batman approval waits. `shipped` reports merged PRs, issues,
LOC, and model/config changes across `ALFRED_SHIPPED_SUMMARY_REPOS` or explicit
`--repo` values.

## `alfred metrics`

Weekly per-agent rollup of firings, cost, turns, tool-use, and Codex tokens.
Read-only; reads `$ALFRED_STATE_DIR` (defaults to `$ALFRED_HOME/state`) and
prints either a table or JSON.

```sh
alfred metrics                          # last 7 days, per-agent
alfred metrics --since 14d              # last 14 days
alfred metrics --since 48h              # rounds up to days
alfred metrics --codename lucius        # one codename only
alfred metrics --by-day                 # daily totals instead of per-agent
alfred metrics --json                   # machine-readable
```

`--since` accepts `7`, `7d`, `48h`, `2w`, `1m`. `--days N` overrides
`--since` when both are passed. Exit 1 on user errors (bad `--since`,
unknown codename); exit 2 when the state directory is missing.

## `alfred logs`

Inspect stream-JSON transcripts under
`$ALFRED_STATE_DIR/transcripts/<codename>/<YYYY-MM>/<firing-id>.jsonl`.

```sh
alfred logs <codename>                              # last 10 firings (summary)
alfred logs <codename> --last 25                    # last 25 firings
alfred logs <codename> --show-tool-calls            # tool-call rollup
alfred logs <codename> --firing-id <id>             # dump one firing
alfred logs <codename> --firing-id <id> --show-tool-calls
alfred logs <codename> --json                       # machine-readable
```

Summary view shows `firing_id, when, subtype, turns, cost, tools, edits,
top tools`. Tool-call mode aggregates `tool_use` blocks across the last N
firings and lists skill invocations separately. The single-firing dump
pretty-prints the stream-JSON one event per line with tool-use inputs
summarised inline.

See [`docs/CLI.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/CLI.md)
for the full reference including library examples.

## State-machine helpers

### `alfred-label-state`

Operator-facing CLI for the [issue claim state machine](/concepts/state-machine/).
Installed into `$ALFRED_HOME/bin/alfred-label-state` by `deploy.sh`.

```sh
alfred-label-state claim       <repo>#<N> [--force]
alfred-label-state release     <repo>#<N>
alfred-label-state dedup-check <repo>#<N> [--json]
alfred-label-state status-issue <repo>#<N> [--json]
alfred-label-state repo        {pause,resume,list} [<repo>]
alfred-label-state sweep-claims [--max-age-hours N] [--repo <name>] [--dry-run]
```

| Subcommand | Use |
|---|---|
| `claim` | Set `do-not-pickup` on an issue so agents skip it while you work it manually. `--force` overrides an in-flight claim. |
| `release` | Remove `do-not-pickup`. Issue returns to the `agent:implement` queue. |
| `dedup-check` | Probe whether an issue is currently claimable. Exits non-zero if not. Designed for use inside a `pre-push` git hook. |
| `status-issue` | Pretty-print the state-machine view of an issue (labels, latest claim, claimable verdict). |
| `repo pause/resume/list` | Pause or resume an entire repo. Agents skip every issue in a paused repo. |
| `sweep-claims` | Force-release stale `agent:in-flight` claims whose latest unreleased claim comment is older than `--max-age-hours` (default 4). |

Configuration (12-factor, env-driven):

| Env var | Purpose |
|---|---|
| `GH_ORG` | GitHub org for repo-targeting helpers in `agent_runner` (required). |
| `ALFRED_HOME` | Runtime root (default `~/.alfred`). |
| `LABEL_STATE_SWEEP_REPOS` | Comma-separated repo slugs for the default `sweep-claims` target set when `--repo` isn't passed. |

Sample invocations:

```sh
# Take #42 in your-backend off the autonomous queue for a manual fix.
alfred-label-state claim your-backend#42

# What's currently going on with #42?
alfred-label-state status-issue your-backend#42 --json

# Daily stale-claim cleanup across your engineering repos:
LABEL_STATE_SWEEP_REPOS="your-backend,your-frontend,your-mobile" \
  alfred-label-state sweep-claims --max-age-hours 4

# Pre-push hook usage: refuse to push if the closed issue is in-flight elsewhere.
alfred-label-state dedup-check your-backend#42 || exit 1
```

A copy also ships at `examples/bin/label_state.py` for fleets that prefer
to wrap the CLI under their own entry-point name.
