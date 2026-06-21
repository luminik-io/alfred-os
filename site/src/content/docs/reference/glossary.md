---
title: Glossary
description: One-sentence definitions for every codename, label, sentinel, and runtime concept a first-reader meets in Alfred docs.
---

One-sentence definitions for the terms a first-reader meets in Alfred docs.
Entries are alphabetical, with cross-links to the page that covers each term
in depth.

- **`agent:authored` label**: Marks a PR opened by an Alfred agent, used by
  Ra's al Ghul, Nightwing, and automerge to know which PRs are theirs to act on.
  See also: [Issue claim state machine](/concepts/state-machine/).
- **`agent:bundle:<slug>` label**: Groups GitHub issues that belong to one
  cross-repo feature; Batman resolves the bundle from this label.
  See also: [Worked example](/guides/multi-repo-worked-example/).
- **`agent-cleanup`**: Housekeeping agent that sweeps stale worktrees,
  expired spend files, stuck locks, and stale `agent:in-flight` claims.
  See also: [The agent fleet](/concepts/fleet/).
- **`agent:done` label**: Terminal state for an issue whose PR has merged;
  set by `release_issue(transition_to="agent:done")`.
  See also: [Issue claim state machine](/concepts/state-machine/).
- **`agent:implement` label**: Marks an issue ready for Lucius (or another
  feature-dev agent) to claim and implement.
  See also: [Issue claim state machine](/concepts/state-machine/).
- **`agent:in-flight` label**: Marks an issue currently claimed by an agent;
  another agent will not pick it up until the claim is released.
  See also: [Issue claim state machine](/concepts/state-machine/).
- **`agent:large-feature` label**: Marks an issue Batman should plan as a
  multi-repo rollout rather than a single-repo implementation.
  See also: [Worked example](/guides/multi-repo-worked-example/).
- **`agent:pr-open` label**: Set on the issue when the implementing agent
  opens its PR; cleared when the PR merges or closes.
  See also: [Issue claim state machine](/concepts/state-machine/).
- **AGENT_RUNNER**: The shared Python package at `lib/agent_runner/` that
  every agent script imports for preflight, locking, spend, gh, and Slack.
  See also: [agent_runner API](/reference/agent-runner/).
- **AgentResult**: The dataclass an engine returns to the runner, containing
  success flag, subtype, turn count, cost, session id, and result text.
  See also: [How it works](/concepts/how-it-works/).
- **ALFRED_HOME**: Operator-overridable directory holding runtime state,
  prompts, worktrees, and logs; defaults to `~/.alfred`.
  See also: [Install](/getting-started/install/).
- **Bane**: Test-coverage agent that picks the lowest-coverage actively
  changed file and opens a tests-only PR for it.
  See also: [The agent fleet](/concepts/fleet/).
- **Bat-signal**: The Slack alert raised when an agent prints `[BLOCKED]`
  or when a fleet-wide spend or rate-limit cap trips.
  See also: [Output samples](/reference/output-samples/).
- **Batman**: Cross-repo architect that turns `agent:large-feature` issues
  into rollout plans and, on the parent-issue path, approved child
  `agent:implement` issues for the normal fleet queue. See also:
  [Worked example](/guides/multi-repo-worked-example/).
- **`claude -p`**: Claude Code's non-interactive subprocess mode, the surface
  Alfred uses to invoke Claude with a prompt and capture an AgentResult.
  See also: [Claude Code and Codex](/guides/claude-code/).
- **code-map**: JSON snapshot of every watched repo's source files, symbols,
  imports, API calls, server routes, and contract drift, written to
  `$ALFRED_HOME/state/code-map.json` by `code-map-refresh`.
  See also: [Alfred on a monorepo](/guides/monorepo/).
- **Codex**: OpenAI's local coding agent, supported as an engine alongside
  Claude Code and selectable per agent via `ALFRED_<AGENT>_ENGINE`.
  See also: [Claude Code and Codex](/guides/claude-code/).
- **doctor.sh**: Script at `bin/doctor.sh` that runs every enabled agent in
  doctor mode (no LLM spend) and reports preflight status.
  See also: [Output samples](/reference/output-samples/).
- **Drake**: Planner agent that reads specs, roadmap, and code-reality and
  files the next well-scoped `agent:implement` issue for the fleet to work.
  See also: [Specs-driven development](/guides/specs-driven-development/).
- **dry-run**: Mode toggled by `--dry-run` or `ALFRED_DRY_RUN=1` that
  narrates a full firing lifecycle without LLM calls or side effects.
  See also: [Dry-run mode](/getting-started/dry-run/).
