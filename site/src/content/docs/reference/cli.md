---
title: Operator CLI
description: install.sh, deploy.sh, doctor.sh, hermes-claude, and the minimal alfred gate CLI.
---

The public framework keeps the operator CLI intentionally small. The private
reference fleet has richer `status`, `logs`, `pause`, `resume`, and metrics
commands; alfred-os exposes only the stable public subset today.

## `install.sh`

Fresh-machine bootstrap. Detects macOS, brew-installs CLI deps, npm-installs
Claude Code, creates `$HERMES_HOME` + `$WORKSPACE_ROOT`, drops `~/.alfredrc`
from the template, and appends a source block to your shell rc.

```sh
bash install.sh [--non-interactive] [--skip-brew] [--skip-npm]
```

## `deploy.sh`

Syncs `lib/` and `bin/` into `$HERMES_HOME`. If `launchd/agents.conf` exists,
it renders plists from `launchd/_template.plist`, installs them under
`~/Library/LaunchAgents/`, and bootstraps each unpaused job. Without
`agents.conf`, it performs a framework-only deploy.

```sh
bash deploy.sh
```

## `bin/doctor.sh`

Runs configured Python agents under `HERMES_DOCTOR=1`. On a fresh checkout
with no `launchd/agents.conf`, it reports `0 passed, 0 failed`.

```sh
bash bin/doctor.sh
```

## `bin/hermes-claude`

Swap which Claude account `claude -p` uses.

```sh
bin/hermes-claude status
bin/hermes-claude primary
bin/hermes-claude secondary
bin/hermes-claude swap
```

## `alfred`

Minimal runner-gate CLI. Installed into `$HERMES_HOME/bin/alfred` by
`deploy.sh` and symlinked to `~/.local/bin/alfred`.

```sh
alfred agents
alfred enable <codename>
alfred disable <codename>
alfred enabled-agents
```

`alfred agents` reads `launchd/agents.conf` and shows schedule, load column,
runner-gate enablement, and role text. `enable` / `disable` update
`$HERMES_HOME/state/fleet/enabled.txt`, which is useful for opt-in runners
such as Batman.

## State-machine Helpers

The issue claim-state helper is currently shipped as an example at
`examples/bin/label_state.py`. Copy or wrap it in your fleet if you want
operator commands such as `claim`, `release`, `dedup-check`, `status-issue`,
`repo pause`, or `sweep-claims`.
