---
title: Native local client
description: "How Alfred should approach a Mac/Linux companion app while keeping Slack as the collaboration surface."
---

The native Alfred client is a local control plane, not a second Alfred.
Slack remains the primary collaboration UI: plans, replies, approvals, and
post-PR follow-up belong in Slack threads.

The first Tauri preview lives under `clients/desktop` and wraps the local
Alfred runtime: Home, Compose, Fleet, Logs, Setup, health signals, plans,
firings, memory review, safe next actions, native runtime launch, and local
follow-up handling.

The client is for trust and operations:

- what needs my attention now
- which plans are waiting
- why a run failed
- which memory candidates are ready to review
- which Slack-created planning drafts need more scope
- whether setup, Slack, GitHub, engines, and schedules are healthy
- which safe action can repair the fleet

Full design note and run commands: [`docs/NATIVE_CLIENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/NATIVE_CLIENT.md).

## Boundary

The client should read and write through the same local surfaces an operator
can inspect by hand:

- `$ALFRED_HOME`
- `alfred serve`
- the operator CLI
- GitHub issue and PR links
- Slack plan threads
- the local fleet brain

It should not introduce a hosted gateway, public port, shadow database, or
separate scheduler. Alfred should still work perfectly from Slack and the CLI
when the client is not running.

## Product shape

The first screen is a Command Center: fleet health, pending approvals, blocked
plans, stale workers, repeated failures, memory review, and safe next actions.

The core tabs are:

| Tab | Job |
|---|---|
| Home | See the decision queue: repeated failures, blocked plans, follow-ups, memory candidates, recent runs, and fleet-wide pause/resume actions. |
| Compose | Draft or refine work, open Slack thread context, inspect affected repos and PR chain, convert follow-ups into planning drafts, or mark them handled. |
| Fleet | Inspect status, run safe dry-runs, pause, resume, and run agents once. |
| Logs | Read notifications and firing timelines, including summaries, engine context, worktree path, issue links, and PR links. |
| Setup gear | Start the local runtime and run fleet/auth/agent/memory/Redis checks in the command console. |

Plans should show whether work started in the local form, a Slack DM, an app
mention, or a registered thread. That keeps Slack as the collaboration trail
while still giving the native client a clean draft inbox.

## Design direction

Use the Alfred brand system: Instrument Sans for display, Quicksand for UI text,
Fragment Mono only for ids and commands. The interface should feel like a calm local
cockpit: compact, direct, high contrast, and friendly enough for a user who
does not want to tail logs.

## Implementation path

1. Stabilize JSON APIs in `alfred serve`. Done.
2. Ship a Tauri shell for Mac/Linux with safe local follow-up actions, runtime launch, a curated command console, status/auth/agent checks, memory checks, Redis checks, and dry-run launch. Done.
3. Add guided install and broader write actions with command previews.
4. Package signed Mac builds and Linux artifacts.

The client already builds native installers locally: `npm run tauri -- build` produces `.app`/`.dmg` on macOS and `.AppImage`/`.deb` on Linux from the Tauri bundle config. CI builds with `--no-bundle` to prove the binary compiles without code signing. See the [desktop client](/concepts/desktop-client/) for the tab-by-tab control surface and build steps.

The direct-host model is inspired by Hermes Desktop's strongest lesson: keep
the host as the source of truth and avoid a second sync layer.

The client is the optional `client` tier of the [layered install](/concepts/layered-install/). The core fleet and CLI run fully standalone without it.
