---
title: Architecture
description: Why pennyworth has the shape it has — design rationale for cron, worktrees, IAM-per-agent, codename pattern.
---

The full design doc lives at [`ARCHITECTURE.md`](https://github.com/luminik-io/pennyworth/blob/main/ARCHITECTURE.md). This page is the executive summary.

## Five non-negotiables

### 1. Cron, not loops

Every agent firing is a **fresh `launchd` event**, not a tick in a long-running process. Trade-offs accepted:

- ✅ Failure isolation. A crashing firing doesn't poison the next one.
- ✅ OS-level reliability. macOS reboots, system updates, sleep cycles — `launchd` handles all of them.
- ✅ Per-firing observability. Stdout/stderr to per-agent files; the operator's grep-and-tail muscle memory works.
- ❌ No in-process state. Anything an agent needs to remember between firings goes through `$HERMES_HOME/state/<agent>/*.json`.
- ❌ Cold start cost. ~1-2s of Python import + agent_runner setup per firing. Acceptable at the 20-min cadence.

### 2. Per-firing git worktree isolation

Every `claude -p` invocation gets its own worktree:

```
~/.hermes/worktrees/eng-<codename>-<repo>-<issue>-<ts>/
```

The worktree is created via `git worktree add` from a fresh `origin/main` (or whatever the agent designates), and `git worktree remove --force` after. Concurrent firings on different issues do not see each other's edits. A crashed firing can't corrupt the operator's main checkout because they're literally different directories pointing at different branches.

### 3. Per-agent IAM

Every agent that touches AWS gets its **own scoped IAM user**:

```
huntress-cron       — read-only on staging E2E test secrets + the Slack webhook secret
oracle-cron         — read-only on ECS, ALB, CloudWatch logs/metrics. No secretsmanager:*.
gordon-cron         — read-only on ECS describe + the Sentry token secret
alfred-host         — read-only on alfred/* secrets (catch-all for fleet-wide config)
```

The operator's SSO (which has admin everywhere) is **never used by cron**. The agent's prompt invokes `aws` with `env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN AWS_PROFILE=<agent>-cron aws ...` so the operator's ambient SSO can't leak through.

See [AWS setup](/pennyworth/guides/aws/) for templates.

### 4. Spend caps + fleet-wide poison pill

Two layers:

- **Per-agent per-day caps** in `SpendState(AGENT)`. Tracks turns, cost, success rate. Each agent's runner enforces its own ceiling and self-pauses if exceeded.
- **Fleet-wide rate-limit block**. When ANY agent hits Anthropic's `error_rate_limit` or `error_budget`, it calls `set_global_block(hours=1, reason=...)`. Every other agent's `is_globally_blocked()` check at the top of `main()` exits silently for the next hour. Stops the stampede.

### 5. Codename pattern

One agent script per **narrow specialist**. Named after a coherent fictional cast (the reference fleet uses Batman side-characters: Lucius, Drake, Bane, Rasalghul, Robin, Nightwing, Huntress, Gordon).

Two reasons it matters:

1. **Operational legibility.** Codenames appear in PR titles, Slack messages, commit-trailer metadata. A coherent cast makes scanning your `#fleet` channel readable.
2. **Design forcing function.** "What does *Bane* do?" is a sharper question than "what does the test agent do?". Narrow scopes per codename force you to actually decide.

See [codename pattern](/pennyworth/concepts/codename-pattern/) for more.

## What this rules out

- Multi-tenant deployments. Pennyworth is single-operator by design.
- Long-running orchestration loops. Cron is the orchestrator.
- LLM routing / model selection at the framework layer. Claude Code does this.
- Browser automation runtimes. If your fleet needs Playwright, install it in the codename's bin script.
- Vector databases for memory. The reference fleet uses gbrain (a doc-shaped layer); pennyworth doesn't ship one.
- Anything Anthropic ships natively (Agent Teams, Memory Tool). When those mature, lean on them rather than re-implementing.

## What this enables

- **Parallel codename agents on a single Mac**, each with its own IAM, spend cap, and Slack reporting, none stepping on the others.
- **The whole fleet pausable in seconds** via `alfred pause all` (in the reference fleet) or `launchctl bootout` per-agent.
- **Reboot survival**. macOS restart, WiFi flap, gh API outage — the fleet picks up where it left off on the next firing.
- **Cooperative coordination via GitHub** (the [issue claim state machine](/pennyworth/concepts/state-machine/)) — no shared database, no shared filesystem, just labels + structured comments.

## Read order for new contributors

1. [`ARCHITECTURE.md`](https://github.com/luminik-io/pennyworth/blob/main/ARCHITECTURE.md) — full doc
2. [`lib/agent_runner.py`](https://github.com/luminik-io/pennyworth/blob/main/lib/agent_runner.py) — module docstring + public API
3. [`examples/bin/echo_summarise.py`](https://github.com/luminik-io/pennyworth/blob/main/examples/bin/echo_summarise.py) — the smallest "real" agent showing the full pattern
4. [`docs/STATE_MACHINE.md`](https://github.com/luminik-io/pennyworth/blob/main/docs/STATE_MACHINE.md) — the cooperative coordination primitive
5. The reference fleet at [`luminik-io/alfred`](https://github.com/luminik-io/alfred) — a complete production application of pennyworth
