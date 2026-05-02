# Roadmap

What's shipped, what's next, what's deliberately out-of-scope. Updated on every release; living doc.

## Shipped (v0.1.0 + Unreleased)

- Framework substrate: preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table.
- launchd plist template + render.sh + deploy.sh.
- doctor.sh — fleet-wide preflight under `HERMES_DOCTOR=1`.
- hermes-claude — two-account swap helper.
- Issue claim state machine (`agent:in-flight` → `agent:pr-open` → `agent:done`) with race resolution + stale-claim sweep.
- Slack severity routing (`info` / `warn` / `alert`).
- `install.sh` + `INSTALL.md` for fresh-machine bootstrap.
- Setup walkthroughs: Slack, AWS, Claude Code, skills, Linux stance, your-first-agent tutorial.
- Operator-facing label-state CLI + pre-push git hook.
- CI (pytest + ruff + mypy + shellcheck + scrub-check) on every PR.
- Release automation (tag → GitHub release with auto-extracted changelog).
- Project hygiene: COC, security policy, support, issue templates, PR template, dependabot.
- pyproject.toml (ruff + mypy), pre-commit config.
- Brew formula skeleton.
- Astro Starlight docs site.

## In flight (next release)

- **Bot token integration** (`xoxb-…`) — unlocks `slack_set_channel_topic()` for fleet status, `chat.postMessage` with `thread_ts` for daily-thread routing of `info`-tier messages, and reactions API for ack-this-without-replying.
- **Drake-style proactive title-token dedup** — runner-level guard before invoking the planner, complementing the issue-claim state machine. The state machine catches actor-vs-actor races; this catches "two issues, same work."
- **`claim_pr` / `release_pr`** — extend the state machine to PR-level work (review-fix agents that race to land patches on the same PR).
- **`render-systemd.sh`** — first-class Linux scheduling. See [`docs/LINUX.md`](docs/LINUX.md) for the design notes.
- **Spend dashboards** — read every agent's `spend-YYYY-MM-DD.json` and render a weekly recap (turns, cost, success rate) for the Slack `fleet-recap` cron.
- **`pennyworth-init` template** — `npm create vite`-style scaffolding for a new fleet (one command → repo with example codename + agents.conf + GH labels created).

## Considered, not yet committed

- **MCP server bundling** — pennyworth could expose its primitives as an MCP server so other Claude Code consumers can call `claim_issue` / `slack_post(severity)` directly. Feasibility check pending; the framework's tight coupling to `$HERMES_HOME` makes "remote MCP" weird.
- **First-class GitHub App** — instead of the operator's gh PAT, pennyworth could ship a GitHub App definition the consumer installs, with scoped permissions per agent. Larger surface area, harder onboarding; defer until there's a demonstrated need.
- **Pluggable spend backends** — today `SpendState` writes JSON files. A backends interface (filesystem / Redis / sqlite) would help operators with multi-host setups. But pennyworth is single-host by design, so this is mostly speculation.
- **Plugin system for skills** — instead of operator-installed skills (see [`docs/SKILLS.md`](docs/SKILLS.md)), pennyworth could ship a `skills/` directory and an installer. Pushes maintenance onto the framework; current decision is to stay out.
- **Web dashboard** — was rejected once already (see "out of scope" below). Listed here so the rejection is visible.

## Out of scope (deliberately)

These are NOT bugs. They are design decisions.

- **Multi-tenant.** Pennyworth runs one operator on one Mac. Multi-fleet topologies need a real orchestrator; that's not pennyworth.
- **Web UI.** Slack is the human surface. A web UI would duplicate Slack and add browser security surface area.
- **Long-running orchestration loop.** Cron is the orchestrator. A long-running Python process has worse failure isolation.
- **LLM routing / model selection at the framework layer.** Claude Code does this. Pennyworth invokes the CLI.
- **Browser automation built in.** If your fleet needs a browser, install Playwright in the codename's `bin` script. Don't bake it into the framework.
- **Vector DB for memory.** A doc-shaped memory layer (gbrain) is the operator's choice if they want one. Pennyworth doesn't ship one.
- **Anything Anthropic ships natively.** Agent Teams, Memory Tool, MCP server registry — when those mature, pennyworth leans on them rather than re-implementing.
- **Hosted SaaS.** This is a framework, not a service. We won't run agents for you.
- **PyPI publishing.** The framework is a `git clone` install — that's the supported path. PyPI adds versioning ceremony for no real win when consumers vendor or fork anyway.

## How to influence the roadmap

- **Strong influence**: a working PR that ships a feature already on the in-flight list.
- **Medium influence**: a well-scoped feature request issue (use the template) with a real use case + proposal.
- **Low influence**: "would be cool if" comments on existing issues.
- **No influence**: requests that broaden the project's scope (multi-tenant, hosted, web UI).
