# Roadmap

What's shipped, what's actively being built, what's committed for next quarter, and what's on the horizon. Living doc; updated on every release.

The roadmap has four tiers. Each tier has a different honesty contract:

- **Shipped** is in the tree. You can `git log` it.
- **In flight** has actual work behind it this quarter. IC assigned, scope locked.
- **Next** is committed for the following quarter. Design first, code second.
- **Horizon** is candid about being speculative. No quarter, no IC.

Effort sizing is uniform across tiers: **S** is roughly a week of focused work, **M** is two to four weeks, **L** is a quarter.

## Shipped

What is in the OSS tree today.

### v0.4.1: unreleased

Reliability and first-run trust polish.

- `alfred dry-run <codename>`: scheduler-free dry-run resolution for every shipped codename. Native dry-run runners execute with side effects stubbed; every other codename gets a safe no-side-effect simulation.
- `fleet-github-poll.py` and `alfred github-poll`: local GitHub issue/PR polling into fleet-brain.
- Bundle memory: `agent:bundle:<slug>` and `bundle:<slug>` labels are mirrored into `bundle_items` for Batman-style rollout inspection.
- Worker heartbeat memory: `alfred brain heartbeat`, `alfred brain workers --stale`, and richer doctor output for stale-worker detection.
- Memory promotion loop: `alfred brain promotions` surfaces high-confidence candidates with evidence before they enter recall.
- Reliability governor: `alfred brain failure-patterns` and `alfred brain governor` classify repeated failures into operator actions.
- Optional Redis AMS provider and sync: `ALFRED_MEMORY_PROVIDERS=fleet,redis` lets advanced operators test external semantic memory without changing the default local install; `alfred brain redis-status` and `alfred brain redis-sync` make the bridge inspectable and explicit.
- Planning memory loop: the Planning tab recalls promoted repo lessons while drafting, embeds prompt-safe hints into saved specs, and proposes reviewable spec-to-issue memory candidates when a spec is saved.
- `alfred serve` cockpit polish: the local dashboard now surfaces governor status, repeated failure patterns, stale workers, memory review suggestions, saved Alfred plans, Planning intake, human-readable timestamps, and mobile card layouts.
- Batman plan clarity: Slack plan messages now show actionable titles, GitHub parent links, readiness verdicts, child issue scopes, done-when checks, and explicit approve/reject/reply instructions before child issues are filed.
- Slack planning assistant: Batman approval threads and the local Planning tab now share `acceptance:`, `test:`, `add repo:`, `remove repo:`, and `question:` commands so operators can adjust plans before implementation. Repo add/remove replies are applied to execution scope before child issues or worktrees are created. Trusted Slack feedback users can shape plans without being able to approve them, and explicit `question:` feedback blocks execution until the plan is resolved.
- Slack follow-up loop: trusted replies after Batman reports or PR links are classified as `change`, `fix`, `test`, `question`, `scope`, or notes, acknowledged in-thread, saved under `$ALFRED_HOME/state/followups`, surfaced in Plans as `needs follow-up`, and can be converted into a local planning draft or marked handled without silently approving, merging, or changing code.
- Slack planning listener: optional Socket Mode listener for trusted DMs, app
  mentions, and registered plan/report threads. It writes local planning drafts
  and feedback context without making chat text an approval mechanism.
- Slack trusted collaborators: operators can add or remove local Slack users
  with `trust <@user>` / `untrust <@user>` or the desktop Setup tab. Trusted
  collaborators can discuss plans and create drafts, while execution approval
  remains operator-only.
- Native local client preview: `clients/desktop` ships a Tauri Mac/Linux shell
  over the local Alfred runtime. It opens to "what needs attention?", shows
  Plans, Runs, Agents, Memory, and Setup, opens external links outside the app,
  can start the local runtime, run safe dry-runs and memory checks, and can
  convert trusted follow-ups into planning drafts or mark them handled without
  bypassing Slack approval.
- Plain intake mode: `ALFRED_INTAKE_PROFILE=plain` turns the planning assistant into a non-technical front door. A teammate can describe work in plain language; the assistant asks at most one or two plain questions, hides specs, scope, readiness scores, and PRs, and renders a "Here's what I'll do … OK to go ahead?" plan framed around reviewing a preview. The same structured draft is built invisibly, so the downstream bridge and fleet are unchanged. Default (unset) stays technical. See `docs/PLAIN_MODE.md`.

