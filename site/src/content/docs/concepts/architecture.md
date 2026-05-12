---
title: Architecture
description: Design rationale for launchd, worktrees, IAM-per-agent, codename pattern.
---

Full design doc at [`ARCHITECTURE.md`](https://github.com/luminik-io/alfred-os/blob/main/ARCHITECTURE.md). This page is the executive summary.

## Five non-negotiables

### 1. launchd, not loops

Every agent firing is a fresh `launchd` event, not a tick in a long-running process. Trade-offs:

- ✅ Failure isolation. A crashing firing doesn't poison the next one.
- ✅ OS-level reliability. macOS reboots, system updates, sleep cycles. `launchd` handles all of them.
- ✅ Per-firing observability. Stdout/stderr to per-agent files; the operator's grep-and-tail muscle memory works.
- ❌ No in-process state. Anything an agent needs to remember between firings goes through `$HERMES_HOME/state/<agent>/*.json`.
- ❌ Cold start cost. ~1-2s of Python import + agent_runner setup per firing. Acceptable at the 20-min cadence.

`HERMES_HOME` is the runtime-root name, not a requirement that the Hermes agent
daemon be installed. The core loop is `launchd -> bin/role.py ->
lib/agent_runner.py -> claude/codex/gh/slack`. Hermes skills, MCP, gbrain,
canon, and dashboarding are optional integrations for fleets that want them.

### 2. Per-firing git worktree isolation

Every `claude -p` invocation gets its own worktree:

```
~/.hermes/worktrees/eng-<codename>-<repo>-<issue>-<ts>/
```

The worktree is created via `git worktree add` from a fresh `origin/main` (or whatever the agent designates), and `git worktree remove --force` after. Concurrent firings on different issues do not see each other's edits. A crashed firing can't corrupt the operator's main checkout because they're literally different directories pointing at different branches.

### 3. Per-agent IAM

Every agent that touches AWS gets its own scoped IAM user:

```
<your-codename>-cron read-only on the agent's specific secrets (test creds, webhooks, etc.)
gordon-cron         read-only on ECS, ALB, CloudWatch logs/metrics, plus the Sentry token
alfred-host         read-only on alfred/* secrets (catch-all for fleet-wide config)
```

The operator's SSO (which has admin everywhere) is never used by scheduled agents. AWS-aware runners read role-specific variables such as `ALFRED_GORDON_AWS_PROFILE`, strip inherited `AWS_*` credentials, and set `AWS_PROFILE` only around the AWS subprocess they own.

See [AWS setup](/guides/aws/) for templates.

### 4. Spend caps + fleet-wide poison pill

Two layers:

- **Per-agent per-day caps** in `SpendState(AGENT)`. Tracks turns, cost, success rate. Each agent's runner enforces its own ceiling and self-pauses if exceeded.
- **Fleet-wide rate-limit block.** When any agent hits Anthropic's `error_rate_limit` or `error_budget`, it calls `set_global_block(hours=1, reason=...)`. Every other agent's `is_globally_blocked()` check at the top of `main()` exits silently for the next hour. Stops the stampede.

### 5. Codename pattern

One agent script per narrow specialist. Named after a coherent fictional cast. The shipped examples use Batman side-characters: Lucius, Drake, Bane, Rasalghul, Robin, Nightwing, Huntress, Gordon.

Two reasons it matters:

1. **Operational legibility.** Codenames appear in PR titles, Slack messages, commit-trailer metadata. A coherent cast makes scanning your `#fleet` channel readable.
2. **Design forcing function.** "What does Bane do?" is a sharper question than "what does the test agent do?". Narrow scopes per codename force you to decide.

See [codename pattern](/concepts/codename-pattern/) for more.

## What this rules out

- Multi-tenant deployments. Alfred-OS is single-operator by design.
- Long-running orchestration loops. The OS scheduler is the orchestrator.
- Hosted LLM gateway. Alfred-OS has local CLI engine adapters and simple per-agent engine selection; it does not run inference for you.
- Browser automation runtimes. If your fleet needs Playwright, install it in the codename's bin script.
- Vector databases for memory. Some fleets use a doc-shaped memory layer; alfred-os doesn't ship one.
- Anything Anthropic ships natively (Agent Teams, Memory Tool). When those mature, lean on them rather than re-implementing.

## What this enables

- **Parallel codename agents on a single Mac**, each with its own IAM, spend cap, and Slack reporting, none stepping on the others.
- **The whole fleet pausable in seconds** via `launchctl bootout` per-agent, or by keeping your own wrapper around the same launchd calls.
- **Reboot survival**. macOS restart, WiFi flap, gh API outage: the fleet picks up where it left off on the next firing.
- **Cooperative coordination via GitHub** (the [issue claim state machine](/concepts/state-machine/)): no shared database, no shared filesystem, just labels + structured comments.

## Read order for new contributors

1. [`ARCHITECTURE.md`](https://github.com/luminik-io/alfred-os/blob/main/ARCHITECTURE.md): full doc
2. [`lib/agent_runner.py`](https://github.com/luminik-io/alfred-os/blob/main/lib/agent_runner.py): module docstring + public API
3. [`examples/bin/echo_summarise.py`](https://github.com/luminik-io/alfred-os/blob/main/examples/bin/echo_summarise.py): the smallest "real" agent showing the full pattern
4. [`docs/STATE_MACHINE.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/STATE_MACHINE.md): the cooperative coordination primitive
5. [`examples/bin/`](https://github.com/luminik-io/alfred-os/tree/main/examples/bin): small runnable agents you can copy into a fleet