- **engine**: The coding backend an agent invokes; Claude Code, Codex, or a
  hybrid that tries Claude first and falls back to Codex on rate limits.
  See also: [Claude Code and Codex](/guides/claude-code/).
- **engine routing**: Per-agent assignment of which engine to use, set via
  `ALFRED_<AGENT>_ENGINE` (`claude`, `codex`, or `hybrid`).
  See also: [Claude Code and Codex](/guides/claude-code/).
- **fast-cleanup**: `agent-cleanup`'s sub-pass that runs after every Lucius
  firing to delete just-closed worktrees without waiting for the nightly sweep.
  See also: [The agent fleet](/concepts/fleet/).
- **firing**: One run of one agent triggered by the host scheduler; bounded
  by lock, preflight, spend caps, and a hard timeout.
  See also: [How it works](/concepts/how-it-works/).
- **GH_ORG**: The GitHub org or user that owns the repos Alfred operates
  against; agents refuse to act on repos outside it.
  See also: [Install](/getting-started/install/).
- **hybrid fallback**: Engine routing mode where the runner tries Claude
  first and falls back to Codex when Claude hits a rate limit or budget cap.
  See also: [Claude Code and Codex](/guides/claude-code/).
- **IAM-per-agent**: AWS pattern where each agent gets its own IAM identity
  and Secrets Manager scope, so a compromised agent can only reach its own keys.
  See also: [AWS](/guides/aws/).
- **launchd** macOS host scheduler that owns the firing cadence on Mac;
  agents ship as `.plist` files in `~/Library/LaunchAgents/`.
  See also: [Architecture](/concepts/architecture/).
- **Lucius**: Feature-dev agent that claims an `agent:implement` issue, opens
  a worktree, invokes the engine, and pushes a PR labelled `agent:authored`.
  See also: [How it works](/concepts/how-it-works/).
- **Nightwing**: Review-fix agent that lands P0/P1 reviewer comments on
  open `agent:authored` PRs without re-litigating design.
  See also: [The agent fleet](/concepts/fleet/).
- **plist** macOS launchd unit file; one per agent, generated by the
  renderer from `launchd/agents.conf` and `launchd/template.plist`.
  See also: [launchd plist template](/reference/launchd/).
- **preflight**: Cheap pre-firing check that the required CLIs, gh auth,
  and workspace checkouts exist before any LLM turn is spent.
  See also: [How it works](/concepts/how-it-works/).
- **Ras al Ghul**: Code-review agent that posts a multi-axis review
  (correctness, security, performance, maintainability) on every fresh
  `agent:authored` PR. See also: [The agent fleet](/concepts/fleet/).
- **role runner**: A Python script under `bin/` that implements one agent
  role; codenames map to role runners via `AGENT_CODENAME`.
  See also: [The agent fleet](/concepts/fleet/).
- **sentinel string**: Bracketed marker like `[OK]`, `[BLOCKED]`, or
  `[SILENT]` that an agent prints to stdout to signal its exit path.
  See also: [Output samples](/reference/output-samples/).
- **Slack post**: One outbound message Alfred sends to a configured Slack
  channel via webhook or bot token, at info, warn, or alert severity.
  See also: [Slack](/guides/slack/).
- **starter fleet**: The recommended first roster (Drake, Lucius, Ra's al
  Ghul, agent-cleanup) the installer enables when you pass `--agents starter`.
  See also: [Install](/getting-started/install/).
- **state machine**: The label transitions on an issue
  (`agent:implement` → `agent:in-flight` → `agent:pr-open` → `agent:done`)
  that coordinate agent handoffs. See also: [State machine](/concepts/state-machine/).
- **`systemd --user`**: Linux host scheduler that owns the firing cadence
  on Debian/Ubuntu; the equivalent of launchd on macOS.
  See also: [Linux](/guides/linux/).
- **turn budget**: Per-firing cap on LLM turns (`max_turns`) plus daily
  rolling caps on turns and cost, enforced before and during a firing.
  See also: [Architecture](/concepts/architecture/).
- **worktree**: Throwaway git worktree under `$ALFRED_HOME/worktrees/`
  branched from a fresh `origin/main`, where the engine writes code for one
  firing. See also: [Alfred on a monorepo](/guides/monorepo/).
- **WORKSPACE_ROOT**: Operator-set directory whose `product/` subdirectory
  contains the local checkouts of every repo Alfred operates against.
  See also: [Workspace patterns](/getting-started/workspace-patterns/).
