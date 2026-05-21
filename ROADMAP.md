# Roadmap

What's shipped, what's next, where Alfred is going, and the design boundaries that stay. Living doc; updated on every release.

## Shipped (v0.2.1)

The default install ships a working engineering agent fleet. After `bash install.sh && ./bin/alfred-init.py`, an operator has:

**Substrate**
- `lib/agent_runner.py`: preflight, lock, spend, Claude/Codex engine adapters, gh, slack, event-log, commit-trailer.
- Issue claim state machine: `agent:in-flight` → `agent:pr-open` → `agent:done` with race resolution + stale-claim sweep.
- Slack severity routing: `info` / `warn` / `alert`.
- launchd plist template + render.sh + agents.conf format.
- `bin/doctor.sh`, `alfred claude`, `deploy.sh`.

**Cross-platform (unreleased, in `[Unreleased]`)**
- Linux support via `systemd --user` timers: `systemd/` template + render.sh, `deploy.sh` host detection, `install.sh` Debian/Ubuntu apt lane.
- `alfred pause` / `resume` / `run` operator verbs on a launchd/systemd scheduler abstraction (`lib/scheduler.py`). See [`docs/LINUX.md`](docs/LINUX.md).

**Engineering agents** (Batman codenames by default; renameable per role at install time)
- `lucius`: feature dev (picks `agent:implement` issues, opens PRs).
- `drake`: issue planner (files `agent:implement` issues from specs / roadmap).
- `batman`: opt-in cross-repo coordinator (plans `agent:large-feature` bundles in OSS).
- `bane`: test coverage (writes tests for low-coverage changed files).
- `rasalghul`: multi-axis PR review.
- `nightwing`: review-fix (lands P0/P1 fixes on `agent:authored` PRs).
- `robin`: bug triage (severity classification, repro requests).
- `huntress`: post-deploy E2E smoke (Playwright against staging).
- `gordon`: daily ECS drift + Sentry top-N read.
- `automerge`: squash-merge of clean `agent:authored` PRs.
- `agent-cleanup`: daily housekeeping (worktrees, stuck locks, stale claims).
- `code-map-refresh`: cross-repo contract scan.
- `agent-morning-brief`, `fleet-recap`: Slack digest cron.

**Operator surface**
- `alfred-init`: interactive and non-interactive installer wizard (Slack webhook, AWS choice, starter/all/custom agent selection, per-role codename, explicit repo selection, prompt seeding, GitHub label setup).
- `alfred` CLI: `agents / enable / disable / enabled-agents / engine status / engine set`.
- Example state-machine CLI (`examples/bin/label_state.py`): `claim / release / dedup-check / status-issue / repo / sweep-claims`.
- Pre-push git hook (`examples/git-hooks/pre-push`): refuses pushes that race in-flight agents.

**Project hygiene**
- CI (pytest 3.11/3.12/3.13 + ruff + mypy + shellcheck + scrub-check) on every PR.
- Release automation (tag → GitHub release with auto-extracted changelog notes + brew sha256).
- Code of conduct, security policy, support, issue templates, PR template, dependabot.
- Astro Starlight docs site at `alfred.luminik.io` (env-overridable for forks/custom domains).
- Homebrew formula pinned to the latest public release tarball.

## In flight (next release)

- **Bot token operations**: `lib/slack_format.py` already supports threaded Block Kit messages when a bot token is configured. Follow-up work: `slack_set_channel_topic()` for fleet status, reactions API for ack-without-replying, and a documented daily-thread routing policy.
- **Drake-style proactive title-token dedup**: runner-level guard before invoking the planner. Catches "two issues for the same work"; complements the issue-claim state machine which catches "two actors on the same issue."
- **`claim_pr` / `release_pr`**: extend the state machine to PR-level work (review-fix agents that race to land patches on the same PR).
- **Spend dashboards**: render a weekly recap (turns, cost, success rate per agent) for `fleet-recap`.
- **`alfred new-codename` scaffold**: single command to add a fresh codename agent (script template + agents.conf entry + label registration).
- **Full OSS Batman execution chain**: approval gate, sequenced worktrees, per-repo PR chain, and cleanup. The current OSS Batman is deliberately plan-only; private fleets can layer execution on top today.
- **Provider auth guardrails**: keep the default path subscription-backed through Claude Code / Codex CLI auth, with diagnostics when `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` would shift usage toward API billing.
- **MCP server adapter**: expose read-only fleet status plus carefully scoped `claim_issue` / `release_issue` / `slack_post(severity)` tools so other Claude Code consumers can call them directly. This should use `${ALFRED_HOME}` and remain optional.
- **Optional Hermes bridge**: Hermes now has persistent `/goal`, gateway-driven cron, Kanban worker profiles, MCP, skills, memory, and dashboards. Alfred should integrate by exposing status/events and by accepting GitHub issue/label handoffs, not by making Hermes a setup dependency or letting Hermes mutate Alfred worktrees/state directly.

