---
title: Roadmap
description: Shipped, in flight, where Alfred is going, and the design boundaries that stay.
---

Full roadmap at [`ROADMAP.md`](https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md). The shape:

## Shipped

- Framework substrate: preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table.
- launchd plist template + render.sh + deploy.sh.
- [Linux support](/guides/linux/): `systemd --user` timers, `systemd/render.sh`, `lib/scheduler.py` host abstraction, and a Debian/Ubuntu apt lane in `install.sh`.
- doctor.sh: fleet-wide preflight under `ALFRED_DOCTOR=1`, plus `--dev` mode for dev installs.
- `alfred claude`: two-account swap helper.
- [Issue claim state machine](/concepts/state-machine/) (`agent:in-flight` → `agent:pr-open` → `agent:done`) with race resolution + stale sweep.
- [Slack severity routing](/concepts/severity-routing/) (`info` / `warn` / `alert`).
- `install.sh` + `INSTALL.md` for fresh-machine bootstrap.
- Setup walkthroughs: Slack, AWS, Claude Code, skills, Linux, your-first-agent tutorial.
- [Operator CLI](/reference/cli/): label-state + pre-push hook.
- CI (pytest + ruff + mypy + shellcheck + scrub-check) on every PR.
- Release automation (tag → GitHub release with auto-extracted changelog).
- Project hygiene: COC, security, support, issue templates, PR template, dependabot.
- pyproject.toml (ruff + mypy), pre-commit config.
- Homebrew formula pinned to the latest public release tarball.
- This Astro Starlight docs site.

## In flight (next release)

- **Bot token integration** (`xoxb-…`). Unlocks `slack_set_channel_topic()`, threaded `chat.postMessage` for daily-thread routing of `info`-tier messages, reactions API.
- **Drake-style proactive title-token dedup**. Runner-level guard before invoking the planner.
- **`claim_pr` / `release_pr`**. Extend the state machine to PR-level work.
- **Spend dashboards**. Render a weekly recap from per-agent spend files.
- **`alfred new-codename` scaffold**. Single command to add a fresh codename agent.

## Beyond engineering: the solo builder's agent OS

The default install ships the engineering fleet. The harness underneath it is department-agnostic. The private fleet Alfred OS was extracted from already runs content, sales, and ops agents on the same substrate. That's the direction: Alfred OS as the solo builder's whole agent OS, one department at a time.

- **Content**: blog / LinkedIn / SEO drafts, site-page generation, content-drift detection. Human-in-the-loop on publish.
- **Sales / SDR**: prospect identification, event-page sourcing, outreach drafts. Human-in-the-loop on send.
- **Personal assistant**: inbox triage, calendar, daily digest. Drafts only.
- **Finance ops**: invoice generation, bank reconciliation, subscription audit. Drafts only.
- **Product ops / SRE**: uptime monitoring, release notes, customer-health signals.

Each department is its own integration surface and per-codename prompt design. One codename per PR, with prompt + tests + docs.

## On the horizon

- **A memory layer**: a recall/reflect layer so an agent starts a firing with what the last firings learned. Optional, zero-dependency, per-fleet.
- **`alfred serve`**: a local read-model + UI over `state/` and per-firing transcripts: live firing feed, cost and success trends, the trace tree for one firing. Read-only and local.

## Considered, not committed

- MCP server adapter (expose read-only fleet status + scoped tools).
- First-class GitHub App (vs the operator's PAT).
- Pluggable spend backends.
- Plugin system for skills.
- `pipx` / PyPI install.

## Design boundaries

These are the design, not missing features.

- **Single operator.** One person, one host, one config. Not multi-tenant, not a hosted SaaS.
- **The OS schedules; Alfred runs.** No long-running orchestration loop.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` / `codex` on your own subscription.
- **Lean on the platform.** Adopt Anthropic-native capabilities (Agent Teams, the Memory Tool) rather than re-implement them.
- **Browser automation is per-codename**: installed in the codename's own bin script.

## Influence

- **Strong**: a working PR for something on the in-flight or roadmap list.
- **Medium**: a well-scoped feature request with a real use case + proposal.
- **Low**: "would be cool if" comments.

Want to take Alfred somewhere new? Open a discussion first.
