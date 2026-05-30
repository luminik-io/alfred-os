---
title: Layered install
description: The core fleet runs standalone and headless. The desktop client and Slack agent are optional surfaces on top.
---

Alfred installs in three tiers. Only the first is required. The desktop client and the Slack agent are optional surfaces that talk to the core through seams you can inspect by hand.

```mermaid
flowchart TB
    subgraph core["core (base, headless)"]
        fleet["fleet: lib/agent_runner + bin/*.py"]
        cli["operator CLI: bin/alfred"]
        sched["scheduler: launchd / systemd --user"]
        serve["alfred serve<br/>localhost JSON API (127.0.0.1)"]
    end

    subgraph client["client (optional)"]
        desktop["Tauri desktop control plane<br/>clients/desktop"]
    end

    subgraph slack["slack (optional)"]
        listener["slack_listener (Socket Mode)"]
        bridge["slack_issue_bridge (off by default)"]
    end

    gh["GitHub"]
    engines["claude -p / codex exec"]

    fleet --> engines
    fleet --> gh
    cli --> fleet
    sched --> fleet
    serve --> fleet
    desktop -->|read/write JSON over 127.0.0.1| serve
    listener --> bridge
    bridge -->|labeled issue| gh
```

## core: standalone and headless

The core tier is the whole product for most operators:

- the fleet (`lib/agent_runner/` plus the `bin/*.py` runners),
- the operator CLI (`bin/alfred`),
- the host scheduler (launchd on macOS, `systemd --user` on Linux),
- `alfred serve`, a localhost JSON API over `$ALFRED_HOME/state`.

Core needs no desktop, no browser, and no Slack. A headless Debian or Ubuntu box runs the entire fleet from timers with nothing on screen. The CLI and fleet are fully standalone; the other tiers are additive.

`alfred serve` is an optional extra so a pure-fleet install stays small:

```sh
pip install 'alfred-os[serve]'   # FastAPI + uvicorn
alfred serve --no-browser        # http://127.0.0.1:7000
```

It binds to `127.0.0.1` by default. Binding to `0.0.0.0` is allowed but discouraged, since the dashboard exposes paths and event payloads that may carry repo context.

## client: the desktop control plane

The optional desktop client (Tauri, under `clients/desktop`) is a thin local control plane, not a second runtime. It is for trust and repair: what needs attention now, which plans are waiting, why a run failed, which memory candidates are ready, and which safe action repairs the fleet. It also offers fleet service control from a guided Setup tab.

The client talks to core only over the `alfred serve` JSON seam, restricted to `http://localhost`, `http://127.0.0.1`, or `http://[::1]` and a fixed set of read paths plus a narrow native command allowlist. No public port, no relay, no shadow database. You can run Alfred entirely without it.

```sh
alfred serve --no-browser   # or let the Setup tab start it
cd clients/desktop
npm install
npm run tauri dev
```

See [native local client](/concepts/native-client/) for the full client design.

## slack: the planning surface

The optional Slack tier is the planning listener plus the issue bridge:

- the **listener** runs in Socket Mode and refines a trusted user's request into a saved local draft. It never files issues, opens PRs, or runs code.
- the **bridge** is off by default. When a trusted user explicitly approves a draft, and the bridge is enabled with a repo allowlist, it files one labeled GitHub issue. From there the fleet claims it through every existing gate. The bridge runs no code.

`slack-sdk` and `boto3` are already in the base install, so the Slack tier needs only configuration. Leave `ALFRED_BRIDGE_ENABLED` unset to keep approvals as refine-only no-ops. See [Slack-native planning](/concepts/slack-native-planning/) and [Slack setup](/guides/slack/).

## Picking your tiers

| You want | Install |
|---|---|
| A headless Linux fleet, no UI | `core` only |
| A Mac cockpit | `core` + `client` |
| Plan-in-Slack workflow | `core` + `slack` (bridge off until you trust it) |
| Everything | all three |

The client and Slack surfaces both sit on top of the same core and never bypass its claim, spend, review, and merge gates. Full tier walkthrough: [`docs/INSTALL_TIERS.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/INSTALL_TIERS.md).
