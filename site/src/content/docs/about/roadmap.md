---
title: Roadmap
description: What's shipped, what's in flight, what's deliberately out of scope.
---

The full roadmap lives at [`ROADMAP.md`](https://github.com/luminik-io/alfred-os/blob/main/ROADMAP.md). The shape:

## Shipped

- Framework substrate: preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table.
- launchd plist template + render.sh + deploy.sh.
- doctor.sh — fleet-wide preflight under `HERMES_DOCTOR=1`.
- hermes-claude — two-account swap helper.
- [Issue claim state machine](/alfred-os/concepts/state-machine/) (`agent:in-flight` → `agent:pr-open` → `agent:done`) with race resolution + stale sweep.
- [Slack severity routing](/alfred-os/concepts/severity-routing/) (`info` / `warn` / `alert`).
- `install.sh` + `INSTALL.md` for fresh-machine bootstrap.
- Setup walkthroughs: Slack, AWS, Claude Code, skills, Linux stance, your-first-agent tutorial.
- [Operator CLI](/alfred-os/reference/cli/) — label-state + pre-push hook.
- CI (pytest + ruff + mypy + shellcheck + scrub-check) on every PR.
- Release automation (tag → GitHub release with auto-extracted changelog).
- Project hygiene: COC, security, support, issue templates, PR template, dependabot.
- pyproject.toml (ruff + mypy), pre-commit config.
- Brew formula skeleton.
- This Astro Starlight docs site.

## In flight (next release)

- **Bot token integration** (`xoxb-…`) — unlocks `slack_set_channel_topic()`, threaded `chat.postMessage` for daily-thread routing of `info`-tier messages, reactions API.
- **Drake-style proactive title-token dedup** — runner-level guard before invoking the planner.
- **`claim_pr` / `release_pr`** — extend the state machine to PR-level work.
- **`render-systemd.sh`** — first-class Linux scheduling.
- **Spend dashboards** — render a weekly recap from per-agent spend files.
- **`alfred-os-init` template** — `npm create vite`-style scaffolding for a new fleet.

## Considered, not yet committed

- MCP server bundling (expose primitives as MCP tools).
- First-class GitHub App (vs operator's PAT).
- Pluggable spend backends.
- Plugin system for skills.
- Web dashboard (rejected once; listed for visibility).

## Out of scope (deliberately)

- Multi-tenant.
- Web UI.
- Long-running orchestration loop.
- LLM routing / model selection at the framework layer.
- Browser automation built in.
- Vector DB for memory.
- Anything Anthropic ships natively (Agent Teams, Memory Tool, MCP server registry).
- Hosted SaaS.
- PyPI publishing.

## Influence

- **Strong**: a working PR for a feature already on the in-flight list.
- **Medium**: a well-scoped feature request issue with a real use case + proposal.
- **Low**: "would be cool if" comments.
- **None**: scope-broadening requests (multi-tenant, hosted, web UI).
