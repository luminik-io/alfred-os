# pennyworth

[![CI](https://github.com/luminik-io/pennyworth/actions/workflows/ci.yml/badge.svg)](https://github.com/luminik-io/pennyworth/actions/workflows/ci.yml)
[![Site](https://github.com/luminik-io/pennyworth/actions/workflows/site.yml/badge.svg)](https://luminik-io.github.io/pennyworth/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Made with Claude Code](https://img.shields.io/badge/Made%20with-Claude%20Code-D97757)](https://docs.claude.com/en/docs/claude-code)
![macOS](https://img.shields.io/badge/macOS-13%2B-black?logo=apple)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)

> **One Mac. One operator. A fleet of narrow-specialist Claude Code agents shipping code while you sleep.**

A small framework for running cron-driven Claude Code agents on a single Mac, dispatched by `launchd`, isolated by per-firing git worktrees, scoped by per-agent IAM, and bounded by per-day spend caps with a fleet-wide rate-limit poison pill.

🌐 **Docs site**: https://luminik-io.github.io/pennyworth
📦 **Reference fleet** (full production application): [`luminik-io/alfred`](https://github.com/luminik-io/alfred)
🛠 **Built for**: solo founders, indie hackers, small-team CTOs

---

## Why this exists

Most agentic frameworks (crewAI, MetaGPT, OpenHands, AutoGPT-style loops) assume one long-running Python process, in-memory state, and a human at a REPL. That's the wrong shape for an *unattended* engineering team:

- Long-running loops have no natural failure isolation. One bad run trashes the others.
- In-memory state can't survive an OS reboot. macOS restarts every few weeks.
- Chat-first interfaces force the operator to be the bottleneck. The whole point is to *not* be one.

Pennyworth picks a different shape:

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
slack_post('<result>', severity=…)  report to your fleet's Slack channel
```

Each firing is a fresh subprocess in its own git worktree. Spend is tracked per agent per day. When any agent hits Anthropic's rate limit, every other agent silently skips for an hour. None of the framework code touches the LLM directly — the runner is dumb Python, the model writes the code.

## Quick start (30 minutes from a fresh Mac)

```sh
git clone https://github.com/luminik-io/pennyworth.git ~/code/pennyworth
cd ~/code/pennyworth
bash install.sh
exec $SHELL                       # pick up ~/.pennyworthrc
gh auth login                     # GitHub
claude                            # Claude Code first-run auth
bash deploy.sh && bash bin/doctor.sh
```

`doctor.sh` will report `0 passed, 0 failed` against an empty fleet — the framework is installed, you just haven't pointed any codename agents at it yet. See [`examples/bin/echo_summarise.py`](examples/bin/echo_summarise.py) for the smallest *useful* codename agent (the one [the tutorial](docs/TUTORIAL.md) builds), or [`examples/bin/hello.py`](examples/bin/hello.py) for the absolute minimum.

Full setup walkthrough including AWS IAM-per-agent, Slack webhook, and your first cron firing: [`BOOTSTRAP.md`](BOOTSTRAP.md). For the from-zero install with troubleshooting: [`INSTALL.md`](INSTALL.md).

## What's in here

| Path | What it is |
|---|---|
| [`lib/agent_runner.py`](lib/agent_runner.py) | Shared library. ~1700 LoC of preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table, **issue claim state machine**, **slack severity routing**. |
| [`bin/`](bin/) | Operator helpers — `doctor.sh` (host validator), `hermes-claude` (two-account swap). |
| [`launchd/`](launchd/) | `_template.plist` + `agents.conf.example` + `render.sh` (TSV → plists). |
| [`deploy.sh`](deploy.sh) | Sync `lib/` + `bin/` into `${HERMES_HOME}`, render plists, bootstrap `launchd`. |
| [`install.sh`](install.sh) | Fresh-machine bootstrap — brew + npm + dirs + shell rc. Idempotent. |
| [`examples/bin/hello.py`](examples/bin/hello.py) | Smallest possible codename agent — just preflight + Slack post. |
| [`examples/bin/echo_summarise.py`](examples/bin/echo_summarise.py) | Full lifecycle reference — pick / claim / claude / act / release / report. |
| [`examples/bin/label_state.py`](examples/bin/label_state.py) | Operator CLI for the issue claim state machine. |
| [`examples/git-hooks/pre-push`](examples/git-hooks/pre-push) | Refuses push if a referenced issue is in-flight. Symmetric guard. |
| [`Formula/pennyworth.rb`](Formula/pennyworth.rb) | Homebrew formula — `brew install luminik-io/tap/pennyworth` (when the tap is published). |
| [`site/`](site/) | Astro Starlight docs site, deployed to GitHub Pages. |

## Documentation

- 📘 [Install](INSTALL.md) — fresh-Mac walkthrough, 30 min.
- 📘 [Bootstrap](BOOTSTRAP.md) — deeper operations guide (AWS IAM, hermes-agent, troubleshooting).
- 📘 [Tutorial: your first agent](docs/TUTORIAL.md) — Echo, end-to-end.
- 📘 [Architecture](ARCHITECTURE.md) — design rationale.
- 📘 [State machine](docs/STATE_MACHINE.md) — `agent:in-flight` → `agent:pr-open` → `agent:done` lifecycle.
- 📘 [Claude Code](docs/CLAUDE_CODE.md) — install, Pro vs Max, hermes-claude.
- 📘 [Slack setup](docs/SLACK_SETUP.md) — webhook + AWS storage + (optional) bot token.
- 📘 [AWS setup](docs/AWS_SETUP.md) — IAM-per-agent, scoped policies.
- 📘 [Skills](docs/SKILLS.md) — recommended Claude Code skills.
- 📘 [Linux](docs/LINUX.md) — current macOS-only stance + interim cron / systemd patterns.
- 📘 [Contributing](CONTRIBUTING.md) | [Roadmap](ROADMAP.md) | [Changelog](CHANGELOG.md)
- 🛡 [Security](SECURITY.md) — private-disclosure process.

Or browse the rendered version at https://luminik-io.github.io/pennyworth/.

## Codename pattern

The framework expects you to write one agent script per **narrow specialist**, name them after a coherent fictional cast, and have them coordinate via labels and gh state rather than in-process calls. The reference fleet ([`luminik-io/alfred`](https://github.com/luminik-io/alfred)) uses Batman side-characters: **Lucius** (feature dev), **Drake** (planner), **Bane** (test coverage), **Ra's al Ghul** (PR review), **Robin** (bug triage), **Nightwing** (review-fix), **Huntress** (post-deploy smoke), **Gordon** (deploy health), **Bat-Signal** (Slack notifier). Pick whatever cast fits your project.

The cast matters for two reasons. First, the codenames appear in PR titles, Slack messages, and commit-trailer metadata — a coherent cast makes scanning your fleet's channel legible. Second, narrow scopes per codename are a forcing function for design quality. "What does *Bane* do?" is a sharper question than "what does the test agent do?".

See [Architecture → Codename pattern](https://luminik-io.github.io/pennyworth/concepts/codename-pattern/) for more.

## What pennyworth deliberately does NOT do

- ❌ Multi-tenant. Single operator, one Mac, one config.
- ❌ A web UI. Slack is the human surface.
- ❌ Long-running orchestration loops. Cron is the orchestrator.
- ❌ LLM routing / model selection at the framework layer (Claude Code already handles model picking; pennyworth invokes the CLI).
- ❌ Browser automation runtimes. If your fleet needs a browser, install Playwright in your codename agent's bin script.
- ❌ Vector databases for memory. The reference fleet uses a doc-shaped memory layer (gbrain). Pennyworth doesn't ship one — that's a per-fleet decision.
- ❌ Anything Anthropic ships natively (Agent Teams, Memory Tool). When those mature, lean on them rather than re-implementing in pennyworth.

## Status

**v0.1.0** — initial public extraction from a fleet that has been running unattended for several months. APIs in `agent_runner` are stable for the operator's own use; expect rough edges if you fork. There is no roadmap to make pennyworth multi-tenant.

Maintained on weekends. Issues triaged on a best-effort basis. PRs that match the design constraints (see [`CONTRIBUTING.md`](CONTRIBUTING.md)) get reviewed; PRs that broaden scope get politely declined.

## License

MIT. See [`LICENSE`](LICENSE).

## Why "pennyworth"

Alfred Pennyworth is Bruce Wayne's butler — the one who keeps the cave running while the mission is in flight. The reference fleet is named `alfred`, the codenames are bat-themed, and the framework that lets the cave function is *pennyworth*.
