# Roadmap

What's shipped, what's next, what's deliberately out of scope. Living doc; updated on every release.

## Shipped (v0.1.0)

The default install ships a working engineering agent fleet. After `bash install.sh && ./bin/alfred-init.py`, an operator has:

**Substrate**
- `lib/agent_runner.py`: preflight, lock, spend, Claude/Codex engine adapters, gh, slack, event-log, commit-trailer.
- Issue claim state machine: `agent:in-flight` → `agent:pr-open` → `agent:done` with race resolution + stale-claim sweep.
- Slack severity routing: `info` / `warn` / `alert`.
- launchd plist template + render.sh + agents.conf format.
- `bin/doctor.sh`, `bin/hermes-claude`, `deploy.sh`.

**Engineering agents** (Batman codenames by default; renameable per role at install time)
- `lucius`: feature dev (picks `agent:implement` issues, opens PRs).
- `drake`: issue planner (files `agent:implement` issues from specs / roadmap).
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
- `alfred-init`: interactive installer wizard (Slack webhook, AWS choice, agent selection, per-role codename, repo selection).
- `alfred` CLI: `agents / enable / disable / enabled-agents / engine status / engine set`.
- Example state-machine CLI (`examples/bin/label_state.py`): `claim / release / dedup-check / status-issue / repo / sweep-claims`.
- Pre-push git hook (`examples/git-hooks/pre-push`): refuses pushes that race in-flight agents.

**Project hygiene**
- CI (pytest 3.11/3.12/3.13 + ruff + mypy + shellcheck + scrub-check) on every PR.
- Release automation (tag → GitHub release with auto-extracted changelog notes + brew sha256).
- Code of conduct, security policy, support, issue templates, PR template, dependabot.
- Astro Starlight docs site at `luminik-io.github.io/alfred-os` (env-overridable for custom domains).
- HEAD-only Brew formula until the first public release tarball has a checksum.

## In flight (v0.2)

- **Bot token operations**: `lib/slack_format.py` already supports threaded Block Kit messages when a bot token is configured. Follow-up work: `slack_set_channel_topic()` for fleet status, reactions API for ack-without-replying, and a documented daily-thread routing policy.
- **Drake-style proactive title-token dedup**: runner-level guard before invoking the planner. Catches "two issues for the same work"; complements the issue-claim state machine which catches "two actors on the same issue."
- **`claim_pr` / `release_pr`**: extend the state machine to PR-level work (review-fix agents that race to land patches on the same PR).
- **`render-systemd.sh`**: first-class Linux scheduling. See [`docs/LINUX.md`](docs/LINUX.md).
- **Spend dashboards**: render a weekly recap (turns, cost, success rate per agent) for `fleet-recap`.
- **`alfred new-codename` scaffold**: single command to add a fresh codename agent (script template + agents.conf entry + label registration).
- **MCP server bundling**: expose `claim_issue` / `release_issue` / `slack_post(severity)` as MCP tools so other Claude Code consumers can call them directly. Pending feasibility check on the `${HERMES_HOME}` coupling.

## Future categories (post-v0.2, out of scope today)

These are agent categories the framework supports in principle but the v0.1 release ships zero of. Each requires its own integration surface and is bigger than a single PR. Tracked here so contributors know where to slot proposals.

- **Sales / SDR agents**: prospect identification, LinkedIn / event-page scraping, outreach drafts. Human-in-the-loop on send.
- **Content agents**: blog / LinkedIn / SEO drafts, site-page generation, content-drift detection. Human-in-the-loop on publish.
- **Personal-assistant agents**: inbox triage, calendar, daily digest. Generates Gmail drafts; never sends.
- **Finance-ops agents**: invoice generation, bank reconciliation, subscription audit. Generates drafts; never moves money.
- **Product-ops / SRE agents**: uptime monitoring, release notes, customer-health signals.

The framework's primitives (`claude_invoke`, `slack_post`, `claim_issue`, `gh_pr_create`) are category-agnostic. The work for each new category is the integration layer (Apollo / Reddit / Gmail / Wise / Sentry SDKs) and the per-codename prompt design.

PRs in any of these categories are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md). Bias: incremental + scoped. One codename per PR, with prompt + tests + docs.

## Considered, not committed

- **First-class GitHub App**: instead of the operator's gh PAT, ship a GitHub App definition with scoped permissions per agent. Bigger onboarding surface; defer until there's demonstrated demand.
- **Pluggable spend backends**: today `SpendState` writes JSON files. Filesystem / Redis / sqlite interface would help multi-host operators. Single-host is the design, so this stays speculative.
- **Plugin system for skills**: instead of operator-installed Claude Code skills, ship a `skills/` directory and an installer. Pushes maintenance onto the framework; current decision is to stay out.
- **Web dashboard**: rejected once already; listed here so the rejection is visible.

## Out of scope (deliberately)

Not bugs. Design decisions.

- **Multi-tenant.** One operator, one Mac, one config.
- **Web UI.** Slack is the human surface.
- **Long-running orchestration loop.** Cron is the orchestrator. Long-running Python has worse failure isolation.
- **Hosted LLM gateway.** Alfred-OS has local CLI engine adapters and simple per-agent engine selection; it does not run a hosted inference service.
- **Browser automation built in.** If your fleet needs a browser, install Playwright in your codename's bin script.
- **Vector DB for memory.** A doc-shaped memory layer is the operator's choice. The framework doesn't ship one.
- **Anything Anthropic ships natively.** Agent Teams, Memory Tool, MCP server registry: when those mature, lean on them rather than re-implement.
- **Hosted SaaS.** This is software you install. We won't run agents for you.
- **PyPI publishing.** Git clone is the supported install path.

## Influence

- **Strong**: working PR for an in-flight feature.
- **Medium**: well-scoped feature request issue with use case + proposal.
- **Low**: "would be cool if" comments.
- **None**: scope-broadening requests (multi-tenant, hosted, web UI).