## Beyond engineering: the solo builder's agent OS

The default install ships the **engineering fleet**. But the harness underneath it (`claude_invoke`, `slack_post`, the issue-claim state machine, per-agent spend caps, per-firing worktrees, the codename pattern) is department-agnostic. Alfred was extracted from a private fleet that already runs content, sales, and ops agents on the same substrate. That is the direction: Alfred as the solo builder's whole agent OS, one department at a time.

Each department is its own integration surface (Apollo / Reddit / Gmail / Stripe / Sentry SDKs) and its own per-codename prompt design. They land incrementally, one codename per PR, with prompt + tests + docs. PRs welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

- **Content**: blog / LinkedIn / SEO drafts, site-page generation, content-drift detection. Human-in-the-loop on publish.
- **Sales / SDR**: prospect identification, event-page sourcing, outreach drafts. Human-in-the-loop on send.
- **Personal assistant**: inbox triage, calendar, daily digest. Drafts only; never sends.
- **Finance ops**: invoice generation, bank reconciliation, subscription audit. Drafts only; never moves money.
- **Product ops / SRE**: uptime monitoring, release notes, customer-health signals.

## On the horizon

Substrate work that makes a growing fleet observable and self-improving.

- **A memory layer.** Today each firing is near-stateless apart from GitHub labels. A doc- or SQLite-shaped recall/reflect layer would let an agent start a firing with what the last firings on the same code learned. Optional, zero-dependency, per-fleet.
- **`alfred serve`, a local read-model + UI.** A small local app over `state/` and the per-firing transcripts: a live firing feed, per-agent cost and success trends, the trace tree for one firing. Read-only and local, the operator's pane of glass rather than a hosted dashboard.

## Considered, not committed

- **First-class GitHub App** instead of the operator's `gh` PAT, with scoped per-agent permissions. Bigger onboarding surface; defer until there's demonstrated demand.
- **Pluggable spend backends** (filesystem / sqlite / Redis). Single-host is the design, so this stays speculative.
- **Plugin system for skills.** Today skills are operator-installed Claude Code skills; a bundled `skills/` directory would push maintenance onto the framework.
- **`pipx` / PyPI install.** Git clone is the supported path today; a packaged install would widen the audience.

## Design boundaries

Alfred has a deliberate shape. These are not missing features; they are the design.

- **Single operator.** One person, one host, one config. Alfred is not multi-tenant and will not become a hosted SaaS. It is software you install and run yourself.
- **The OS schedules; Alfred runs.** No long-running orchestration loop. `launchd` / `systemd` own cadence; each firing is a fresh, isolated process. That means better failure isolation, and it survives reboots.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` / `codex` through your local CLI auth. The default path uses subscription-backed CLI accounts and does not require provider API keys.
- **Lean on the platform.** When Anthropic ships a capability natively (Agent Teams, the Memory Tool), Alfred adopts it rather than re-implementing it.
- **Browser automation is per-codename.** If a codename needs a browser, it installs Playwright in its own bin script. The core stays lean.

## Influence

- **Strong**: a working PR for something already on the in-flight or roadmap list.
- **Medium**: a well-scoped feature request with a real use case and a proposal.
- **Low**: "would be cool if" comments.

Want to take Alfred somewhere new, like a new department or a substrate change? Open a discussion first, so the design fits before the code does.
