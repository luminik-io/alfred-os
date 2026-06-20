---
title: Alfred Desktop
description: "How the optional Mac/Linux desktop app works with the local Alfred core runtime while keeping Slack as the collaboration surface."
---

Alfred Desktop is a local companion app for Alfred core, not a second Alfred.
Slack remains the primary collaboration UI: plans, replies, approvals, and
post-PR follow-up belong in Slack threads.

The Tauri client lives under `clients/desktop` and wraps the local
Alfred runtime: Inbox, Ask, Work, Agents, Setup, health signals, plans,
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

## First Launch

1. Install Alfred core from [Install](/getting-started/install/) or the
   Homebrew/source commands on [Download](/download/).
2. Start the local API:

   ```sh
   alfred serve
   ```

3. Install Alfred Desktop from the signed macOS DMG or Linux package on
   [Download](/download/).
4. Open Alfred Desktop. The app connects to `http://127.0.0.1` or
   `http://localhost`, reads the same local state as the CLI, and shows Setup
   actions if the runtime is not reachable yet.

The desktop package alone is not the agent runtime. It needs Alfred core, your
GitHub auth, and at least one configured repo before it can show real plans,
runs, and agents.

Full implementation note and build commands: [`docs/NATIVE_CLIENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/NATIVE_CLIENT.md).

## Boundary

The client should read and write through the same local surfaces Alfred already
uses:

- `$ALFRED_HOME`
- `alfred serve`
- the Alfred CLI
- GitHub issue and PR links
- Slack plan threads
- the local fleet brain

It opens no public port and keeps Alfred's existing scheduler and local state as
the source of truth. Alfred should still work perfectly from Slack and the CLI
when the client is not running.

## Product Shape

The first screen is a Command Center: fleet health, pending approvals, blocked
plans, stale workers, repeated failures, memory review, and safe next actions.

The core tabs are:

| Tab | Job |
|---|---|
| Inbox | See the decision queue, repeated failures, blocked plans, follow-ups, memory candidates, recent runs, shipped work, and capacity rail. |
| Ask | Draft or refine work, open Slack thread context, inspect affected repos and PR chain, convert follow-ups into planning drafts, or mark them handled. |
| Work | Manage queued work, active work, shipped cards, saved plans, and issue queue controls. |
| Agents | Inspect roster state, activity, latest runs, memory candidates, safe dry-runs, pause, resume, and run-once actions. |
| Setup | Start the local runtime and run fleet/auth/agent/memory/Slack checks in the command console. |

Plans should show whether work started in the local form, a Slack DM, an app
mention, or a registered thread. That keeps Slack as the collaboration trail
while still giving the native client a clean draft inbox.

## Design Direction

Use the Alfred brand system: Instrument Sans for display, Quicksand for UI text,
Fragment Mono only for ids and commands. The interface should feel like a calm local
control surface: compact, direct, high contrast, and friendly enough for a user who
does not want to tail logs.

## Implementation Path

1. Stabilize JSON APIs in `alfred serve`. Done.
2. Ship a Tauri shell for Mac/Linux with safe local follow-up actions, runtime launch, a curated command console, status/auth/agent checks, memory checks, Redis checks, and dry-run launch. Done.
3. Add deeper guided install and broader write actions with command previews.
4. Package signed Mac builds and Linux artifacts. Done.

The client builds native installers locally: `npm run tauri -- build` produces `.app`/`.dmg` on macOS and `.AppImage`/`.deb` on Linux from the Tauri bundle config. CI builds with `--no-bundle` to prove the binary compiles without code signing. Releases publish a signed and notarized macOS DMG and app zip, plus Linux AppImage and Debian artifacts. See [Alfred Desktop](/concepts/desktop-client/) for the tab-by-tab control surface and build steps.

The direct-host model is inspired by Hermes Desktop's strongest lesson: keep
the host as the source of truth and avoid a second sync layer.

The client is Alfred Desktop in the optional `client` tier of the [layered install](/concepts/layered-install/). Alfred core and the CLI run fully standalone without it.