### v0.4.0: 2026-05-23

Substrate, observability, planning, approval, memory, and connector primitives. Merged to `main` on 2026-05-23 via PRs #89 and #90; tagged as `v0.4.0`.

- `lib/agent_runner.py` decomposed into a 10-file `lib/agent_runner/` package (preflight, lock, spend, engines, gh, slack, event-log, commit-trailer, transcripts, dedup). Public import surface preserved.
- `alfred-metrics` CLI: per-agent firings, cost, success rate, p50/p95 turn count from on-disk state.
- `alfred-logs` CLI: tail and filter per-firing transcripts without grepping `state/` by hand.
- `alfred-label-state` CLI: read-only inspector for the issue-claim state machine across all configured repos.
- Cross-repo PR primitive: `lib/cross_repo_pr.py` for bundles that need to land coordinated PRs across more than one repo.
- Damian spec-bundle planner: a planner codename that turns a spec document into an `agent:large-feature` bundle that Batman can execute.
- `slack_approval`: reaction-based approval gate. An agent posts a proposal, the operator reacts with the configured emoji, the agent proceeds.
- `slop-detector`: PR-time linter for AI-authored prose patterns. Used by the new `curator` codename.
- `curator` codename: documentation hygiene agent. Runs slop-detector against docs PRs, flags drift between code and docs.
- fleet-brain v1: a SQLite-backed memory layer. Per-fleet, optional, zero external dependency. Backs the `MemoryProvider` protocol.
- fleet-brain reliability tools: reviewable memory candidates, failure-event history, `alfred brain doctor`, and a read-only memory MCP bridge.
- `MemoryProvider` protocol plus `gbrain` bridge: agents can read and write to a memory store through a stable interface; the OSS reference is fleet-brain, and operators can drop in their own.
- `alfred spec`: template and lint helpers for specs-driven development.
- `Connector` protocol with reference implementations for Linear (issue handoff) and Sentry (read-only error pulls).
- Batman execute-after-approval: once a bundle plan is approved, Batman now executes the per-repo PR sequence rather than stopping at the plan.
- `alfred serve` v1: read-only local dashboard over `state/` and per-firing transcripts. Live firing feed, per-agent trends, single-firing trace tree.
- `alfred-shipped-public` emitter: a self-host CLI that reads `$ALFRED_HOME/state`, scrubs against a public field allowlist and a partner-name redaction table, and writes a `weekly.json` that operators can publish on their own site if they want a public proof page. The canonical alfred-os site does not host a live rendering; operators decide whether to publish.
- Three new concept pages on the docs site covering the memory protocol, the connector protocol, and the approval gate.

### v0.3.0 and earlier

See [`CHANGELOG.md`](CHANGELOG.md) for the full ledger.

## In flight (this quarter)

Items with active work and a committed IC.

- **Plan-review gate as a runtime feature.** Promote `plan() -> review_plan() -> execute() -> review_diff()` from an architecture note to the default lifecycle for codenames that opt in. Today the review step exists in prose; the runtime makes it enforceable. IC: core. Effort: M. Issue: TBD.
- **Public unattended-SLA emit format.** Extend `alfred-shipped-public` with a 30-day rolling window covering firings, success rate, and unattended hours. Operators who want a public proof page can render this on their own site. IC: core. Effort: S. Issue: TBD.
- **Native local client v2.** Slack remains the primary planning and collaboration UI. Build on the preview client with signed Mac builds, Linux artifacts, guided install, credentials, health, logs, safe pause/resume, lock clearing, dry-run launch, recovery, and memory promotion actions. Favor a direct-host model inspired by Hermes Desktop: no extra gateway, no local mirror, no second source of truth. Borrow Ivy Tendril's plan lifecycle clarity, project verification profiles, plan-health doctor, and recommendations inbox without adopting a second scheduler or source-of-truth database. Keep `alfred serve` JSON APIs stable so the Tauri shell stays thin rather than becoming a second runtime. IC: core. Effort: M. Issue: TBD.
- **fleet-brain v2.** Replace the SQLite layer with PGLite plus Apache AGE for graph queries and pgvector for semantic recall, exposed through an MCP server adapter so other Claude Code consumers can read fleet memory. IC: core. Effort: L. Issue: TBD.
- **Memory quality loop v2.** Add evidence-linked lesson promotion, approved follow-up execution for governor findings, spec-to-issue memory, and lightweight candidate quality checks before promotion. IC: core. Effort: M. Issue: TBD.

