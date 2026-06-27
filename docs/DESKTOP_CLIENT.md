# Desktop app guide

Alfred Desktop (`clients/desktop`) is a native Mac/Linux control surface for a local Alfred install. It is the optional `client` tier of the [layered install](INSTALL_TIERS.md): the core fleet and CLI run fully standalone without it.

Slack stays Alfred's collaboration surface. The desktop app is for local trust and repair: what needs attention, which plans are waiting, why a run failed, which memory candidates are ready, and which local actions are safe to run next. It is a thin local control surface, not a second runtime, and it never becomes a chat app.

For the JSON API it reads, see [`SERVE.md`](SERVE.md). This page is both the design rationale and the user-facing tour: why the desktop app is shaped the way it is, what each tab does, and how to build installers.

## Decision

Build the native Mac/Linux app as a thin local control surface and installer, not a second Alfred runtime.

Slack remains the primary collaboration UI because it already has threads, reactions, search, mobile push, and shared context. The native client makes Alfred easier to trust and repair: setup checks, health, logs, approvals, memory review, safe pause/resume, dry-run launch, and recovery.

The client reads and writes through the same local APIs, state files, and CLI commands Alfred already uses. You can run Alfred with or without the client, and Slack remains the collaboration surface.

## Product principles

- One source of truth: `$ALFRED_HOME`, GitHub, Slack threads, and the local fleet brain.
- Direct host access: local HTTP on `127.0.0.1` for same-machine installs, SSH for remote Linux hosts. No public port, no relay, no sync service.
- Slack-native collaboration: every plan, approval, rejection, and follow-up should have a Slack thread link where discussion happened.
- Actionable, not chatty: the home screen answers "what needs me right now?" before showing logs or historical data.
- Explain before acting: write actions show the target, expected effect, and rollback path before running. The underlying command is audit detail, not the primary interface.
- Accessible to technical and non-technical users: product language first, shell commands second. The app can reveal details without making them the default.

## The control surface

The app is a Tauri shell around a React UI. It opens on Inbox and keeps primary navigation to five everyday work surfaces:

| Tab | What it shows | What it can do |
|---|---|---|
| **Inbox** | The decision queue: blocked plans, follow-ups, stale workers, repeated failures, memory candidates, recent runs, and the capacity rail for Claude and Codex subscription headroom (backed by the live `GET /api/usage` endpoint). | Draft work, refresh state, pause or resume scheduled firings through the native allowlist, and jump to the right surface. |
| **Ask** | Plain-language planning intake backed by the same readiness engine as Slack. | Draft or refine a plan before it is converted into an issue or spec. |
| **Work** | The Kanban board: Queued / Working now / Shipped, saved plans, Slack follow-ups, and local draft actions. | Queue an issue, hold work, mark work done, convert follow-ups, or inspect saved detail in-app. |
| **Agents** | The agent roster, activity feed, latest-run inspector, and memory learning queue. | Pause, resume, run once, dry-run a codename, promote or reject memory candidates, and inspect firing traces. |
| **Setup** | Guided repair and onboarding: runtime, auth, repos, labels, engine checks, code memory, Slack collaborators, and demo data. | Start the local runtime, run curated checks in-app, and add or remove local trusted Slack collaborators. |

Plans carry their origin so the Slack collaboration trail stays visible while the app keeps a clean local draft inbox.

### Inbox in detail

Inbox is the first screen: a decision queue and local command center.

- fleet health
- Claude and Codex subscription headroom on the capacity rail (backed by the live `GET /api/usage` endpoint)
- pending approvals
- blocked plans
- stale workers
- repeated failures
- memory candidates ready for review
- Slack listener health and newly saved planning drafts
- safe actions: refresh, pause all, resume all, open Slack thread, open PR, and run the memory doctor

The top row should feel like an operations status strip, not a dashboard full of vanity metrics.

### Ask in detail

Ask owns plain-language intake and the planning inbox. Plan cards show:

- parent issue and Slack thread
- readiness verdict
- affected repos and rollout order
- open questions
- latest revision summary
- source: local form, Slack DM, app mention, or registered thread
- approve/reject status
- PR chain after execution starts

The app can help draft or refine a spec, but the final collaboration loop stays in Slack. Any "send to Alfred" action posts to or links back to the approval thread. A locally drafted single-repo issue lands behind an approval gate (`agent:plan-pending-approval`) and is held from autonomous pickup until you approve it, so nothing single-repo ships without a go-ahead.

