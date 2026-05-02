# Changelog

All notable changes to pennyworth will be documented here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Issue claim state machine** — `claim_issue` / `release_issue` / `find_stale_claims` / `force_release_stale_claim` / `is_repo_paused` / `set_repo_paused` / `list_paused_repos` / `issue_dedup_check`. Lifecycle labels `agent:in-flight` / `agent:pr-open` / `agent:done` plus operator-override `do-not-pickup`. Full doc at `docs/STATE_MACHINE.md`.
- **Slack severity routing** — `slack_post(text, severity="info" | "warn" | "alert")`. `info` is back-compat default; `warn` prefixes ⚠️; `alert` prefixes 🚨 + appends `<!here>`.
- **Fresh-machine bootstrap** — `install.sh` (idempotent, brew + npm + dirs + shell rc), `INSTALL.md` (TL;DR + step-by-step), `.pennyworthrc.example` (operator config template).
- **Setup walkthroughs** — `docs/SLACK_SETUP.md`, `docs/AWS_SETUP.md`, `docs/CLAUDE_CODE.md`, `docs/SKILLS.md`, `docs/LINUX.md`.
- **Tutorial** — `docs/TUTORIAL.md` builds the `Echo` reference agent end-to-end in 30 minutes.
- **Operator-facing CLI example** — `examples/bin/label_state.py` (`claim` / `release` / `dedup-check` / `status-issue` / `repo` / `sweep-claims`).
- **Pre-push git hook** — `examples/git-hooks/pre-push` blocks pushes that would race an in-flight agent.
- **Docs site** — Astro Starlight at `site/`, deployed to GitHub Pages on push to main.
- **CI** — `pytest` + `ruff` + `mypy` + `shellcheck` on every PR.
- **Release automation** — tag-driven GitHub release with auto-extracted changelog notes.
- **Project hygiene** — `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, issue templates, PR template, `dependabot.yml`, `pyproject.toml` (ruff + mypy), `.pre-commit-config.yaml`.
- **`Formula/pennyworth.rb`** — Homebrew formula skeleton.
- **`ROADMAP.md`** — shipped / in-flight / out-of-scope.

### Changed

- `STANDARD_LABELS` now includes the lifecycle labels; consumers no longer need to extend it for the state machine to work.

### Deferred

- **Threading for `info`-tier Slack posts** — requires bot token (`xoxb-…`) + `chat.postMessage` with `thread_ts`. Webhooks cannot thread.
- **Channel topic updates** — same constraint; needs bot token.
- **Linux support** — `launchd` is macOS-only. Interim cron and hand-rolled systemd-user instructions live in `docs/LINUX.md`.

## [0.1.0] — 2026-05-02

Initial extraction from [`luminik-io/alfred`](https://github.com/luminik-io/alfred).

### Added

- `lib/agent_runner.py` — preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table primitives.
- `bin/doctor.sh` — host validator (preflight every agent under `HERMES_DOCTOR=1`).
- `bin/hermes-claude` — swap helper for two Claude accounts.
- `launchd/_template.plist` + `launchd/render.sh` + `launchd/agents.conf.example` — plist generation.
- `deploy.sh` — copy lib + bin into `$HERMES_HOME`, render plists, bootstrap launchd.
- `examples/bin/hello.py` — minimal codename-agent reference.
- `tests/test_agent_runner.py` — 22 cases covering preflight, doctor_mode, load_prompt, commit_trailer, HandoffTable, EventLog, _full_repo.
- Top-level docs: `README.md`, `ARCHITECTURE.md`, `BOOTSTRAP.md`, `CONTRIBUTING.md`, `LICENSE` (MIT), `docs/INDEX.md`.

[Unreleased]: https://github.com/luminik-io/pennyworth/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/luminik-io/pennyworth/releases/tag/v0.1.0
