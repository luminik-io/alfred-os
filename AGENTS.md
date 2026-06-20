# AGENTS.md

Guidance for AI coding agents working in this repository. Humans: read
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md)
first; this file is the short version those agents need.

## What this repo is

Alfred is the open-source runtime for a fleet of autonomous engineering agents
on Claude Code and Codex. The OS scheduler (launchd on macOS, systemd on Linux)
fires each agent; `lib/agent_runner/` wraps every firing in a lock,
preflight, spend cap, and an isolated git worktree. Agents are one Python file
per role under `bin/`, named after a coherent fictional cast (the codename
pattern). `examples/` holds the reference agents the tutorial builds.

Users inspect and steer the fleet through the Alfred CLI (`bin/alfred`),
the optional `alfred serve` JSON API, the optional Tauri desktop client under
`clients/desktop`, and Slack. The desktop client has a Claude + Codex
subscription-headroom rail (backed by the live `GET /api/usage` endpoint, read
from local CLI state with no billing API; the same data is available from
`alfred usage`) and a cinematic agent roster. Any issue carrying the
approval gate label (`agent:plan-pending-approval`) is held from
autonomous pickup until the configured approver clears it; firings
emit step-level run events so the run
timeline shows real progress.

## Design boundaries (do not cross without a discussion)

- **Single-person install.** One person, one host, one config. Not multi-tenant,
  not a hosted SaaS.
- **The OS schedules; Alfred runs.** No long-running orchestration loop.
- **Local CLIs, not a model gateway.** Alfred shells out to `claude` / `codex`.
- **Lean on the platform.** Adopt Anthropic-native capabilities rather than
  re-implement them.

Scope-broadening changes get declined. If a change touches these boundaries,
open a discussion before writing code. See [`ROADMAP.md`](ROADMAP.md).

## Conventions

- **No em-dashes** in prose or comments. Use periods, commas, colons, or
  parentheses.
- **No `Co-Authored-By` or AI-attribution trailers** on commits. Conventional
  commit messages (`feat:`, `fix:`, `docs:`, `chore:`).
- One codename per PR, with prompt + tests + docs. Keep PRs scoped.
- This is a public repo: no host paths, no cloud account IDs, no secrets, no
  personal handles. `bin/scrub-check.sh` enforces this.

## Checks before opening a PR

```sh
uv run --with 'ruff>=0.6' ruff check .
uv run --with 'ruff>=0.6' ruff format --check .
uv run --with 'mypy>=1.10' mypy lib/
uv run --with pytest pytest tests/ -v
bash bin/scrub-check.sh
```

Shell scripts must pass `shellcheck -S warning`. The docs site
(`site/`) must `npm run build` cleanly if you touch it.
