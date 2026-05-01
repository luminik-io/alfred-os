# pennyworth

A small framework for running a fleet of narrow-specialist Claude Code agents on a single Mac, dispatched by cron, isolated by git worktree, scoped by per-agent IAM, and bounded by per-day spend caps with a fleet-wide rate-limit poison pill.

> Built for one operator with one Mac Mini in a closet. Not a multi-tenant orchestrator, not a hosted SaaS, not a chat-first agent framework. Optimised for the case where you want code to ship while you sleep.

## Why this exists

Most agentic frameworks (crewAI, MetaGPT, OpenHands, AutoGPT-style loops) assume one long-running Python process, in-memory state, and a human at a REPL. That's the wrong shape for an *unattended* engineering team:

- Long-running loops have no natural failure isolation. One bad run trashes the others.
- In-memory state can't survive an OS reboot. macOS restarts every few weeks.
- Chat-first interfaces force the operator to be the bottleneck. The whole point is to *not* be one.

pennyworth picks a different shape:

```
launchd plist (every N min)
   │
   ▼
${HERMES_HOME}/bin/<codename>.py    ~150-300 lines per agent
   │
   ▼
agent_runner module                 lock + preflight + spend + claude_invoke + gh + slack
   │
   ▼
claude -p '<prompt>' --max-turns N  the actual LLM work, in a fresh subprocess
   │
   ▼
slack_post('<result>')              report to the consumer's Slack channel
```

Each firing is a fresh subprocess in its own git worktree. Spend is tracked per agent per day. When any agent hits Anthropic's rate limit, every other agent silently skips for an hour. None of the framework code touches the LLM directly — the runner is dumb Python, the model writes the code.

## What's in here

- `lib/agent_runner.py` — the shared library. ~1000 LoC of preflight / lock / spend / claude_invoke / gh / slack / event-log / commit-trailer / handoff-table primitives.
- `bin/hermes-claude` — switch which Claude Code account the launchd-spawned agents use (primary vs secondary, useful when one hits a weekly cap).
- `bin/doctor.sh` — exercises every agent's preflight under `HERMES_DOCTOR=1`. Reports pass/fail across the whole fleet without burning a Claude turn or making side effects.
- `launchd/_template.plist` + `launchd/render.sh` — render concrete plists from a template + per-agent config (TSV format documented in `launchd/agents.conf.example`).
- `deploy.sh` — copy `lib/`, `bin/` into `${HERMES_HOME}/{lib,bin}/`. Symlinks `hermes-claude` and `pennyworth-doctor` onto `~/.local/bin`.
- `examples/` — reference codename agents + an operator-facing label-state CLI + a pre-push git hook you can drop into your fleet.
- `docs/` + top-level docs:
  - [`ARCHITECTURE.md`](ARCHITECTURE.md) — design rationale: codename pattern, plan-review gate, worktree-per-firing, IAM-per-agent.
  - [`BOOTSTRAP.md`](BOOTSTRAP.md) — fresh-fork setup walkthrough.
  - [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to propose a new codename, change a primitive, run the tests.
  - [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md) — issue claim lifecycle (`agent:in-flight` → `agent:pr-open` → `agent:done`) + race resolution + stale sweep.

## Quick start

```sh
git clone https://github.com/luminik-io/pennyworth.git ~/code/pennyworth
cd ~/code/pennyworth
bash deploy.sh
bash bin/doctor.sh
```

`doctor.sh` will report `0 passed, 0 failed` against an empty fleet — the framework is installed, you just haven't pointed any codenames at it yet. See `examples/bin/hello.py` for the smallest possible codename agent and copy it.

Full setup including AWS IAM-per-agent, Slack webhook, hermes-agent, and your first cron firing: [`BOOTSTRAP.md`](BOOTSTRAP.md).

## Codename pattern

The framework expects you to write one agent script per **narrow specialist**, name them after a coherent fictional cast, and have them coordinate via labels and gh state rather than in-process calls. The reference fleet (`luminik-io/alfred`) uses Batman side-characters: Lucius (feature dev), Drake (planner), Bane (test coverage), Rasalghul (code review), Robin (bug triage), Nightwing (review-fix), Huntress (post-deploy smoke). Pick whatever cast fits your brand.

The cast matters for two reasons. First, the codenames appear in PR titles, Slack messages, and commit-trailer metadata — a coherent cast makes scanning `#your-fleet-channel` legible. Second, narrow scopes per codename are a forcing function for design quality. "What does *Bane* do?" is a sharper question than "what does the test agent do?".

See [`ARCHITECTURE.md`](ARCHITECTURE.md#codename-pattern) for more.

## What pennyworth deliberately does NOT do

- Multi-tenant. Single operator, one Mac, one config.
- A web UI. Slack is the human surface.
- Long-running orchestration loops. Cron is the orchestrator.
- LLM routing / model selection at the framework layer (Claude Code already handles model picking; pennyworth invokes the CLI).
- Browser automation runtimes. If your fleet needs a browser, install Playwright in your codename agent's bin script. Don't bake it in.
- Vector databases for memory. The reference fleet uses a doc-shaped memory layer (gbrain). Pennyworth doesn't ship one — that's a per-fleet decision.
- Anything Anthropic ships natively (Agent Teams, Memory Tool). When those mature, lean on them rather than re-implementing in pennyworth.

## Status

Working title. The reference fleet ([`luminik-io/alfred`](https://github.com/luminik-io/alfred)) has been running on this framework for several months. APIs in `agent_runner` are stable for the operator's own use; expect rough edges if you fork. There is no roadmap to make pennyworth multi-tenant.

Maintained by [@prasadus92](https://github.com/prasadus92) on weekends. Issues triaged on a best-effort basis. PRs that match the design constraints (see [`CONTRIBUTING.md`](CONTRIBUTING.md)) get reviewed; PRs that broaden scope get politely declined.

## License

MIT. See [`LICENSE`](LICENSE).

## Why "pennyworth"

Alfred Pennyworth is Bruce Wayne's butler — the one who keeps the cave running while the mission is in flight. The reference fleet is named `alfred`, the codenames are bat-themed, and the framework that lets the cave function is *pennyworth*. The name is also distant enough from "Claude" that there's no trademark risk on the public release. (`claude-fleet` was the working title; renamed after a brief look at Anthropic's enforcement history.)
