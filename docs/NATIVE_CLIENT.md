# Alfred Desktop

Status: Alfred Desktop ships under `clients/desktop` with signed macOS
packages and Linux artifacts. Slack is still the
primary collaboration surface, the desktop app is the guided local control
center, and the CLI remains the durable power-user and automation surface.

## Decision

Build the native Mac/Linux app as a thin local control surface and installer,
not a second Alfred runtime.

Slack remains the primary collaboration UI because it already has threads,
reactions, search, mobile push, and shared context. The native client should
make Alfred easier to trust and repair: setup checks, health, logs, approvals,
memory review, safe pause/resume, dry-run launch, and recovery.

The client reads and writes through the same local APIs, state files, and CLI
commands Alfred already uses. Users should be able to run Alfred with or
without the client, and Slack remains the collaboration surface.

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
- Accessible to technical and non-technical users: use product language
  first, shell commands second. The app can reveal details without making them
  the default.

## Information Architecture

### Inbox

Inbox is the first screen: a decision queue and local command center.

- fleet health
- Claude and Codex subscription headroom on the Inbox capacity rail (backed by
  the live `GET /api/usage` endpoint)
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

### Ask

Ask owns plain-language intake and the planning inbox. Plan cards show:

- parent issue and Slack thread
- readiness verdict
- affected repos and rollout order
- open questions
- latest revision summary
- source: local form, Slack DM, app mention, or registered thread
- approve/reject status
- PR chain after execution starts

The Ask intake leads with a stable "New request" eyebrow rather than a
mode-named label, because the plain-language vs technical mode is already shown
by the toggle beside it.

The app can help draft or refine a spec, but the final collaboration loop stays
in Slack. Any "send to Alfred" action should post to or link back to the
approval thread. A locally drafted single-repo issue lands behind an
approval gate (`agent:plan-pending-approval`) and is held from
autonomous pickup until you approve it, so nothing single-repo ships
without a go-ahead.

### Agents

Agents is the operational surface. The agent roster is the default
view: a cinematic deck of themed agent cards, each carrying the agent's accent,
status, cadence, runs-today, latest signal, and a monogram. A Cinematic / List
toggle (persisted to local storage) switches to a dense list when you want
every agent at a glance. Motion respects `prefers-reduced-motion`, and the
cards are real buttons announced as actionable controls.

Per-agent controls:

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
- step-level run events: real lifecycle milestones (plan created, worktree
  created, pre-push checks passed, branch pushed, PR opened) emitted at the
  moment the underlying action succeeds, so the timeline shows real progress
  rather than only start and stop
- engine used
- worktree path
- issue and PR links
- event log
- transcript link when present
- final status and next recommended action

Logs should be readable without horizontal scrolling on narrow screens.

### Memory

Memory is reviewable and appears where you are already working:

- Home surfaces memory candidates ready for review
- Compose recalls promoted planning hints beside drafts
- Memory exposes candidate promote/reject, memory doctor, Redis status, Redis
  sync preview, and repeated-failure harvest
- Setup keeps memory and Redis checks available as repair actions
- Slack exposes `memory`, `remember`, `memory remember`, `memory promote`,
  `memory reject`, `memory redis`, and `memory sync`

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

- primary display font: Instrument Sans
- secondary UI font: Quicksand
- mono (Fragment Mono) for IDs, command previews, and logs only
- dark-first, high contrast, no decorative gradients
- compact cards for repeated items, not nested card stacks
- stable table-to-card responsive layouts
- links to GitHub and Slack open outside the app

The app should feel like a calm local control surface: dense enough for engineers,
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

Tabs: Inbox · Ask · Work · Agents · Setup
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

## Current Tauri Client

The shipped client lives at `clients/desktop`:

- Tauri v2 + React + Vite + TypeScript.
- Brand fonts and logo from the Alfred site system.
- Local API calls through Rust commands, not browser `fetch`.
- API calls are restricted to `http://localhost`, `http://127.0.0.1`, or
  `http://[::1]` and to the `/api/status`, `/api/actions`, `/api/usage`,
  `/api/firings`, `/api/plans`, compose-draft, and follow-up action contracts.
- Local plan and firing details stay in native inspector panes; only explicit
  Slack and GitHub links open outside the app.
- The app opens to Inbox and has Inbox, Ask, Work, Agents, and Setup surfaces.
- Public releases start as draft GitHub Releases. Signed and notarized macOS
  assets, plus Linux AppImage and Debian packages, are attached before the
  release is published. Local `npm run tauri -- build` still produces
  host-native bundles for inspection.
- Inbox shows the decision queue, the Claude and Codex capacity rail (backed by
  the live `GET /api/usage` endpoint), recent plans, recent runs,
  memory candidates, and fleet-wide pause/resume actions.
- Agents defaults to the cinematic agent roster with a Cinematic / List toggle
  persisted to local storage.
- Ask combines plain-language planning intake with saved plans and
  follow-ups.
- Agents Activity combines in-app notifications and firing timelines.
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
- Pause, resume, dry-run, follow-up handling, and memory promotion are native
  write actions behind the allowlist; destructive actions still need explicit
  preview or confirmation before they run.

Run it locally:

```bash
cd clients/desktop
npm install
npm run tauri dev
```

The Setup gear can start `alfred serve --port 7010 --no-browser` for you. The
app uses 7010 because macOS can reserve 7000 for Control Center. Legacy saved
`7000` URLs are treated as stale local configuration and rewritten to 7010.

## API Shape To Stabilize Next

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

`GET /api/usage` is served by `alfred serve` today and backs the capacity rail.
It reports your real Claude and Codex subscription headroom for the
rolling 5-hour and weekly windows, read from the engines' own local CLI state
files on the host. Alfred drives Claude Code and Codex through their local
subscription CLIs rather than API keys, so there is no billing API to query and
no per-token dollar figure (that number is meaningless under a Max or Pro
subscription). A window the local state cannot confirm reads as not synced
rather than a fabricated number.

`GET /api/usage/providers` is also served by `alfred serve` (a flat per-engine
re-projection of `/api/usage`), and the same usage numbers are available from
the command line with `alfred usage`.

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
2. Tauri shell with Inbox, Ask, Work, Agents, Setup, safe
   local follow-up actions, runtime launch, status, pause/resume/run controls,
   memory checks, candidate promote/reject, Redis status, Redis sync preview,
   failure-pattern harvest, and dry-run launch. Done.
3. Guided install, signed update flow, and broader safe write actions with
   dry-run previews.
4. Signed Mac builds and Linux AppImage/deb artifacts. Done.

## Inspiration

Two Hermes Desktop projects are useful reference points:

- `dodo-reach/hermes-desktop`: the strongest lesson is restraint. The app keeps
  the host as source of truth and keeps sync layers out of the critical path.
- `fathah/hermes-desktop`: the strongest lesson is guided setup. A local client
  can make install, providers, memory, tools, schedules, and logs approachable.
- `Ivy-Interactive/Ivy-Tendril`: the strongest lesson is plan lifecycle
  legibility. Borrow the durable plan states, per-project verification
  profiles, plan health doctor, repair/prune workflow, and recommendations
  inbox. Keep Alfred's existing scheduler and local state as the source of
  truth.

Alfred should borrow these ideas while staying Slack-native.
