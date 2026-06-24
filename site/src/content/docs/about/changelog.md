---
title: Changelog
description: Recent Alfred releases. Full history in CHANGELOG.md.
---

A readable summary of recent releases. The canonical, complete history lives in [`CHANGELOG.md`](https://github.com/luminik-io/alfred-os/blob/main/CHANGELOG.md) on GitHub, which follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and [Semantic Versioning](https://semver.org/spec/v2.0.0.html); tagged releases are at [github.com/luminik-io/alfred-os/releases](https://github.com/luminik-io/alfred-os/releases). For forward-looking work (in flight, next, horizon), see the [roadmap](/about/roadmap/).

## Unreleased

Changes merged to `main` since the last tagged release. The running list is the [`[Next]` section of `CHANGELOG.md`](https://github.com/luminik-io/alfred-os/blob/main/CHANGELOG.md).

## 0.5.3 (2026-06-24)

- The signed, notarized macOS app and Linux packages ship on this release: `brew install --cask alfred-os` installs the desktop app and `brew install alfred-os` installs the CLI, or grab the assets from [`/download/`](/download/).
- Ask is now a real conversation: a plain question gets a direct answer instead of a saved plan, and Alfred only opens the plan-and-issue flow when you are describing work to build.
- The workflow canvas is the full-width primary surface, with automatic left-to-right pipeline layout, fit and zoom controls, a status-colored minimap, and each agent's detail in a dismissible drawer.
- Activity tells the real story of a run: a one-line headline that expands to the full step timeline, an "Errors only" filter, and a failure that reports its real cause rather than a misleading provider message.
- A self-healing reliability core classifies failures: transient errors retry the same engine with backoff, authentication and budget failures surface honestly without retrying, and the engine fallback fires only on a real capability gap.
- A code-structure memory layer attaches over MCP, giving agents code search, call-graph, blast-radius, and ownership lookups while planning. On by default, opts out with `ALFRED_CODE_MEMORY_MCP=0`, and a clean no-op when not installed.
- Alfred keeps the most useful lessons on its own: a confidence reviewer saves a sound, evidence-backed lesson into recall without you, holds anything it is unsure about, and every automatic save is reversible. The local memory server is now a plain lesson store.
- The first-run setup wizard shows honest progress and states its value once, scheduled runs read auth from one place with an early auth preflight, and the emergency disk cleanup reclaims regenerable caches so a full disk no longer stalls the fleet.

## 0.5.2 (2026-06-22)

- The native app ships the refreshed dark interface from the public source, so Inbox / Ask / Work / Agents / Setup look the same in the internal and OSS builds.
- Redis Agent Memory is now the default local memory layer, with FleetBrain kept as the review and reliability layer; docs and install copy match that runtime path.
- Slack planning threads are easier to follow: the first message needs the Alfred mention, and follow-up replies stay attached to the same plan.
- Repo-graph support in the code-map flow, and visual-QA tooling (screenshot parity, pixel-sweep hardening) for the desktop client.
- Fresher public-site impact proof, cleaner mobile hero layouts, and launch copy that explains Alfred's real value: coding agents keep work moving from Slack, GitHub, or a rough plan while you are away.

## 0.5.1 (2026-06-17)

- Public [`/download/`](/download/) page links the signed desktop artifacts (`Alfred.dmg`, `Alfred.app.zip`, `Alfred.AppImage`, `Alfred.deb`), and the homepage points there directly.
- `alfred serve` and the desktop app now agree on port 7010 by default, avoiding macOS Control Center's use of port 7000; stale saved 7000 URLs are migrated automatically.
- Docs and site were updated to match the shipped native client: signed and notarized macOS artifacts, Linux AppImage and Debian packages, and the current Inbox / Ask / Work / Agents / Setup navigation.
- Cleared high-severity frontend audit findings across the desktop and site lockfiles, and silenced `astro-mermaid` console noise on diagram-free pages.

## 0.5.0 (2026-06-15)

The first native desktop app, live subscription-usage display, a single-repo approval gate, a disk guardian, and review-first fleet memory.

- First signed native desktop app for Mac and Linux (`clients/desktop`, built with Tauri): Inbox, Ask, Work, Agents, and Setup over the local `alfred serve` API. Everything still runs from the command line and Slack.
- Live Claude and Codex subscription usage in the app, read from each tool's own local state. A window it cannot confirm reads "not synced" rather than a fabricated number.
- Single-repo work now waits for your go-ahead: a planned single-repo change is held with an approval label until you approve it, the same human gate that already protected multi-repo work.
- Disk guardian: when free space drops below a safe floor, Alfred reclaims its own leftover files first and otherwise skips the run cleanly (no crash-loop) with one throttled heads-up.
- An approved Slack plan can become a labeled GitHub issue in one step. It is off by default, requires both a trusted person and an explicit approval word, and only files an issue: it never runs code on its own.
- Review-first fleet memory: lessons from real runs queue as candidates you approve or reject from Slack or the app before they shape future work, with an optional Redis Agent Memory Server provider.
- Setup polish: `alfred setup-token` sets up a long-lived sign-in token so scheduled agents stay authenticated, plus search-engine basics and consent-gated analytics on the site.

## 0.4.0 (2026-05-23)

Substrate, observability, planning, approval, memory, and connector primitives. The largest single release since 0.1.0; lays down building blocks the next two quarters of roadmap items compose on.

- **`agent_runner` package decomposition**: the single-file monolith becomes a 10-file package (preflight, lock, spend, engines, gh, slack, event-log, commit-trailer, transcripts, dedup). Public import surface preserved.
- **`alfred metrics` + `alfred logs` CLIs**: weekly per-agent rollups (firings, cost, turns, tool-use), per-firing stream-JSON transcript inspection.
- **State machine + multi-repo**: atomic `LabelClient` for the issue-claim state machine, `cross_repo_pr` coordinator for stacked PRs across repos, managed `multi_worktree` pool, `alfred label-state` CLI.
- **Damian + Batman planning/execution**: Damian files `agent:bundle:<slug>` siblings across affected repos; Batman executes the approved plan flow by applying the gate, preserving scope, and filing child issues.
- **`slack_approval`**: reaction-based approval gate as a `typing.Protocol` so the call site can swap Slack for any other channel.
- **FleetBrain local ledger**: review candidates, failure history, worker heartbeats, GitHub cache, and local reliability tooling. Redis Agent Memory is the default recalled-lesson store; FleetBrain remains the local evidence and review layer. See [Memory providers](https://github.com/luminik-io/alfred-os/blob/main/docs/MEMORY_PROVIDERS.md).
- **`Connector` Protocol + Linear + Sentry**: pull-mode adapters into the `agent:implement` queue, env-only credentials, one bad connector cannot break the sync.
- **`alfred serve` v1**: localhost-only, read-only FastAPI dashboard with three views (fleet status, recent firings, single-firing detail).
- **`slop_detector`**: PR-time linter for AI-authored prose (banned vocabulary, em-dashes, hedged numbers, marketing fluff) with JSON-configurable rules.

## 0.3.0 (2026-05-21)

- **Linux support** via `systemd --user` timers, a Debian/Ubuntu apt lane in `install.sh`, systemd unit rendering in `deploy.sh`, and a `lib/scheduler.py` host abstraction behind the `alfred` CLI. See [Linux](/guides/linux/).
- **`--dry-run` mode**: run a full agent firing lifecycle with every side-effecting boundary stubbed: no LLM call, no spend, no Slack post, no GitHub or git mutation. Works with zero host config. See [Dry-run mode](/getting-started/dry-run/).
- **`alfred pause` / `resume` / `run`** control verbs; `alfred agents` now shows a real scheduler-load column.
- **`alfred claude probe`**, **`alfred codex status/probe`**, and **`alfred auth status/probe`**: first-class Claude Code and Codex CLI auth diagnostics.
- **Solo-builder setup cleanup**: `alfred-init.py --repos`, starter-fleet default, prompt seeding, standard GitHub label setup, and Batman visible as an opt-in cross-repo architect.

## 0.2.1 (2026-05-12)

Public launch hardening release.

- Checked-in CodeQL workflow (Actions, Python, Ruby, JS/TS) with PR, push, scheduled, and manual triggers.
- Optional Hermes integration guide (since removed).
- Stopped Lucius from logging GitHub issue-author trust details to stdout/Slack (CodeQL clear-text-logging fix).
- Public repo metadata moved to clearer Alfred positioning; squash-only merges + Dependabot.

## 0.2.0 (2026-05-12)

The pivot from "extracted framework substrate" to "complete engineering agent fleet". The default install ships 12 working agents configured via the interactive `alfred-init` wizard.

- **Role field on every agent**: `agents.conf` gains a 6th column; the role surfaces in CLI and Slack output.
- **Runner-level fleet gate**: `enabled.txt` plus `alfred enable / disable / agents`.
- **Slack threading + Block Kit + severity colour stripes**: `lib/slack_format.py` with bot-token-aware per-firing threads.
- **Runner-side dedup**: `find_open_authored_pr_for_issue` + `reuse_or_make_worktree` so partial work survives across firings.
- **Release-readiness hardening**: Lucius wraps GitHub issue content as untrusted input and checks issue-author association before autonomous execution.
