# Native local client

Status: native preview shipped under `clients/desktop`. Slack is still the
primary collaboration surface, the desktop app is the guided local control
center, and the CLI remains the durable power-user and automation surface.

## Decision

Build the native Mac/Linux client as a thin local control plane and installer,
not a second Alfred runtime.

Slack remains the primary collaboration UI because it already has threads,
reactions, search, mobile push, and shared context. The native client should
make Alfred easier to trust and repair: setup checks, health, logs, approvals,
memory review, safe pause/resume, dry-run launch, and recovery.

The client must not become a hosted gateway, a shadow database, or a softer
copy of Slack. It reads and writes through the same local APIs, state files,
and CLI commands the operator can inspect by hand. Users should be able to run
Alfred with or without the client.

## Product Principles

- One source of truth: `$ALFRED_HOME`, GitHub, Slack threads, and the local
  fleet brain.
- Direct host access: local HTTP on `127.0.0.1` for same-machine installs,
  SSH for remote Linux hosts. No public port, no relay, no sync service.
- Slack-native collaboration: every plan, approval, rejection, and follow-up
  should have a Slack thread link where discussion happened.
- Actionable, not chatty: the home screen answers "what needs me right now?"
  before showing logs or historical data.
- Explain before acting: write actions show the target, expected effect, and
  rollback path before running. The underlying command is audit detail, not the
  primary interface.
- Accessible to technical and non-technical operators: use product language
  first, shell commands second. The app can reveal details without making them
  the default.

## Information Architecture

### Home

The first screen is a decision queue and local command center:

- fleet health
- pending approvals
- blocked plans
- stale workers
- repeated failures
- memory candidates ready for review
- Slack listener health and newly saved planning drafts
- safe actions: refresh, pause all, resume all, open Slack thread, open PR,
  and run the memory doctor

The top row should feel like an operations status strip, not a dashboard full
of vanity metrics.

### Compose

Compose owns plain-language intake and the planning inbox. Plan cards show:

- parent issue and Slack thread
- readiness verdict
- affected repos and rollout order
- open questions
- latest revision summary
- source: local form, Slack DM, app mention, or registered thread
- approve/reject status
- PR chain after execution starts

The app can help draft or refine a spec, but the final collaboration loop stays
in Slack. Any "send to Alfred" action should post to or link back to the
approval thread.

### Fleet

Fleet controls are the operational surface:

- enabled/paused state
- schedule
- last run
- last failure
- dry-run
- pause/resume
- run once
- clear stale lock with proof
- open prompt/config files

Every destructive or state-changing action should have a dry-run preview where
the CLI supports one.

### Logs

Logs combine notifications and firings for forensics:

- timeline by firing
- engine used
- worktree path
- issue and PR links
- event log
- transcript link when present
- final status and next recommended action

Logs should be readable without horizontal scrolling on narrow screens.

### Memory

Memory is reviewable and appears where the operator is already working:

- Home surfaces memory candidates ready for review
- Compose recalls promoted planning hints beside drafts
- Setup exposes memory doctor, Redis status, and explicit Redis sync checks
- Slack exposes `memory`, `remember`, `memory promote`, `memory reject`,
  `memory redis`, and `memory sync`

The app must visibly separate promoted lessons from candidates and raw logs.

### Setup

Setup is a guided doctor:

- install or repair the CLI
- start the local runtime
- GitHub auth
- Slack bot/webhook
- engine CLIs
- launchd or systemd timers
- watched repos
- labels
- memory provider
- browser dependencies for agents that need them

Failures should tell the user what Alfred checked, why it matters, and the
smallest next step.

## UI Direction

Use the Alfred site design system:

- primary display font: Space Grotesk
- secondary UI font: Quicksand
- mono for IDs, command previews, and logs only
- dark-first, high contrast, no decorative gradients
- compact cards for repeated items, not nested card stacks
- stable table-to-card responsive layouts
- links to GitHub and Slack open outside the app

The app should feel like a calm local cockpit: dense enough for engineers,
legible enough for someone who has never used `launchctl`.

## Experience Signature

The client should open to a single question: "What needs attention?"

The top strip should show health, pending approvals, blocked plans, memory
candidates, and stale workers. Each item should lead to a concrete repair or
review action. Historical metrics are useful, but they should never outrank work
that needs a human decision.

Current first viewport:

```text
Alfred
Local fleet healthy · 15 agents · 4 approvals waiting

[Needs decision] Batman plan #42      Open Slack thread
[Needs repair]   Huntress browser     Pause Huntress
[Memory]         3 candidates         Run memory check

Tabs: Home · Compose · Fleet · Logs · Setup gear
```

Interaction rules:

