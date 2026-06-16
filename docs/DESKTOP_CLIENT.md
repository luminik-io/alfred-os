# Desktop client

The Alfred desktop client (`clients/desktop`) is a native Mac/Linux control plane for a local Alfred install. It is the optional `client` tier of the [layered install](INSTALL_TIERS.md): the core fleet and CLI run fully standalone without it.

Slack stays Alfred's collaboration surface. The desktop app is for local trust and repair: what needs attention, which plans are waiting, why a run failed, which memory candidates are ready, and which local actions are safe to run next. It is a thin control plane, not a second runtime, and it never becomes a chat app or a hosted gateway.

For the design rationale and the Slack boundary, see [`NATIVE_CLIENT.md`](NATIVE_CLIENT.md). For the JSON API it reads, see [`SERVE.md`](SERVE.md). This page is the operator-facing tour of the control surface and how to build installers.

## The control surface

The app is a Tauri shell around a React UI. It opens on Home and keeps primary navigation to the everyday work surfaces. Setup lives behind the gear because it is a repair surface, not an everyday tab:

| Tab | What it shows | What it can do |
|---|---|---|
| **Home** | The decision queue: blocked plans, follow-ups, stale workers, repeated failures, memory candidates, recent runs, and the Inbox capacity rail for Claude and Codex subscription headroom (backed by the live `GET /api/usage` endpoint). | Draft work, refresh state, pause or resume all scheduled firings through the native allowlist, and jump to the right surface. |
| **Compose** | Plain-language planning intake backed by the same readiness engine as Slack. | Draft or refine a plan before it is converted into an issue or spec. |
| **Plans** | Saved Batman plans, Slack drafts, local compose drafts, and captured follow-ups. | Convert a follow-up into a planning draft, mark it handled, or inspect the saved detail in-app. |
| **Memory** | Reviewable memory candidates, promotion suggestions, memory errors, Redis status, Redis sync preview, and failure-pattern harvest. | Promote or reject candidates, run the memory doctor, check Redis AMS, preview sync, and queue repeated-failure lessons. |
| **Fleet** | The agent roster (cinematic deck by default, with a Cinematic / List toggle persisted to local storage), service state, and per-agent controls. | Pause, resume, run once, or dry-run a codename through the native allowlist. |
| **Logs** | Notifications and firing timelines in one readable stream. | Mark activity seen, inspect firing traces in-app, and open explicit Slack or GitHub links outside the app. |
| **Setup gear** | A command console for fleet, auth, agent, memory, Redis, runtime, and Slack collaborator checks. | Start the local runtime, run curated checks in-app, and add or remove local trusted Slack collaborators. |

Plans carry their origin so the Slack collaboration trail stays visible while the app keeps a clean local draft inbox.

## How it talks to the fleet

The client reads the fleet's own state over the `alfred serve` JSON seam and runs a small set of safe local actions through a native command allowlist. It introduces no public port, no relay, and no shadow database; `$ALFRED_HOME` remains the single source of truth.

- **Read path.** The UI loads `/api/status`, `/api/actions`, `/api/usage`, `/api/memory/candidates`, `/api/firings`, `/api/plans`, and `/api/slack/trusted-users` from `alfred serve`. In the desktop shell these go through a Tauri command (`fetch_alfred_json`) that only allows Alfred JSON API paths on `http://localhost`, `http://127.0.0.1`, or `http://[::1]`.
- **Local actions.** State-changing controls use a narrow native allowlist: start the local runtime, fleet status, list agents, auth status, brain doctor, Redis status, Redis sync preview, memory harvest, safe agent dry-runs, pause, resume, run once, local memory review endpoints (`promote`, `reject`), local follow-up planning endpoints (`convert-followup`, `mark-handled`), and local Slack collaborator edits. There is no arbitrary shell execution. Each action surfaces the result and command audit detail.
- **Outside links.** Slack and GitHub links open outside the app through Tauri's opener plugin. Local Alfred plans and firings stay in the native inspector panes.

When run in a plain browser (development preview), the app stays read-only: native actions are unavailable and only the JSON read path works.

## Usage on the capacity rail

The Home Inbox capacity rail shows real Claude and Codex subscription headroom for the rolling 5-hour and weekly windows. The figures come from `GET /api/usage`, which reads the engines' own local CLI state files on the host. Alfred drives Claude Code and Codex through their local subscription CLIs rather than API keys, so there is no billing API to query and no per-token dollar figure (it is meaningless under a Max or Pro subscription). A window the local state cannot confirm reads as not synced rather than a fabricated number. The same numbers are available from the command line with `alfred usage`. See [`SERVE.md`](SERVE.md) for the endpoint and [`CLI.md`](CLI.md) for the CLI.

## Run it locally

Start the runtime from the Setup gear, or run the same port manually:

```sh
alfred serve --port 7010 --no-browser
```

The app probes `7010` first because macOS can reserve `7000` for Control Center.
If you already have an older `alfred serve` running on `7000`, the app probes
that as the legacy fallback and the Setup gear lets you point the client at a
custom local URL.

```sh
alfred serve --no-browser
```

Then run the desktop shell:

```sh
cd clients/desktop
npm install
npm run tauri dev
```

The client defaults to `http://127.0.0.1:7010` and falls back to `7000`.

## Build native installers

`clients/desktop/src-tauri/tauri.conf.json` builds the native installer for the host platform:

```sh
cd clients/desktop
npm install
npm run tauri -- build
```

| Host | Artifacts |
|---|---|
| macOS | `.app` and `.dmg` |
| Linux | `.AppImage` and `.deb` |

Continuous integration builds the client with `--no-bundle` to prove the native binary compiles without requiring code signing, DMG packaging, or Linux package artifacts:

```sh
npm run tauri -- build --no-bundle --ci
```

Signed Mac builds and published Linux artifacts are on the roadmap; today you build the installer locally from the tagged source.

## Checks

```sh
cd clients/desktop
npm run typecheck
npm run build
source "$HOME/.cargo/env"
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo test --manifest-path src-tauri/Cargo.toml
npm run tauri -- build --no-bundle --ci
```

## Plain mode

The desktop Compose box can act as a plain-language intake when the runtime is started with `ALFRED_INTAKE_PROFILE=plain`. A non-technical user types a request, answers a question or two, and approves a plan; the same structured draft and every downstream gate are unchanged. See [`PLAIN_MODE.md`](PLAIN_MODE.md).
