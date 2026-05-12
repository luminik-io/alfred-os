# alfred-os

[![CI](https://github.com/luminik-io/alfred-os/actions/workflows/ci.yml/badge.svg)](https://github.com/luminik-io/alfred-os/actions/workflows/ci.yml)
[![Site](https://github.com/luminik-io/alfred-os/actions/workflows/site.yml/badge.svg)](https://luminik-io.github.io/alfred-os/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![macOS](https://img.shields.io/badge/macOS-13%2B-black?logo=apple)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)

A local engineering-fleet runtime: launchd-managed Claude Code-first agents on a single Mac, with optional Codex routing for review-style work. `launchd` dispatches each firing as a fresh subprocess in its own git worktree. Per-agent IAM. Per-day spend caps. Fleet-wide rate-limit block.

Docs site: https://luminik-io.github.io/alfred-os

## Relationship to Hermes

alfred-os does not require an external Hermes install. The runtime directory is named `HERMES_HOME` for compatibility with the fleet it was extracted from; by default it is just `~/.hermes`, where alfred-os stores deployed scripts, state, logs, and worktrees.

alfred-os is also not a hosted model gateway. It owns the repeatable local fleet pattern: schedules, worktrees, issue claims, PR loops, Slack reporting, and failure guards. Concrete engines such as Claude Code CLI, Codex CLI, and future SDK-backed runners plug in as adapters.

## Design notes

Most agent frameworks (crewAI, MetaGPT, OpenHands, AutoGPT-style loops) assume one long-running Python process, in-memory state, and a human at a REPL. Wrong shape for unattended work:

- Long-running loops have no failure isolation. One bad run trashes the others.
- In-memory state can't survive an OS reboot. macOS restarts every few weeks.
- Chat-first interfaces put the operator on the critical path.

Alfred-OS's shape:

```
launchd plist (every N min)
   │
   ▼
${HERMES_HOME}/bin/<role>.py        one file per agent role
   │
   ▼
agent_runner module                 lock + preflight + spend + claude/codex invoke + gh + slack
   │
   ▼
claude -p '<prompt>' --max-turns N    the LLM work, in a fresh subprocess
                                      N is the caller's value or the
                                      framework default ("effectively
                                      unlimited"); the wall-clock
                                      timeout is the real ceiling
   │
   ▼
slack_post('<result>', severity=…)  report to the fleet's Slack channel
```

Each firing is a fresh subprocess in its own worktree. Spend tracked per agent per day. When any agent hits Anthropic's rate limit, every other agent skips for an hour. The framework code never touches the LLM directly; the runner is plain Python, the model writes the code.

## Quick start

About 30 minutes from a fresh Mac.

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub
claude                            # Claude Code first-run auth
./bin/alfred-init.py              # choose agents, repos, codenames, Slack
```

`alfred-init.py` writes `launchd/agents.conf`, updates `~/.alfredrc`, runs deploy, and runs doctor. For a framework-only install with no agents configured, use `bash deploy.sh && bash bin/doctor.sh`; doctor reports `0 passed, 0 failed`. See [`examples/bin/echo_summarise.py`](examples/bin/echo_summarise.py) for the smallest useful agent (the one [the tutorial](docs/TUTORIAL.md) builds) or [`examples/bin/hello.py`](examples/bin/hello.py) for the absolute minimum.

Full setup including AWS IAM-per-agent, Slack webhook, and your first scheduled firing: [`BOOTSTRAP.md`](BOOTSTRAP.md). From-zero install with troubleshooting: [`INSTALL.md`](INSTALL.md).

## What's in here

| Path | What it is |
|---|---|
| [`lib/agent_runner.py`](lib/agent_runner.py) | Shared library. Preflight, lock, spend, claude_invoke, codex_invoke, gh, slack, event-log, commit-trailer, handoff-table, issue claim state machine, runner gate helpers, dedup helpers (`find_open_authored_pr_for_issue`, `reuse_or_make_worktree`), slack severity routing. |
| [`lib/slack_format.py`](lib/slack_format.py) | Block Kit + bot-token Slack helpers: per-firing `firing_thread_root` / `firing_thread_reply` / `firing_thread_close`. Severity colour stripes. |
| [`lib/batman.py`](lib/batman.py) | Bundle primitives for the multi-repo coordinator: `Bundle`, `claim_bundle` (all-or-nothing), `release_bundle`, `parse_plan_from_bundle`. |
| [`bin/alfred`](bin/alfred) | Operator CLI: `alfred agents`, `alfred enable <codename>`, `alfred disable <codename>`, `alfred enabled-agents`, `alfred engine status/set`. |
| [`bin/batman.py`](bin/batman.py) | Skeleton multi-repo coordinator. Picks `agent:large-feature` / `agent:bundle:<slug>` issues and posts a plan to Slack. |
| [`bin/fleet-doctor.py`](bin/fleet-doctor.py) | Daily fleet-health snapshot. Read-only checks (paused repos, global block, stale worktrees, runner gate list) → severity-stripe Slack thread. |
| [`bin/`](bin/) | Operator helpers: `doctor.sh` (host validator), `hermes-claude` (two-account swap). |
| [`launchd/`](launchd/) | `_template.plist` + `agents.conf.example` + `render.sh` (TSV → plists). |
| [`deploy.sh`](deploy.sh) | Sync `lib/` + `bin/` into `${HERMES_HOME}`. If `launchd/agents.conf` exists, render plists and bootstrap `launchd`; otherwise do a framework-only deploy. |
| [`install.sh`](install.sh) | Fresh-machine bootstrap: brew + npm + dirs + shell rc. Idempotent. |
| [`examples/bin/hello.py`](examples/bin/hello.py) | Smallest possible codename agent: preflight + Slack post. |
| [`examples/bin/echo_summarise.py`](examples/bin/echo_summarise.py) | Full lifecycle reference: pick / claim / claude / act / release / report. |
| [`examples/bin/label_state.py`](examples/bin/label_state.py) | Operator CLI for the issue claim state machine. |
| [`examples/git-hooks/pre-push`](examples/git-hooks/pre-push) | Refuses push if a referenced issue is in-flight. Symmetric guard. |
| [`Formula/alfred-os.rb`](Formula/alfred-os.rb) | Draft Homebrew formula. Published after the first public tag has a real tarball checksum. |
| [`site/`](site/) | Astro Starlight docs site, with GitHub Pages publishing gated by the release repo variable. |

## Documentation

- [Install](INSTALL.md): fresh-Mac walkthrough.
- [Bootstrap](BOOTSTRAP.md): operations guide (AWS IAM, Slack, troubleshooting).
- [Tutorial: your first agent](docs/TUTORIAL.md): Echo, end-to-end.
- [Architecture](ARCHITECTURE.md): design rationale.
- [State machine](docs/STATE_MACHINE.md): `agent:in-flight` → `agent:pr-open` → `agent:done` lifecycle.
- [Claude Code](docs/CLAUDE_CODE.md): install, Pro vs Max, hermes-claude.
- [Slack setup](docs/SLACK_SETUP.md): webhook + AWS storage + (optional) bot token.
- [AWS setup](docs/AWS_SETUP.md): IAM-per-agent, scoped policies.
- [Skills](docs/SKILLS.md): recommended Claude Code skills.
- [Linux](docs/LINUX.md): current macOS-only stance + interim cron / systemd patterns.
- [Contributing](CONTRIBUTING.md) | [Roadmap](ROADMAP.md) | [Changelog](CHANGELOG.md)
- [Security](SECURITY.md): private-disclosure process.
- [Release checklist](docs/RELEASE_CHECKLIST.md): pre-tag gates, scrub scan, GitHub Release flow.

Rendered version: https://luminik-io.github.io/alfred-os/.

## Codename pattern

The framework expects one agent script per narrow specialist, named after a coherent fictional cast, coordinating via labels and gh state rather than in-process calls. The shipped examples use Batman side-characters: **Batman** (multi-repo coordinator), **Lucius** (feature dev), **Drake** (planner), **Bane** (test coverage), **Ra's al Ghul** (PR review), **Robin** (bug triage), **Nightwing** (review-fix), **Huntress** (post-deploy smoke), **Gordon** (deploy health). Pick whatever cast fits.

The cast matters for two reasons. Codenames appear in PR titles, Slack messages, and commit-trailer metadata; a coherent cast makes the fleet's channel scannable. And narrow scopes per codename are a forcing function for design quality. "What does Bane do?" is a sharper question than "what does the test agent do?".

See [Architecture → Codename pattern](https://luminik-io.github.io/alfred-os/concepts/codename-pattern/) for more.

## What alfred-os does NOT do

- ❌ Multi-tenant. Single operator, one Mac, one config.
- ❌ A web UI. Slack is the human surface.
- ❌ Long-running orchestration loops. The OS scheduler is the orchestrator.
- ❌ Hosted model gateways. alfred-os shells out to local CLIs (`claude`, optional `codex`, optional Ollama); it does not run a multi-tenant inference gateway.
- ❌ Browser automation runtimes. If your fleet needs a browser, install Playwright in your codename agent's bin script.
- ❌ Vector databases for memory. Some fleets use a doc-shaped memory layer. Alfred-OS doesn't ship one; that's a per-fleet decision.
- ❌ Anything Anthropic ships natively (Agent Teams, Memory Tool). When those mature, lean on them rather than re-implementing in alfred-os.

## Status

**v0.2.0**. Public release candidate for a complete local engineering-agent fleet. APIs in `agent_runner` are stable for the operator's own use; expect rough edges if you fork. There is no roadmap to make alfred-os multi-tenant.

Maintained on weekends. Issues triaged on a best-effort basis. PRs that match the design constraints (see [`CONTRIBUTING.md`](CONTRIBUTING.md)) get reviewed; PRs that broaden scope get politely declined.

## License

MIT. See [`LICENSE`](LICENSE).

## Why "alfred-os"

Alfred-OS is named after Bruce Wayne's butler, the one who keeps the cave running while the mission is in flight. The default codenames are bat-themed, and the framework that lets the cave function is `alfred-os`.