- Every card has one primary action and at most one secondary action.
- Links to GitHub and Slack open outside the app.
- State-changing actions show an affected path, rollback, and command audit
  detail after or before the action.
- Tables collapse into cards before they become horizontally cramped.
- Timestamps render as "5m ago", "yesterday 12:24", or "May 27, 12:24" with
  exact UTC in the tooltip.
- Empty states tell the user what Alfred checked and what to do next.
- The app never hides the local source of truth: every run links to its event
  file or transcript when available.

## Current Tauri Preview

The first client lives at `clients/desktop`:

- Tauri v2 + React + Vite + TypeScript.
- Brand fonts and logo from the Alfred site system.
- Local API calls through Rust commands, not browser `fetch`.
- API calls are restricted to `http://localhost`, `http://127.0.0.1`, or
  `http://[::1]` and to the `/api/status`, `/api/actions`, `/api/firings`,
  `/api/plans`, compose-draft, and follow-up action contracts.
- Links to Slack, GitHub, and local serve detail pages open outside the app.
- The app opens to Home and has Home, Compose, Fleet, and Logs tabs, with Setup
  behind the gear.
- Home shows the decision queue, recent plans, recent runs, memory candidates,
  and fleet-wide pause/resume actions.
- Compose combines plain-language planning intake with saved plans and
  follow-ups.
- Logs combines in-app notifications and firing timelines.
- Follow-up plan cards can call the local `Plan next pass` and `Mark handled`
  endpoints. These actions only move local follow-up files or create local
  planning drafts.
- The Setup gear can start the local runtime, run `alfred status --json`, run
  auth checks, list agents, run the memory doctor, check Redis memory, and
  dry-run an agent through a narrow native allowlist.
- Setup includes a local action console. It is intentionally not an arbitrary
  shell: it runs curated Alfred actions and shows terminal-style output inside
  the client. Browser preview is read-only instead of presenting copy-command
  fallbacks.
- Pause, resume, lock clearing, and memory promotion should become write
  actions only after they return preview/result payloads.

Run it locally:

```bash
cd clients/desktop
npm install
npm run tauri dev
```

The Setup gear can start `alfred serve --no-browser` for you. If port 7000 is
taken, run `alfred serve --port 7010 --no-browser`; the app also probes that
fallback on first load.

## API Shape To Stabilize Next

The client uses these local API contracts today:

```text
GET  /api/status
GET  /api/actions
GET  /api/firings
GET  /api/firings/{firing_id}
GET  /api/plans
GET  /api/plans/{plan_id}
POST /api/plans/{plan_id}/convert-followup
POST /api/plans/{plan_id}/mark-handled
GET  /api/planning-drafts
GET  /api/slack/threads
```

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
```

Broader write endpoints should come next, behind command previews:

```text
GET  /api/agents
GET  /api/agents/{codename}
POST /api/agents/{codename}/dry-run
POST /api/agents/{codename}/pause
POST /api/agents/{codename}/resume
GET  /api/memory/candidates
POST /api/memory/candidates/{id}/promote
POST /api/memory/candidates/{id}/reject
GET  /api/doctor
POST /api/doctor/run
```

Write endpoints should return a command preview, a result, and the path or
state file they changed.

## Native Shell Recommendation

Stay on Tauri for Mac and Linux. It keeps the app small, lets the UI reuse the
existing site design tokens, and does not force Alfred into a bundled
Node/Electron runtime. Electron remains a fallback only if terminal embedding
or OS integration becomes the deciding constraint.

Distribution sequence:

1. `alfred serve` read APIs plus local follow-up action contracts with tests.
   Done.
2. Tauri shell with Home, Compose, Fleet, Logs, Setup, safe local follow-up
   actions, runtime launch, status, pause/resume/run controls, memory checks,
   Redis check, and dry-run launch. Done.
3. Guided install and broader safe write actions with dry-run previews.
4. Signed Mac builds and Linux AppImage/deb artifacts.

## Inspiration

Two Hermes Desktop projects are useful reference points:

- `dodo-reach/hermes-desktop`: the strongest lesson is restraint. The app keeps
  the host as source of truth and avoids a gateway, mirror, or extra sync layer.
- `fathah/hermes-desktop`: the strongest lesson is guided setup. A local client
  can make install, providers, memory, tools, schedules, and logs approachable.
- `Ivy-Interactive/Ivy-Tendril`: the strongest lesson is plan lifecycle
  legibility. Borrow the durable plan states, per-project verification
  profiles, plan health doctor, repair/prune workflow, and recommendations
  inbox. Do not borrow a second scheduler, hosted gateway, or heavyweight
  database as the source of truth.

Alfred should borrow these ideas while staying Slack-native.