## Next (next quarter)

Committed for the following quarter. Design first, then code.

- **Multi-engine routing v2.** Add Gemini and Ollama adapters alongside the current Claude and Codex engines. Per-codename engine selection stays the existing surface; the work is the adapter contract plus auth probes plus billing posture docs. Effort: M. Issue: TBD.
- **Better Batman v2.** Post-approval per-repo execution sweep with bundle-completion detection: Batman keeps watch on the PR chain it opened, reports per-repo progress, and closes the bundle once every PR has landed or been explicitly dropped. Effort: M. Issue: TBD.
- **Native lifecycle dry-run for every shipped runner.** `alfred dry-run <codename>` now resolves every codename safely; next step is making every individual runner support the full synthetic lifecycle, not just the safe simulation. Effort: S. Issue: TBD.
- **`alfred serve` v2.** v0.4.0 shipped a read-only localhost dashboard ([`docs/SERVE.md`](docs/SERVE.md)) with three views: fleet status, recent firings, single-firing detail. v2 adds per-agent cost and success trends, full trace tree per firing, and transcript pretty-printing. Cross-platform precursor to any future native menu-bar UI. Effort: S. Issue: TBD.

## Horizon (no committed quarter)

Candidly speculative. No IC, no quarter, no committed effort estimate.

- Content-fleet codename pack (Scribe, Herald, Curator) for blog, LinkedIn, and SEO drafts.
- Sales-fleet codename pack for prospect identification, event-page sourcing, and outreach drafts.
- Marketing and SEO-fleet codename pack for site-page generation, content-drift detection, and search-visibility monitoring.
- Ops-fleet codename pack for uptime, release notes, and customer-health signals.
- Personal-assistant codename pack for inbox triage, calendar, and daily digest.

**How these reach OSS.** Cross-department codename packs build in the private operator orchestrator first, validated against real production usage, then port to OSS once generalised. See the private-to-public boundary workflow for the rules each port has to clear (no operator paths, no internal infra, no customer data, scrubbed prompts).

## Considered, not committed

Decisions on the table that did not make the cut. Listed so contributors do not re-pitch them.

- **Plugin or skill marketplace bundled into Alfred.** Considered and decided against. Skills are operator-installed Claude Code skills; a bundled marketplace would push maintenance onto the framework. The convention-only resolver stays.
- **Hosted Alfred SaaS.** Not on the roadmap. Alfred is operator-hosted by design; multi-tenant is a different product.
- **First-class GitHub App** instead of the operator's `gh` PAT. Larger onboarding surface; deferred until there is demonstrated demand.
- **Pluggable spend backends** (filesystem, sqlite, Redis). Single-host is the design, so this stays speculative.
- **`pipx` / PyPI install.** Git clone is the supported path today; a packaged install would widen the audience but the install story is fine.

## Design boundaries

Alfred has a deliberate shape. These are not missing features; they are the design.

- **Single operator.** One person, one host, one config. Alfred is not multi-tenant and will not become a hosted SaaS. It is software you install and run yourself.
- **The OS schedules; Alfred runs.** No long-running orchestration loop. `launchd` and `systemd` own cadence; each firing is a fresh, isolated process. Better failure isolation, and it survives reboots.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` and `codex` through your local CLI auth. The default path uses subscription-backed CLI accounts and does not require provider API keys.
- **Lean on the platform.** When Anthropic ships a capability natively (Agent Teams, the Memory Tool), Alfred adopts it rather than re-implementing it.
- **Browser automation is per-codename.** If a codename needs a browser, it installs Playwright in its own bin script. The core stays lean.

## Influence

- **Strong.** A working PR for something already on the in-flight or next list.
- **Medium.** A well-scoped feature request with a real use case and a proposal.
- **Low.** "Would be cool if" comments.

Want to take Alfred somewhere new, like a new department or a runtime change? Open a discussion first, so the design fits before the code does.
