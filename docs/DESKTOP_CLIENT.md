# Desktop app guide

Alfred Desktop (`clients/desktop`) is a native Mac/Linux control surface for a local Alfred install. It is the optional `client` tier of the [layered install](INSTALL_TIERS.md): the core fleet and CLI run fully standalone without it.

Slack stays Alfred's collaboration surface. The desktop app is for local trust and repair: what needs attention, which plans are waiting, why a run failed, which memory candidates are ready, and which local actions are safe to run next. It is a thin local control surface, not a second runtime, and it never becomes a chat app.

For the design rationale and the Slack boundary, see [`NATIVE_CLIENT.md`](NATIVE_CLIENT.md). For the JSON API it reads, see [`SERVE.md`](SERVE.md). This page is the user-facing tour of the desktop app and how to build installers.

## The control surface

The app is a Tauri shell around a React UI. It opens on Inbox and keeps primary navigation to five everyday work surfaces:

| Tab | What it shows | What it can do |
|---|---|---|
| **Inbox** | The decision queue: blocked plans, follow-ups, stale workers, repeated failures, memory candidates, recent runs, and the capacity rail for Claude and Codex subscription headroom (backed by the live `GET /api/usage` endpoint). | Draft work, refresh state, pause or resume scheduled firings through the native allowlist, and jump to the right surface. |
| **Ask** | Plain-language planning intake backed by the same readiness engine as Slack. | Draft or refine a plan before it is converted into an issue or spec. |
| **Work** | The Kanban board: Queued / Working now / Shipped, saved plans, Slack follow-ups, and local draft actions. | Queue an issue, hold work, mark work done, convert follow-ups, or inspect saved detail in-app. |
| **Agents** | The agent roster, activity feed, latest-run inspector, and memory learning queue. | Pause, resume, run once, dry-run a codename, promote or reject memory candidates, and inspect firing traces. |
| **Setup** | Guided repair and onboarding: runtime, auth, repos, labels, engine checks, Slack collaborators, and demo data. | Start the local runtime, run curated checks in-app, and add or remove local trusted Slack collaborators. |

Plans carry their origin so the Slack collaboration trail stays visible while the app keeps a clean local draft inbox.

## How it talks to the fleet

The client reads the fleet's own state over the `alfred serve` JSON API and runs a small set of safe local actions through a native command allowlist. It opens no public port, and `$ALFRED_HOME` remains the single source of truth.

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

The app uses `7010` because macOS can reserve `7000` for Control Center.
Legacy saved `7000` URLs are treated as stale local configuration and rewritten
to 7010. Setup still lets you point the client at a custom localhost URL when
needed.

Then run the desktop shell:

```sh
cd clients/desktop
npm install
npm run tauri dev
```

The client defaults to `http://127.0.0.1:7010`.

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

The release pipeline publishes a signed and notarized macOS DMG and app zip,
plus Linux AppImage and Debian artifacts. Local `tauri build` still works when
you need to inspect or test the installer output yourself.

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
