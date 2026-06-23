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

The [changelog](/about/changelog/) is the detailed, version-by-version ledger. This tier is the short version, so the roadmap stays a forward-looking document.

### v0.5.1 (2026-06-17)

First-run trust polish and the public download path for the signed native client: a public `/download/` page, `alfred serve` and the desktop app aligned on port 7010, and the docs/site brought in line with the shipped client.

### v0.5.0 (2026-06-15)

The native desktop app and the trust features around it: a signed Mac/Linux app over `alfred serve` (Inbox, Ask, Work, Agents, Setup), live Claude and Codex subscription usage, a single-repo approval gate, a disk guardian, an approved-Slack-plan-to-issue bridge, and review-first fleet memory with an optional Redis Agent Memory Server.

### v0.4.0 (2026-05-23)

Substrate, observability, planning, approval, memory, and connector primitives: the `agent_runner` package decomposition, `alfred metrics` / `alfred logs`, the issue-claim state machine and multi-repo coordination, Damian and Batman planning/execution, the FleetBrain ledger, the connector protocol, `alfred serve` v1, and the slop detector.

### v0.3.0 and earlier

Linux support, dry-run mode, fleet control verbs, and the solo-builder setup wizard. See the [changelog](/about/changelog/) for the full ledger.

## In flight (this quarter)

Items with active work and a committed IC.

- **Plan-review gate as a runtime feature.** Promote `plan() -> review_plan() -> execute() -> review_diff()` from an architecture note to the default lifecycle for codenames that opt in. Today the review step exists in prose; the runtime makes it enforceable. IC: core. Effort: M. Issue: TBD.
- **Public unattended-SLA emit format.** Extend `alfred-shipped-public` with a 30-day window covering firings, success rate, and unattended hours. People who want a public usage page can render this on their own site. IC: core. Effort: S. Issue: TBD.
- **Alfred Desktop v2.** Keep Slack as the collaboration surface and build on the packaged Tauri shell with guided setup repair, release/update status, lock recovery, safer command previews, and a first-class Goals inbox with evidence. No extra gateway, no local mirror, no second source of truth. Keep `alfred serve` JSON APIs stable so the Tauri shell stays thin. IC: core. Effort: M. Issue: TBD.
- **Memory quality loop v2.** Improve duplicate collapse, evidence ranking, stale lesson retirement, and approved follow-up execution before a lesson can shape future runs. IC: core. Effort: M. Issue: TBD.

## Next (next quarter)

Committed for the following quarter. Design first, then code.

- **Multi-engine routing v2.** Add Gemini and Ollama adapters alongside the current Claude and Codex engines. Per-codename engine selection stays the existing surface; the work is the adapter contract plus auth probes plus billing posture docs. Effort: M. Issue: TBD.
- **Better Batman v2.** Bundle-completion tracking after approval: Batman keeps watch on child issues and PRs, reports per-repo progress, and closes the bundle once every child has landed or been explicitly dropped. Effort: M. Issue: TBD.
- **Native lifecycle dry-run for every shipped runner.** `alfred dry-run <codename>` now resolves every codename safely; next step is making every individual runner support the full synthetic lifecycle, not just the safe simulation. Effort: S. Issue: TBD.
- **`alfred serve` API hardening.** v0.5.1 ships the local control surface used by Alfred Desktop. Next work is a versioned API contract, compatibility tests, richer event-stream traces, and clearer error payloads for native clients. Effort: S. Issue: TBD.

## Horizon (no committed quarter)

Candidly speculative. No IC, no quarter, no committed effort estimate.

- Content-fleet codename pack (Scribe, Herald, Curator) for blog, LinkedIn, and SEO drafts.
- Sales-fleet codename pack for prospect identification, event-page sourcing, and outreach drafts.
- Marketing and SEO-fleet codename pack for site-page generation, content-drift detection, and search-visibility monitoring.
- Ops-fleet codename pack for uptime, release notes, and customer-health signals.
- Personal-assistant codename pack for inbox triage, calendar, and daily digest.

**How these reach OSS.** Cross-department codename packs build in the private internal orchestrator first, validated against real production usage, then port to OSS once generalised. See the private-to-public boundary workflow for the rules each port has to clear (no local host paths, no internal infra, no customer data, scrubbed prompts).

## Considered, not committed

Decisions considered and left out. Listed so contributors do not re-pitch them.

- **Plugin or skill marketplace bundled into Alfred.** Considered and decided against. Skills are user-installed Claude Code skills; a bundled marketplace would push maintenance onto the framework. The convention-only resolver stays.
- **Hosted Alfred SaaS.** Not on the roadmap. Alfred is self-hosted by design; multi-tenant is a different product.
- **First-class GitHub App** instead of local `gh` auth. Larger onboarding surface; deferred until there is demonstrated demand.
- **Pluggable spend backends** (filesystem, SQLite, Redis). Single-host is the design, so this stays speculative.
- **`pipx` / PyPI install.** Git clone is the supported path today; a packaged install would widen the audience but the install story is fine.

## Design boundaries

These are the design, not missing features.

- **Single-person install.** One person, one host, one config. Not multi-tenant, not a hosted SaaS.
- **The OS schedules; Alfred runs.** No long-running orchestration loop.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` and `codex` through your local CLI auth. The default path uses subscription-backed CLI accounts and does not require provider API keys.
- **Lean on the platform.** Adopt Anthropic-native capabilities (Agent Teams, the Memory Tool) rather than re-implement them.
- **Browser automation is per-codename**: installed in the codename's own bin script.

## Influence

- **Strong**: a working PR for something already on the in-flight or next list.
- **Medium**: a well-scoped feature request with a real use case and a proposal.
- **Low**: "would be cool if" comments.

Want to take Alfred somewhere new? Open a discussion first.
