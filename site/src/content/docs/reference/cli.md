---
title: Operator CLI
description: pennyworth-label-state, pennyworth-doctor, pennyworth-hermes-claude, pennyworth-deploy, pennyworth-install.
---

The framework ships five operator-facing commands. The `pennyworth-` prefix is what the brew formula installs to `/opt/homebrew/bin/`; if you cloned + ran `bash install.sh`, the underlying scripts are at `${HERMES_HOME}/bin/` and `<repo>/bin/`.

## `pennyworth-install`

Fresh-machine bootstrap. Detects macOS, brew-installs CLI deps, npm-installs Claude Code, creates `$HERMES_HOME` + `$WORKSPACE_ROOT`, drops `~/.pennyworthrc` from the template, appends source-line to your shell rc.

```sh
pennyworth-install [--non-interactive] [--skip-brew] [--skip-npm]
```

Env overrides: `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`, `HERMES_HOME`, `WORKSPACE_ROOT`. Full doc at [Install](/pennyworth/getting-started/install/).

## `pennyworth-deploy`

Sync `lib/` and `bin/` into `$HERMES_HOME`, render launchd plists from `launchd/_template.plist` + `launchd/agents.conf`, install them under `~/Library/LaunchAgents/`, bootstrap each one. Idempotent. Honours pause markers — agents in `$HERMES_HOME/state/_paused/` stay paused.

```sh
pennyworth-deploy
```

## `pennyworth-doctor`

Run every agent in `$HERMES_HOME/bin/` under `HERMES_DOCTOR=1`. Each agent's `doctor_mode()` short-circuits before doing real work and emits its `[<NAME>-DOCTOR-OK]` sentinel. Reports pass/fail per agent.

```sh
pennyworth-doctor
```

Use after AWS-key rotation, `aws sso login` refresh, `pennyworth-hermes-claude swap`, or any IAM change. Re-run until it shows all-passed.

## `pennyworth-hermes-claude`

Swap which Claude account `claude -p` uses. Symlinks `~/.claude` to `~/.claude-primary/` or `~/.claude-secondary/`.

```sh
pennyworth-hermes-claude status      # which is active
pennyworth-hermes-claude primary     # symlink to primary
pennyworth-hermes-claude secondary   # symlink to secondary
pennyworth-hermes-claude swap        # toggle
```

See [Claude Code → Two-account swap](/pennyworth/guides/claude-code/) for the populate-snapshots flow.

## `pennyworth-label-state`

Operator CLI for the [issue claim state machine](/pennyworth/concepts/state-machine/).

```sh
pennyworth-label-state claim <repo>#<N> [--force]
    # Set do-not-pickup on an issue. Agents skip it.

pennyworth-label-state release <repo>#<N>
    # Remove do-not-pickup. Issue returns to the autonomous queue.

pennyworth-label-state dedup-check <repo>#<N> [--json]
    # Exit non-zero if not claimable. Used by the pre-push hook.

pennyworth-label-state status-issue <repo>#<N> [--json]
    # Pretty-print the state-machine view.

pennyworth-label-state repo {pause,resume,list} [<repo>]
    # Pause / resume / list repos. Paused repos are skipped by every agent.

pennyworth-label-state sweep-claims [--max-age-hours N] [--repo <name>] [--dry-run]
    # Force-release stale agent:in-flight claims.
```

Repo set for `sweep-claims` is env-driven via `LABEL_STATE_SWEEP_REPOS` (comma-separated).

## Pre-push git hook

[`examples/git-hooks/pre-push`](https://github.com/luminik-io/pennyworth/blob/main/examples/git-hooks/pre-push) refuses any push that references a currently-in-flight or PR-open issue.

```sh
# Install in a target repo
ln -s "$HOME/code/pennyworth/examples/git-hooks/pre-push" \
      <target-repo>/.git/hooks/pre-push
```

Override per-push: `git push --no-verify`. Override globally: `LABEL_STATE_SKIP_DEDUP_CHECK=1` in your shell rc.

## Convention

Every operator-facing binary is doctor-mode aware — invoking it under `HERMES_DOCTOR=1` emits the `[<NAME>-DOCTOR-OK]` sentinel and exits 0 without side effects. So `pennyworth-doctor` works correctly across the whole `bin/` directory regardless of whether a particular file is an agent runner or an operator helper.
