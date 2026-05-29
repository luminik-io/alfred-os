---
title: Native local client
description: "How Alfred should approach a Mac/Linux companion app while keeping Slack as the collaboration surface."
---

The native Alfred client is a local control plane, not a second Alfred.
Slack remains the primary collaboration UI: plans, replies, approvals, and
post-PR follow-up belong in Slack threads.

The first Tauri preview lives under `clients/desktop` and wraps the
`alfred serve` JSON contract: a sticky command center, health signals, plans,
firings, memory review, and safe next actions.

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
separate scheduler.

## Product shape

The first screen is a Command Center: fleet health, pending approvals, blocked
plans, stale workers, repeated failures, memory review, and safe next actions.

The core tabs are:

| Tab | Job |
|---|---|
| Now | See the decision queue: repeated failures, blocked plans, follow-ups, memory candidates. |
| Plans | Review plan state, open Slack thread, inspect affected repos and PR chain. |
| Runs | Read firing timelines, summaries, engine context, worktree path, issue and PR links. |
| Agents | Inspect status and copy dry-run commands. |
| Memory | Review candidates and inspect recalled planning hints. |
| Setup | Copy start, doctor, fallback-port, and dry-run commands. |

Plans should show whether work started in the local form, a Slack DM, an app
mention, or a registered thread. That keeps Slack as the collaboration trail
while still giving the native client a clean draft inbox.

## Design direction

Use the Alfred brand system: Space Grotesk for display, Quicksand for UI text,
mono only for ids and commands. The interface should feel like a calm local
cockpit: compact, direct, high contrast, and friendly enough for a user who
does not want to tail logs.

## Implementation path

1. Stabilize JSON APIs in `alfred serve`. Done.
2. Ship a read-only Tauri shell for Mac/Linux. Done.
3. Add safe write actions with command previews.
4. Package signed Mac builds and Linux artifacts.

The direct-host model is inspired by Hermes Desktop's strongest lesson: keep
the host as the source of truth and avoid a second sync layer.
