---
title: Operator CLI
description: install.sh, deploy.sh, doctor.sh, and the alfred operator CLI.
---

The operator CLI covers the local fleet control surface: install, deploy,
doctor, starter setup, status, runner-gate enablement, pause/resume, manual
runs, engine selection, Claude account management, and shipped-work summaries.

## `install.sh`

Fresh-machine bootstrap. Detects macOS, brew-installs CLI deps, npm-installs
Claude Code, creates `$ALFRED_HOME` + `$WORKSPACE_ROOT`, drops `~/.alfredrc`
from the template, and appends a source block to your shell rc.

```sh
bash install.sh [--non-interactive] [--skip-brew] [--skip-npm]
```

## `deploy.sh`

Syncs `lib/` and `bin/` into `$ALFRED_HOME`. If `launchd/agents.conf` exists,
it renders plists from `launchd/_template.plist`, installs them under
`~/Library/LaunchAgents/`, and bootstraps each unpaused job. Without
`agents.conf`, it performs a framework-only deploy.

```sh
bash deploy.sh
```

## `bin/alfred-init.py`

Configures the scheduled fleet after the base install.

```sh
./bin/alfred-init.py
./bin/alfred-init.py --non-interactive --agents starter --repos owner/repo --slack-webhook skip
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

## `alfred claude`

Swap which Claude account `claude -p` uses.

```sh
alfred claude status
alfred claude primary
alfred claude secondary
alfred claude swap
alfred claude probe
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
alfred engine status [codename]
alfred engine set <codename> <claude|codex|hybrid>
alfred shipped --period weekly
```

`alfred agents` reads `launchd/agents.conf` and shows schedule, load column,
runner-gate enablement, and role text. `enable` / `disable` update
`$ALFRED_HOME/state/fleet/enabled.txt`, which is useful for opt-in runners
such as Batman. `engine` persists per-agent Claude/Codex mode under
`$ALFRED_HOME/state/engines/<codename>`. `status` reports local locks, pauses,
recent firings, and Batman approval waits. `shipped` reports merged PRs,
issues, LOC, and model/config changes across `ALFRED_SHIPPED_SUMMARY_REPOS`
or explicit `--repo` values.

## State-machine Helpers

The issue claim-state helper is currently shipped as an example at
`examples/bin/label_state.py`. Copy or wrap it in your fleet if you want
operator commands such as `claim`, `release`, `dedup-check`, `status-issue`,
`repo pause`, or `sweep-claims`.
