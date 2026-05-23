---
title: Roadmap
description: Shipped, in flight, committed for next quarter, and on the horizon. Plus the design boundaries that stay.
---

What's shipped, what's actively being built, what's committed for next quarter, and what's on the horizon. Living doc; updated on every release.

Full source at [`ROADMAP.md`](https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md). The roadmap has four tiers, each with a different honesty contract:

- **Shipped** is in the tree. You can `git log` it.
- **In flight** has actual work behind it this quarter. IC assigned, scope locked.
- **Next** is committed for the following quarter. Design first, code second.
- **Horizon** is candid about being speculative. No quarter, no IC.

Effort sizing is uniform across tiers: **S** is roughly a week of focused work, **M** is two to four weeks, **L** is a quarter.

## Shipped

### v0.4.0 (staging)

Substrate, observability, and approval primitives. Currently being staged on the `positioning/operations-room` branch.

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
- `MemoryProvider` protocol plus `gbrain` bridge: agents read and write to a memory store through a stable interface; the OSS reference is fleet-brain, and operators can drop in their own.
- `Connector` protocol with reference implementations for Linear (issue handoff) and Sentry (read-only error pulls).
- Batman execute-after-approval: once a bundle plan is approved, Batman now executes the per-repo PR sequence rather than stopping at the plan.
- [`alfred serve`](/concepts/architecture/) v1: read-only local dashboard over `state/` and per-firing transcripts. Live firing feed, per-agent trends, single-firing trace tree.
- `alfred-shipped-public` emitter: a self-host CLI that reads `$ALFRED_HOME/state`, scrubs against a public field allowlist and a partner-name redaction table, and writes a `weekly.json` that operators can publish on their own site if they want a public proof page. The canonical alfred-os site does not host a live rendering; operators decide whether to publish.
- Three new concept pages covering the memory protocol, the connector protocol, and the approval gate.

### v0.3.0 and earlier

See the [changelog](/about/changelog/) for the full ledger.

## In flight (this quarter)

Items with active work and a committed IC.

- **Plan-review gate as a runtime feature.** Promote `plan() -> review_plan() -> execute() -> review_diff()` from an architecture note to the default lifecycle for codenames that opt in. Today the review step exists in prose; the runtime makes it enforceable. IC: core. Effort: M. Issue: TBD.
- **Public unattended-SLA emit format.** Extend `alfred-shipped-public` with a 30-day rolling window covering firings, success rate, and unattended hours. Operators who want a public proof page can render this on their own site. IC: core. Effort: S. Issue: TBD.
- **Cross-platform menubar app.** A small native menubar (macOS first, Linux tray second) that surfaces fleet status and click-throughs to the local `alfred serve` UI. Read-only. IC: core. Effort: M. Issue: TBD.
- **fleet-brain v2.** Replace the SQLite layer with PGLite plus Apache AGE for graph queries and pgvector for semantic recall, exposed through an MCP server adapter so other Claude Code consumers can read fleet memory. IC: core. Effort: L. Issue: TBD.

## Next (next quarter)

Committed for the following quarter. Design first, then code.

- **Multi-engine routing v2.** Add Gemini and Ollama adapters alongside the current Claude and Codex engines. Per-codename engine selection stays the existing surface; the work is the adapter contract plus auth probes plus billing posture docs. Effort: M. Issue: TBD.
- **Spec linter and template generator.** `alfred spec lint` checks a spec file for missing acceptance criteria, missing test plan, and other lifecycle gaps. `alfred spec new` scaffolds a fresh spec from a template. Effort: S. Issue: TBD.
- **Better Batman v2.** Post-approval per-repo execution sweep with bundle-completion detection: Batman keeps watch on the PR chain it opened, reports per-repo progress, and closes the bundle once every PR has landed or been explicitly dropped. Effort: M. Issue: TBD.
- **`alfred dry-run <codename>` for every shipped codename.** Today dry-run is wired through specific runners; widen it so every codename in the tree runs end-to-end with no side effects. Effort: S. Issue: TBD.

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

These are the design, not missing features.

- **Single operator.** One person, one host, one config. Not multi-tenant, not a hosted SaaS.
- **The OS schedules; Alfred runs.** No long-running orchestration loop.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` and `codex` through your local CLI auth. The default path uses subscription-backed CLI accounts and does not require provider API keys.
- **Lean on the platform.** Adopt Anthropic-native capabilities (Agent Teams, the Memory Tool) rather than re-implement them.
- **Browser automation is per-codename**: installed in the codename's own bin script.

## Influence

- **Strong**: a working PR for something already on the in-flight or next list.
- **Medium**: a well-scoped feature request with a real use case and a proposal.
- **Low**: "would be cool if" comments.

Want to take Alfred somewhere new? Open a discussion first.
