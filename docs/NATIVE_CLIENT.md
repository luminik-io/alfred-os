# Native local client

Status: design target. The shipped surface is still Slack, CLI, and
`alfred serve`.

## Decision

Build the native Mac/Linux client as a thin local control plane, not a second
Alfred runtime.

Slack remains the primary collaboration UI because it already has threads,
reactions, search, mobile push, and shared context. The native client should
make Alfred easier to trust and repair: setup checks, health, logs, approvals,
memory review, safe pause/resume, dry-run launch, and recovery.

The client must not become a hosted gateway, a shadow database, or a softer
copy of Slack. It reads and writes through the same local APIs, state files,
and CLI commands the operator can inspect by hand.

## Product Principles

- One source of truth: `$ALFRED_HOME`, GitHub, Slack threads, and the local
  fleet brain.
- Direct host access: local HTTP on `127.0.0.1` for same-machine installs,
  SSH for remote Linux hosts. No public port, no relay, no sync service.
- Slack-native collaboration: every plan, approval, rejection, and follow-up
  should have a Slack thread link where discussion happened.
- Actionable, not chatty: the home screen answers "what needs me right now?"
  before showing logs or historical data.
- Explain before acting: write actions show the exact command, target, and
  rollback path before running.
- Non-technical friendly: use product language first, shell commands second.
  The app can reveal details without making them the default.

## Information Architecture

### Command Center

The first screen is a decision queue:

- fleet health
- pending approvals
- blocked plans
- stale workers
- repeated failures
- memory candidates ready for review
- safe actions: run doctor, pause one codename, open Slack thread, open PR

The top row should feel like an operations status strip, not a dashboard full
of vanity metrics.

### Plans

Plan cards show:

- parent issue and Slack thread
- readiness verdict
- affected repos and rollout order
- open questions
- latest revision summary
- approve/reject status
- PR chain after execution starts

The app can help draft or refine a spec, but the final collaboration loop stays
in Slack. Any "send to Alfred" action should post to or link back to the
approval thread.

### Memory

Memory is reviewable:

- recalled planning hints shown beside the draft
- new spec-to-issue lessons proposed as candidates
- evidence links before promotion
- promote, reject, retire, or open source evidence
- optional Redis AMS status and explicit sync for reviewed lessons

The app must visibly separate promoted lessons from candidates and raw logs.

### Runs

Runs are for forensics:

- timeline by firing
- engine used
- worktree path
- issue and PR links
- event log
- transcript link when present
- final status and next recommended action

Logs should be readable without horizontal scrolling on narrow screens.

### Agents

Agent controls:

- enabled/paused state
- schedule
- last run
- last failure
- dry-run
- pause/resume
- clear stale lock with proof
- open prompt/config files

Every destructive or state-changing action should have a dry-run preview.

### Setup

Setup is a guided doctor:

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

Suggested first viewport:

```text
Alfred
Local fleet healthy · 15 agents · 4 approvals waiting

[Needs decision] Batman plan #42      Open Slack thread  Review scope
[Needs repair]   Huntress browser     Install browsers   Pause Huntress
[Memory]         3 candidates         Review evidence    Promote selected

Tabs: Command Center · Plans · Runs · Agents · Memory · Setup
```

Interaction rules:

- Every card has one primary action and at most one secondary action.
- Links to GitHub and Slack open outside the app.
- State-changing actions show a command preview, affected path, and rollback.
- Tables collapse into cards before they become horizontally cramped.
- Timestamps render as "5m ago", "yesterday 12:24", or "May 27, 12:24" with
  exact UTC in the tooltip.
- Empty states tell the user what Alfred checked and what to do next.
- The app never hides the local source of truth: every run links to its event
  file or transcript when available.

## API Shape To Stabilize First

Before a native shell, `alfred serve` should expose JSON endpoints.

Read-only endpoints are the first contract:

```text
GET  /api/status
GET  /api/actions
GET  /api/firings
GET  /api/firings/{firing_id}
GET  /api/plans
GET  /api/plans/{plan_id}
```

Write endpoints should come next, behind command previews:

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

Start with Tauri for Mac and Linux once the API contracts above are stable.
It keeps the app small, lets the UI reuse the existing site design tokens, and
does not force Alfred into a bundled Node/Electron runtime. Electron remains a
fallback only if terminal embedding or OS integration becomes the deciding
constraint.

Distribution sequence:

1. `alfred serve` API contracts with tests.
2. Tauri shell with read-only Command Center, Plans, Runs, and Memory.
3. Safe write actions with dry-run previews.
4. Signed Mac builds and Linux AppImage/deb artifacts.

## Inspiration

Two Hermes Desktop projects are useful reference points:

- `dodo-reach/hermes-desktop`: the strongest lesson is restraint. The app keeps
  the host as source of truth and avoids a gateway, mirror, or extra sync layer.
- `fathah/hermes-desktop`: the strongest lesson is guided setup. A local client
  can make install, providers, memory, tools, schedules, and logs approachable.

Alfred should borrow both ideas while staying Slack-native.