### Agents in detail

Agents is the operational surface. The agent roster is the default view: a cinematic deck of themed agent cards, each carrying the agent's accent, status, cadence, runs-today, latest signal, and a monogram. A Cinematic / List toggle (persisted to local storage) switches to a dense list when you want every agent at a glance. Motion respects `prefers-reduced-motion`, and the cards are real buttons announced as actionable controls.

Per-agent controls: enabled/paused state, schedule, last run, last failure, dry-run, pause/resume, run once, clear stale lock with proof, and open prompt/config files. Every destructive or state-changing action has a dry-run preview where the CLI supports one.

The Agents activity feed combines in-app notifications and firing timelines for forensics: timeline by firing, step-level run events (plan created, worktree created, pre-push checks passed, branch pushed, PR opened) emitted the moment the underlying action succeeds, the engine used, the worktree path, issue and PR links, the event log, the transcript link when present, and the final status with the next recommended action. The feed reads without horizontal scrolling on narrow screens.

### Memory review

Memory review is reviewable and appears where you are already working:

- Inbox surfaces memory candidates ready for review
- Ask recalls promoted planning hints beside drafts
- candidate promote/reject, the memory doctor, Redis status, a Redis sync preview, and the repeated-failure harvest are available in-app
- Setup keeps memory, code-memory, and Redis checks available as repair actions
- Slack exposes `memory`, `remember`, `memory remember`, `memory promote`, `memory reject`, `memory redis`, and `memory sync`

The app visibly separates promoted lessons from candidates and raw logs.

### Setup in detail

Setup is a guided doctor: install or repair the CLI, start the local runtime, GitHub auth, Slack bot/webhook, engine CLIs, launchd or systemd timers, watched repos, labels, memory provider, code-memory graph layer, and browser dependencies for agents that need them. Failures tell you what Alfred checked, why it matters, and the smallest next step.

## How it talks to the fleet

The client reads the fleet's own state over the `alfred serve` JSON API and runs a small set of safe local actions through a native command allowlist. It opens no public port, and `$ALFRED_HOME` remains the single source of truth.

- **Read path.** The UI loads `/api/status`, `/api/actions`, `/api/usage`, `/api/memory/candidates`, `/api/firings`, `/api/plans`, and `/api/slack/trusted-users` from `alfred serve`. In the desktop shell these go through a Tauri command (`fetch_alfred_json`) that only allows Alfred JSON API paths on `http://localhost`, `http://127.0.0.1`, or `http://[::1]`.
- **Local actions.** State-changing controls use a narrow native allowlist: start the local runtime, fleet status, list agents, auth status, brain doctor, code-memory doctor, Redis status, Redis sync preview, memory harvest, safe agent dry-runs, pause, resume, run once, local memory review endpoints (`promote`, `reject`), local follow-up planning endpoints (`convert-followup`, `mark-handled`), and local Slack collaborator edits. There is no arbitrary shell execution. Each action surfaces the result and command audit detail.
- **Outside links.** Slack and GitHub links open outside the app through Tauri's opener plugin. Local Alfred plans and firings stay in the native inspector panes.

When run in a plain browser (development preview), the app stays read-only: native actions are unavailable and only the JSON read path works.

## Usage on the capacity rail

The Inbox capacity rail shows real Claude and Codex subscription headroom for the rolling 5-hour and weekly windows. The figures come from `GET /api/usage`, which reads the engines' own local CLI state files on the host. Alfred drives Claude Code and Codex through their local subscription CLIs rather than API keys, so there is no billing API to query and no per-token dollar figure (it is meaningless under a Max or Pro subscription). A window the local state cannot confirm reads as not synced rather than a fabricated number. The same numbers are available from the command line with `alfred usage`. See [`SERVE.md`](SERVE.md) for the endpoint and [`CLI.md`](CLI.md) for the CLI.

## Run it locally

Start the runtime from the Setup gear, or run the same port manually:

```sh
alfred serve --port 7010 --no-browser
```

The app uses `7010` because macOS can reserve `7000` for Control Center. Legacy saved `7000` URLs are treated as stale local configuration and rewritten to 7010. Setup still lets you point the client at a custom localhost URL when needed.

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
| macOS 11+ on Apple silicon | `.app` and `.dmg` |
| Linux | `.AppImage` and `.deb` |

