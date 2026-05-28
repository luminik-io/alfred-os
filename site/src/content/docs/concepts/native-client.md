---
title: Native local client
description: "How Alfred should approach a Mac/Linux companion app while keeping Slack as the collaboration surface."
---

The native Alfred client should be a local control plane, not a second Alfred.
Slack remains the primary collaboration UI: plans, replies, approvals, and
post-PR follow-up belong in Slack threads.

The current `alfred serve` redesign is the web contract for that client: a
sticky command center, health signals, plans, firings, memory review, and safe
next actions. A Mac/Linux shell should wrap this contract before inventing a
new one.

The client is for trust and operations:

- what needs my attention now
- which plans are waiting
- why a run failed
- which memory candidates are ready to review
- whether setup, Slack, GitHub, engines, and schedules are healthy
- which safe action can repair the fleet

Full design note: [`docs/NATIVE_CLIENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/NATIVE_CLIENT.md).

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
| Plans | Review plan state, open Slack thread, inspect affected repos and PR chain. |
| Runs | Read firing timelines, logs, engine used, worktree path, issue and PR links. |
| Agents | Pause, resume, dry-run, clear stale locks with proof, inspect schedule. |
| Memory | Promote or reject candidates with evidence, inspect recalled planning hints. |
| Setup | Run doctor, repair Slack/GitHub/engine/scheduler/browser setup. |

## Design direction

Use the Alfred brand system: Space Grotesk for display, Quicksand for UI text,
mono only for ids and commands. The interface should feel like a calm local
cockpit: compact, direct, high contrast, and friendly enough for a user who
does not want to tail logs.

## Implementation path

1. Stabilize JSON APIs in `alfred serve`.
2. Ship a read-only Tauri shell for Mac/Linux.
3. Add safe write actions with command previews.
4. Package signed Mac builds and Linux artifacts.

The direct-host model is inspired by Hermes Desktop's strongest lesson: keep
the host as the source of truth and avoid a second sync layer.
