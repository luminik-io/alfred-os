# Desktop client

The Alfred desktop client (`clients/desktop`) is a native Mac/Linux control plane for a local Alfred install. It is the optional `client` tier of the [layered install](INSTALL_TIERS.md): the core fleet and CLI run fully standalone without it.

Slack stays Alfred's collaboration surface. The desktop app is for local trust and repair: what needs attention, which plans are waiting, why a run failed, which memory candidates are ready, and which local actions are safe to run next. It is a thin control plane, not a second runtime, and it never becomes a chat app or a hosted gateway.

For the design rationale and the Slack boundary, see [`NATIVE_CLIENT.md`](NATIVE_CLIENT.md). For the JSON API it reads, see [`SERVE.md`](SERVE.md). This page is the operator-facing tour of the control surface and how to build installers.

## The control surface

The app is a Tauri shell around a React UI. It opens on a Command Center and has nine tabs:

| Tab | What it shows | What it can do |
|---|---|---|
| **Now** | The decision queue: repeated failures, blocked plans, follow-ups, memory candidates. | Jump straight to the surface that needs attention. |
| **Activity** | Notifications from fleet state, follow-ups, and memory. | Mark activity seen and jump to the relevant surface. |
| **Compose** | Plain-language planning intake backed by the same readiness engine as Slack. | Draft or refine a plan and save it to the local Plans inbox. |
| **Plans** | Plan state, source (local form, Slack DM, app mention, or registered thread), affected repos, and the PR chain. | Convert a follow-up into a planning draft, or mark it handled. |
| **Runs** | Firing timelines, summaries, engine context, worktree path, issue and PR links. | Open the linked issue or PR outside the app. |
| **Agents** | Per-agent status and last summary. | Run a safe agent dry-run. |
| **Fleet** | Service state and per-agent controls. | Pause, resume, or run a codename through the native allowlist. |
| **Memory** | Review candidates, recalled planning hints, memory-doctor and Redis checks. | Run the memory doctor and check Redis memory. |
| **Setup** | A command console for fleet, auth, agent, memory, and Slack collaborator checks. | Start the local runtime, run curated checks in-app, and add or remove local trusted Slack collaborators. |

Plans carry their origin so the Slack collaboration trail stays visible while the app keeps a clean local draft inbox.

## How it talks to the fleet

The client reads the fleet's own state over the `alfred serve` JSON seam and runs a small set of safe local actions through a native command allowlist. It introduces no public port, no relay, and no shadow database; `$ALFRED_HOME` remains the single source of truth.

- **Read path.** The UI loads `/api/status`, `/api/actions`, `/api/firings`, `/api/plans`, and `/api/slack/trusted-users` from `alfred serve`. In the desktop shell these go through a Tauri command (`fetch_alfred_json`) that only allows Alfred JSON API paths on `http://localhost`, `http://127.0.0.1`, or `http://[::1]`.
- **Local actions.** State-changing controls use a narrow native allowlist: start the local runtime, fleet status, list agents, auth status, brain doctor, Redis status, and safe agent dry-runs, plus local follow-up planning endpoints (`convert-followup`, `mark-handled`) and local Slack collaborator edits. There is no arbitrary shell execution. Each action surfaces an explicit preview, the affected path, the result, and a rollback hint.
- **Outside links.** Links to Slack, GitHub, and `alfred serve` open outside the app through Tauri's opener plugin rather than inside a webview.

When run in a plain browser (development preview), the app stays read-only: native actions are unavailable and only the JSON read path works.

## Run it locally

Start the runtime first (or start it from the Setup tab):

```sh
alfred serve --no-browser
```

If port 7000 is taken:

```sh
alfred serve --port 7010 --no-browser
```

Then run the desktop shell:

```sh
cd clients/desktop
npm install
npm run tauri dev
```

The client defaults to `http://127.0.0.1:7000` and falls back to `7010`.

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
