---
title: Operator CLI
description: install.sh, deploy.sh, doctor.sh, and the alfred operator CLI.
---

The public framework keeps the operator CLI intentionally small. Fleet-specific
wrappers can add richer status, logs, pause, resume, and metrics commands on
top of these stable primitives.

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

Minimal runner-gate CLI. Installed into `$ALFRED_HOME/bin/alfred` by
`deploy.sh` and symlinked to `~/.local/bin/alfred`.

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
