---
title: Operator CLI
description: alfred-label-state, alfred-doctor, alfred-hermes-claude, alfred-deploy, alfred-install.
---

The framework ships five operator-facing commands. The `alfred-os-` prefix is what the brew formula installs to `/opt/homebrew/bin/`; if you cloned + ran `bash install.sh`, the underlying scripts are at `${HERMES_HOME}/bin/` and `<repo>/bin/`.

## `alfred-install`

Fresh-machine bootstrap. Detects macOS, brew-installs CLI deps, npm-installs Claude Code, creates `$HERMES_HOME` + `$WORKSPACE_ROOT`, drops `~/.alfredrc` from the template, appends source-line to your shell rc.

```sh
alfred-install [--non-interactive] [--skip-brew] [--skip-npm]
```

Env overrides: `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`, `HERMES_HOME`, `WORKSPACE_ROOT`. Full doc at [Install](/alfred-os/getting-started/install/).

## `alfred-deploy`

Sync `lib/` and `bin/` into `$HERMES_HOME`, render launchd plists from `launchd/_template.plist` + `launchd/agents.conf`, install them under `~/Library/LaunchAgents/`, bootstrap each one. Idempotent. Honours pause markers: agents in `$HERMES_HOME/state/_paused/` stay paused.

```sh
alfred-deploy
```

## `alfred-doctor`

Run every agent in `$HERMES_HOME/bin/` under `HERMES_DOCTOR=1`. Each agent's `doctor_mode()` short-circuits before doing real work and emits its `[<NAME>-DOCTOR-OK]` sentinel. Reports pass/fail per agent.

```sh
alfred-doctor
```

Use after AWS-key rotation, `aws sso login` refresh, `alfred-hermes-claude swap`, or any IAM change. Re-run until it shows all-passed.

## `alfred-hermes-claude`

Swap which Claude account `claude -p` uses. Symlinks `~/.claude` to `~/.claude-primary/` or `~/.claude-secondary/`.

```sh
alfred-hermes-claude status      # which is active
alfred-hermes-claude primary     # symlink to primary
alfred-hermes-claude secondary   # symlink to secondary
alfred-hermes-claude swap        # toggle
```

See [Claude Code → Two-account swap](/alfred-os/guides/claude-code/) for the populate-snapshots flow.

## `alfred-label-state`

Operator CLI for the [issue claim state machine](/alfred-os/concepts/state-machine/).

```sh
alfred-label-state claim <repo>#<N> [--force]
    # Set do-not-pickup on an issue. Agents skip it.

alfred-label-state release <repo>#<N>
    # Remove do-not-pickup. Issue returns to the autonomous queue.

alfred-label-state dedup-check <repo>#<N> [--json]
    # Exit non-zero if not claimable. Used by the pre-push hook.

alfred-label-state status-issue <repo>#<N> [--json]
    # Pretty-print the state-machine view.

alfred-label-state repo {pause,resume,list} [<repo>]
    # Pause / resume / list repos. Paused repos are skipped by every agent.

alfred-label-state sweep-claims [--max-age-hours N] [--repo <name>] [--dry-run]
    # Force-release stale agent:in-flight claims.
```

Repo set for `sweep-claims` is env-driven via `LABEL_STATE_SWEEP_REPOS` (comma-separated).

## Pre-push git hook

[`examples/git-hooks/pre-push`](https://github.com/luminik-io/alfred-os/blob/main/examples/git-hooks/pre-push) refuses any push that references a currently-in-flight or PR-open issue.

```sh
# Install in a target repo
ln -s "$HOME/code/alfred-os/examples/git-hooks/pre-push" \
      <target-repo>/.git/hooks/pre-push
```

Override per-push: `git push --no-verify`. Override globally: `LABEL_STATE_SKIP_DEDUP_CHECK=1` in your shell rc.

## Convention

Every operator-facing binary is doctor-mode aware. Invoking it under `HERMES_DOCTOR=1` emits the `[<NAME>-DOCTOR-OK]` sentinel and exits 0 without side effects. `alfred-doctor` works correctly across the whole `bin/` directory regardless of whether a particular file is an agent runner or an operator helper.
