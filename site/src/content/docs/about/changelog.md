---
title: Changelog
description: Recent Alfred releases. Full history in CHANGELOG.md.
---

Recent releases. The canonical, complete history lives in [`CHANGELOG.md`](https://github.com/luminik-io/alfred-os/blob/main/CHANGELOG.md) on GitHub. It follows the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format and [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Tagged releases are at [github.com/luminik-io/alfred-os/releases](https://github.com/luminik-io/alfred-os/releases).

## Next

- **`alfred setup-token`**: one-command bootstrap of `CLAUDE_CODE_OAUTH_TOKEN` so launchd / `systemd --user` agents authenticate without the host credential store. Wraps `claude setup-token`, captures the token, writes one managed export line to `~/.alfredrc` with 0600 perms, and is idempotent under `--force` rotation. The `alfred-init` wizard now offers it on first install. See [Claude Code auth](https://github.com/luminik-io/alfred-os/blob/main/docs/CLAUDE_CODE.md#authenticating-scheduled-launchd--systemd-firings).
- **`ALFRED_FLEET_OVERLAY` hook**: optional operator-supplied module imported at the end of `agent_runner` init, so a fleet can populate `GH_REPO_TO_LOCAL`, `STANDARD_LABELS`, and `HANDOFFS` from one place instead of forking every `bin/*.py`.
- **`preflight()` slug-to-local mapping**: consults `GH_REPO_TO_LOCAL` when checking that local checkouts exist, so multi-repo workspaces with renames (`org/myorg-backend` cloned at `product/backend/`) stop reporting bogus "missing checkout" errors.
- **Python 3.14 bytes/str fix**: `subprocess.TimeoutExpired.stdout` returns bytes on Python 3.14 even when `text=True` is passed to `subprocess.run`. The `agent_runner.process.run` wrapper now decodes so downstream callers that hand the result to `Path.write_text` keep working.
- **Fleet memory reliability tools**: reviewable memory candidates, normalized failure-event history, `alfred brain doctor`, `alfred brain harvest` for repeated-failure lesson candidates, and a read-only `alfred mcp serve` bridge for local MCP clients.
- **Optional Redis AMS memory provider**: operators who already run Redis Agent Memory Server can set `ALFRED_MEMORY_PROVIDERS=fleet,redis`, check it with `alfred brain redis-status`, and sync reviewed lessons with `alfred brain redis-sync` while keeping the default install local and dependency-light.
- **Slack memory curation**: trusted users can run `memory`, `memory harvest`, `remember [repo:] <lesson>`, `memory remember ...`, and `memory redis` from Slack to inspect and stage memory, while operator-only `memory promote <id>` / `memory reject <id>` control what enters future recall.
- **Scheduled memory harvest**: `memory-harvest.py` queues reviewable repeated-failure candidates from the reliability governor, nudges Slack only when there is something to review, and never promotes lessons or syncs Redis by itself.
- **Reviewable runtime memory by default**: engine-written reflections now queue as memory candidates unless `ALFRED_MEMORY_REFLECTION_MODE=direct` is set explicitly.
- **Planning memory loop**: the Planning tab recalls promoted repo lessons while drafting, embeds advisory hints in saved specs, and queues reviewable spec-to-issue memory candidates.
- **`alfred serve` control-surface polish**: Fleet / Firings / Plans / Planning tabs, saved Alfred plan inbox, issue/spec intake, human-readable timestamps, and mobile card layouts for the previously cramped tables.
- **Batman planning replies**: `add repo:` and `remove repo:` replies in the approval thread now amend execution scope before child issues or worktrees are created.
- **Slack follow-up capture**: trusted replies after Batman reports or PR links are classified, acknowledged, and carried as context for the next plan or PR pass without granting merge approval.
- **Slack planning listener**: optional Socket Mode listener for trusted DMs,
  app mentions, and registered Alfred threads. It saves local planning drafts
  and feedback context while keeping reaction approval as the only execution
  gate.
- **Alfred Desktop preview**: `clients/desktop` ships the first Tauri
  Mac/Linux shell over `alfred serve` JSON APIs, with Home, Compose, Fleet,
  Logs, and Setup gear surfaces plus external Slack/GitHub/local links.
- **`alfred spec`**: template, lint, and readiness helpers for specs-driven development, including acceptance criteria, rollout checks, and GitHub-ready issue drafts.
- **Removed**: `lib/claude_proxy/`, `bin/claude-proxy.py`, the four proxy tests, `docs/CLAUDE_PROXY.md`, `docs/MACOS_KEYCHAIN.md`, `bin/alfred-grant-keychain.sh`, and `examples/launchd/luminik.claude-proxy.plist.example`. The proxy daemon shipped in v0.4.0 worked around a macOS Keychain ACL issue that `CLAUDE_CODE_OAUTH_TOKEN` resolves natively. Operators who installed the proxy should `launchctl bootout` it and unset `ALFRED_CLAUDE_PROXY_SOCKET`. No agent-script changes needed.

## 0.4.0 (2026-05-23)

Substrate, observability, planning, approval, memory, and connector primitives. The largest single release since 0.1.0; lays down building blocks the next two quarters of roadmap items will compose on.

- **`agent_runner` package decomposition**: the single-file monolith becomes a 10-file package (preflight, lock, spend, engines, gh, slack, event-log, commit-trailer, transcripts, dedup). Public import surface preserved.
- **`alfred metrics` + `alfred logs` CLIs**: weekly per-agent rollups (firings, cost, turns, tool-use), per-firing stream-JSON transcript inspection.
- **State machine + multi-repo**: atomic `LabelClient` for the issue-claim state machine, `cross_repo_pr` coordinator for stacked PRs across repos, managed `multi_worktree` pool, `alfred label-state` operator CLI.
- **Damian + Batman planning/execution**: Damian files `agent:bundle:<slug>` siblings across affected repos; Batman now executes the approved plan flow by applying the gate, preserving scope, and filing child issues.
- **`slack_approval`**: reaction-based approval gate as a `typing.Protocol` so the call site can swap Slack for any other channel.
- **`fleet-brain` v1 memory**: SQLite-backed per-codename / per-repo `recall` / `reflect` with atomic writes, ULID ids, stdlib-only. `MemoryProvider` Protocol with chained + null implementations, optional `gbrain` shim. See [Fleet brain](https://github.com/luminik-io/alfred-os/blob/main/docs/FLEET_BRAIN.md).
- **`Connector` Protocol + Linear + Sentry**: pull-mode adapters into the `agent:implement` queue, env-only credentials, one bad connector cannot break the sync.
- **`alfred serve` v1**: localhost-only, read-only FastAPI dashboard with three views (fleet status, recent firings, single-firing detail).
- **`alfred-shipped-public`**: self-host emitter that writes a public allowlisted `weekly.json` from `$ALFRED_HOME/state`.
- **`slop_detector`**: PR-time linter for AI-authored prose (banned vocabulary, em-dashes, hedged numbers, marketing fluff) with JSON-configurable rules.
- **Three new concept pages** mirrored across docs/ and the site: state and memory, engine routing, operating the fleet.
- **ROADMAP** rewritten as a four-tier model: Shipped, In flight, Next, Horizon.
- **Fleet diagnostic + cleanup hardening**: pause-marker honouring under launchd, fail-streak + pause-marker sync, status-cache TTL, throttled preflight Slack alerts, distinct alert for concurrent engine-auth failures.

## 0.3.0 (2026-05-21)

- **Linux support** via `systemd --user` timers, a Debian/Ubuntu apt lane in `install.sh`, systemd unit rendering in `deploy.sh`, and a `lib/scheduler.py` host abstraction behind the `alfred` CLI. See [Linux](/guides/linux/).
- **`--dry-run` mode**: run a full agent firing lifecycle with every side-effecting boundary stubbed: no LLM call, no spend, no Slack post, no GitHub or git mutation. Works with zero host config. See [Dry-run mode](/getting-started/dry-run/).
- **`alfred pause` / `resume` / `run`** operator verbs; `alfred agents` now shows a real scheduler-load column.
- **`bin/doctor.sh --dev`**: dev-install mode that tolerates host-config gaps while still failing hard on code defects.
- **`alfred claude probe`**: a first-class Claude Code auth smoke test.
- **`alfred codex status/probe` and `alfred auth status/probe`**: first-class Codex CLI and combined provider-auth diagnostics.
- **Solo-builder setup cleanup**: `alfred-init.py --repos`, starter-fleet default, prompt seeding, standard GitHub label setup, and Batman visible as an opt-in cross-repo architect (plan-only in v0.3.0, gained execute-after-approval in v0.4.0).
- Docs: a [publishing guide](https://github.com/luminik-io/alfred-os/blob/main/docs/PUBLISHING.md) for maintainers, a rewritten [Linux guide](/guides/linux/), [Codex provider guide](https://github.com/luminik-io/alfred-os/blob/main/docs/CODEX_PROVIDER.md), mermaid diagrams across the concept pages, and this docs site.
- Fixes: Batman bundle scans stay inside the selected repository scope, and `alfred auth status` now returns nonzero when the Codex CLI status path fails.

## 0.2.1 (2026-05-12)

Public launch hardening release.

- Checked-in CodeQL workflow (Actions, Python, Ruby, JS/TS) with PR, push, scheduled, and manual triggers.
- Optional [Hermes integration guide](/guides/hermes/).
- Stopped Lucius from logging GitHub issue-author trust details to stdout/Slack (CodeQL clear-text-logging fix).
- Public repo metadata moved to clearer Alfred positioning; squash-only merges + Dependabot.

## 0.2.0 (2026-05-12)

The pivot from "extracted framework substrate" to "complete engineering agent fleet". The default install ships 12 working agents configured via the interactive `alfred-init` wizard.

- **Role field on every agent**: `agents.conf` gains a 6th column; the role surfaces in CLI and Slack output.
- **Runner-level fleet gate**: `enabled.txt` plus `alfred enable / disable / agents`.
- **Slack threading + Block Kit + severity colour stripes**: `lib/slack_format.py` with bot-token-aware per-firing threads.
- **Bundle-label model + Batman skeleton**: `lib/batman.py` for the architect agent.
- **Runner-side dedup**: `find_open_authored_pr_for_issue` + `reuse_or_make_worktree` so partial work survives across firings.
- **Fleet doctor**: read-only health checks into a single severity-stripe Slack thread.
- **Release-readiness hardening**: Lucius wraps GitHub issue content as untrusted input and checks issue-author association before autonomous execution.