Continuous integration builds the client with `--no-bundle` to prove the native binary compiles without requiring code signing, DMG packaging, or Linux package artifacts:

```sh
npm run tauri -- build --no-bundle --ci
```

The public release workflow creates the draft release. Signed and notarized macOS assets, plus Linux AppImage and Debian packages, are attached before that release is published. Local `tauri build` still works when you need to inspect or test the installer output yourself.

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

The desktop Ask box can act as a plain-language intake when the runtime is started with `ALFRED_INTAKE_PROFILE=plain`. A non-technical user types a request, answers a question or two, and approves a plan; the same structured draft and every downstream gate are unchanged. See [`PLAIN_MODE.md`](PLAIN_MODE.md).

## API shape to stabilize next

The client uses these local API contracts today:

```text
GET  /api/status
GET  /api/schedule
GET  /api/actions
GET  /api/shipped
GET  /api/usage             # served; backs the capacity rail
GET  /api/usage/providers   # served; flat per-engine re-projection of /api/usage
GET  /api/firings
GET  /api/firings/{firing_id}
GET  /api/firings/{firing_id}/tail
GET  /api/plans
GET  /api/plans/drafts
GET  /api/plans/{plan_id}
POST /api/plans/{plan_id}/convert-followup
POST /api/plans/{plan_id}/mark-handled
POST /api/plans/{plan_id}/discard
POST /api/plans/{plan_id}/decision
POST /api/plans/{plan_id}/file-issue
POST /api/plans/draft
POST /api/compose/converse
POST /api/compose/converse/stream
GET  /api/memory/candidates
POST /api/memory/candidates/{id}/promote
POST /api/memory/candidates/{id}/reject
POST /api/queue
GET  /api/setup/status
GET  /api/setup/repos
POST /api/setup/repos
GET  /api/setup/playbooks
POST /api/setup/playbook
POST /api/setup/demo
POST /api/setup/demo/clear
GET  /api/slack/trusted-users
POST /api/slack/trusted-users
POST /api/slack/trusted-users/{user_id}/remove
POST /api/conversation/control
```

`GET /api/usage` is served by `alfred serve` today and backs the capacity rail. It reports your real Claude and Codex subscription headroom for the rolling 5-hour and weekly windows, read from the engines' own local CLI state files on the host.

`GET /api/usage/providers` is also served by `alfred serve` (a flat per-engine re-projection of `/api/usage`), and the same usage numbers are available from the command line with `alfred usage`.

The native client also has a narrow local command allowlist:

```text
alfred serve --port <port> --no-browser
alfred status --json
alfred agents
alfred enabled-agents
alfred auth status
alfred dry-run <codename>
alfred brain doctor --json
alfred brain status --json
alfred brain redis-status --json
alfred brain redis-sync --dry-run --json
alfred brain harvest --apply --json
```

Broader write endpoints should come next, behind command previews:

```text
GET  /api/agents
GET  /api/agents/{codename}
POST /api/agents/{codename}/dry-run
POST /api/agents/{codename}/pause
POST /api/agents/{codename}/resume
GET  /api/doctor
POST /api/doctor/run
```

Write endpoints should return a command preview, a result, and the path or state file they changed.

## Native shell recommendation

Stay on Tauri for Mac and Linux. It keeps the app small, lets the UI reuse the existing site design tokens, and does not force Alfred into a bundled Node/Electron runtime. Electron remains a fallback only if terminal embedding or OS integration becomes the deciding constraint.

Distribution sequence:

1. `alfred serve` read APIs plus local follow-up action contracts with tests. Done.
2. Tauri shell with Inbox, Ask, Work, Agents, Setup, safe local follow-up actions, runtime launch, status, pause/resume/run controls, memory checks, candidate promote/reject, Redis status, Redis sync preview, failure-pattern harvest, and dry-run launch. Done.
3. Guided install, signed update flow, and broader safe write actions with dry-run previews.
4. Signed Mac builds and Linux AppImage/deb artifacts. Done.

## UI direction

Use the Alfred site design system:

- primary display font: Instrument Sans
- secondary UI font: Quicksand
- mono (Fragment Mono) for IDs, command previews, and logs only
- dark-first, high contrast, no decorative gradients
- compact cards for repeated items, not nested card stacks
- stable table-to-card responsive layouts
- links to GitHub and Slack open outside the app

The app should feel like a calm local control surface: dense enough for engineers, legible enough for someone who has never used `launchctl`.
