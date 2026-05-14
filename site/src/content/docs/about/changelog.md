---
title: Changelog
description: Recent Alfred OS releases. Full history in CHANGELOG.md.
---

Recent releases. The canonical, complete history lives in [`CHANGELOG.md`](https://github.com/luminik-io/alfred-os/blob/main/CHANGELOG.md) on GitHub — [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Tagged releases are at [github.com/luminik-io/alfred-os/releases](https://github.com/luminik-io/alfred-os/releases).

## Unreleased

What's about to ship. See the [`[Unreleased]`](https://github.com/luminik-io/alfred-os/blob/main/CHANGELOG.md#unreleased) section for the live list.

- **Linux support** via `systemd --user` timers — a Debian/Ubuntu apt lane in `install.sh`, systemd unit rendering in `deploy.sh`, and a `lib/scheduler.py` host abstraction behind the `alfred` CLI. See [Linux](/guides/linux/).
- **`--dry-run` mode** — run a full agent firing lifecycle with every side-effecting boundary stubbed: no LLM call, no spend, no Slack post, no GitHub or git mutation. Works with zero host config. See [Dry-run mode](/getting-started/dry-run/).
- **`alfred pause` / `resume` / `run`** operator verbs; `alfred agents` now shows a real scheduler-load column.
- **`bin/doctor.sh --dev`** — dev-install mode that tolerates host-config gaps while still failing hard on code defects.
- **`alfred claude probe`** — a first-class Claude Code auth smoke test.
- Docs: a [publishing guide](https://github.com/luminik-io/alfred-os/blob/main/docs/PUBLISHING.md) for maintainers, a rewritten [Linux guide](/guides/linux/), mermaid diagrams across the concept pages, and this docs site.

## 0.2.1 — 2026-05-12

First public launch cleanup pass.

- Checked-in CodeQL workflow (Actions, Python, Ruby, JS/TS) with PR, push, scheduled, and manual triggers.
- Optional [Hermes integration guide](/guides/hermes/).
- Stopped Lucius from logging GitHub issue-author trust details to stdout/Slack (CodeQL clear-text-logging fix).
- Public repo metadata moved to the sharper `alfred-os` positioning; squash-only merges + Dependabot.

## 0.2.0 — 2026-05-12

The pivot from "extracted framework substrate" to "complete engineering agent fleet". The default install ships 12 working agents configured via the interactive `alfred-init` wizard.

- **Role field on every agent** — `agents.conf` gains a 6th column; the role surfaces in CLI and Slack output.
- **Runner-level fleet gate** — `enabled.txt` plus `alfred enable / disable / agents`.
- **Slack threading + Block Kit + severity colour stripes** — `lib/slack_format.py` with bot-token-aware per-firing threads.
- **Bundle-label model + Batman skeleton** — `lib/batman.py` for the multi-repo coordinator.
- **Runner-side dedup** — `find_open_authored_pr_for_issue` + `reuse_or_make_worktree` so partial work survives across firings.
- **Fleet doctor** — read-only health checks into a single severity-stripe Slack thread.
- **Release-readiness hardening** — Lucius wraps GitHub issue content as untrusted input and checks issue-author association before autonomous execution.
