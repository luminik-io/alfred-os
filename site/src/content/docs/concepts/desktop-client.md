---
title: Desktop client
description: The Tauri Mac/Linux control plane over alfred serve, with native installers and no hosted gateway.
---

The Alfred desktop client (`clients/desktop`) is a native Mac/Linux control
plane for a local install. It is the optional `client` tier of the
[layered install](/concepts/layered-install/): the core fleet and CLI run fully
standalone without it.

Slack stays Alfred's collaboration surface. The desktop app is for local trust
and repair: what needs attention, which plans are waiting, why a run failed,
which memory candidates are ready, and which local actions are safe to run next.
It is a thin control plane, not a second runtime.

Design note and run commands: [`docs/DESKTOP_CLIENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/DESKTOP_CLIENT.md) and [`docs/NATIVE_CLIENT.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/NATIVE_CLIENT.md).

## The control plane

```mermaid
flowchart TB
    subgraph client["desktop client (clients/desktop, Tauri)"]
        ui["React UI tabs:<br/>Home / Compose / Fleet / Logs<br/>Setup gear"]
        native["native command allowlist:<br/>start runtime, status, agents,<br/>auth, brain doctor, redis,<br/>safe dry-run"]
    end

    subgraph core["core fleet (headless)"]
        serve["alfred serve<br/>localhost JSON API"]
        state[("$ALFRED_HOME/state<br/>firings / plans / memory")]
        cli["operator CLI: bin/alfred"]
        fleet["lib/agent_runner + bin/*.py"]
    end

    gh["GitHub issue / PR links"]
    slackthread["Slack plan threads"]

    ui -->|local JSON over 127.0.0.1| serve
    native -->|narrow allowlist, no arbitrary shell| cli
    serve --> state
    cli --> fleet
    fleet --> state
    ui -.->|open outside the app| gh
    ui -.->|open outside the app| slackthread
```

## Tabs

| Tab | Job |
|---|---|
| Home | The decision queue: repeated failures, blocked plans, follow-ups, memory candidates, recent plans, recent runs, and fleet-wide pause/resume actions. |
| Compose | Plain-language planning intake plus plan state and origin: local form, Slack DM, app mention, registered thread, affected repos, PR chain, follow-up conversion, and handled state. |
| Fleet | Per-agent service state, safe dry-runs, pause, resume, and run-once actions. |
| Logs | Notifications and firing timelines, including engine context, worktree path, issue links, and PR links. |
| Setup gear | Start the local runtime and run fleet/auth/agent/memory/Redis checks in-app. |

## Boundary

The client reads and writes the same local surfaces an operator can inspect by
hand: `$ALFRED_HOME`, `alfred serve`, the operator CLI, GitHub issue and PR
links, Slack plan threads, and the local fleet brain. It introduces no hosted
gateway, public port, shadow database, or separate scheduler.

The boundary is enforced in the Tauri layer. The fetch command only allows
Alfred JSON API paths on `http://localhost`, `http://127.0.0.1`, or
`http://[::1]`. Links to Slack, GitHub, and `alfred serve` open outside the app.
State-changing controls use a narrow native allowlist (start runtime, run
checks, safe dry-runs, pause, resume, run once, and local follow-up planning)
and surface command audit detail with the result. There is no arbitrary shell
execution. In a plain browser preview, native actions are unavailable.

## Run it locally

```sh
alfred serve --no-browser
cd clients/desktop
npm install
npm run tauri dev
```

## Native installers

`tauri.conf.json` builds the native installer for the host platform:

```sh
cd clients/desktop
npm run tauri -- build
```

| Host | Artifacts |
|---|---|
| macOS | `.app` and `.dmg` |
| Linux | `.AppImage` and `.deb` |

Continuous integration builds with `--no-bundle` to prove the binary compiles
without code signing or packaging. Signed Mac builds and published Linux
artifacts are on the roadmap; today you build the installer locally from the
tagged source.
