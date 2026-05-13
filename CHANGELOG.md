# Changelog

Notable changes to Alfred. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Publishing guide for GitHub Pages workflow mode, release-site verification, and optional custom-domain setup.
- `alfred claude probe` for a first-class Claude Code auth smoke test.

### Changed

- Refreshed README, roadmap, docs site status, and release checklist after the v0.2.1 public launch cleanup.
- Switched the public docs URL to `https://alfred.luminik.io/` and made docs-site links root-relative for the custom domain.
- Moved Claude account routing fully into `alfred claude`; the standalone helper is no longer shipped.

## [0.2.1] - 2026-05-12

Patch release for the first public launch cleanup pass.

### Added

- Checked-in CodeQL workflow for GitHub Actions, Python, Ruby, and JavaScript/TypeScript, with PR, push, scheduled, and manual dispatch triggers.
- Optional Hermes integration guide in `docs/HERMES.md` and the docs site.

### Fixed

- Stopped Lucius from logging GitHub issue-author trust details to stdout or Slack, resolving the CodeQL clear-text logging alerts on `bin/lucius.py`.
- Fixed GitHub Pages manual dispatch so the site can be republished without a code change.

### Changed

- Public repository metadata now uses the sharper `alfred-os` positioning, squash-only PR merges, auto-update branches, and Dependabot security updates.

## [0.2.0] - 2026-05-12

Pivot from "extracted framework substrate" to "complete engineering agent fleet". The default install now ships 12 working agents the operator configures via an interactive `alfred-init` wizard.

### Added

#### 2026-05-09 public fleet release

- **Role field on every agent.** `agents.conf` gets a 6th tab-separated column carrying a one-line operational descriptor; `render.sh` emits `ALFRED_<CODENAME>_ROLE` env vars; `agent_role()` / `codename_with_role()` surface the role in CLI + Slack post prefixes.
- **Runner-level fleet gate file.** New `$HERMES_HOME/state/fleet/enabled.txt` plus `is_agent_enabled` / `enable_agent` / `disable_agent` helpers. Listed codenames are enabled; missing codenames fall back to each runner's default so opt-in agents can be gated without making normal launchd agents look disabled. New `bin/alfred` CLI ships `alfred enable / disable / agents / enabled-agents`.
- **Slack threading + Block Kit + severity colour stripes.** New `lib/slack_format.py` with bot-token-aware `firing_thread_root` / `firing_thread_reply` / `firing_thread_close`. Attachment duplicate-render guard baked in from day one. Honours `BATMAN_APPROVAL_CHANNEL` for routing.
- **Bundle-label model + Batman skeleton.** New `lib/batman.py` with `Bundle` dataclass, all-or-nothing `claim_bundle`, best-effort `release_bundle`, loose-markdown `parse_plan_from_issue` / `parse_plan_from_bundle`. Scope-widening guard included. New `bin/batman.py` skeleton runner posts plan summaries; full execution chain deferred.
- **Runner-side dedup.** `find_open_authored_pr_for_issue` (with substring-false-positive guard) + `reuse_or_make_worktree` so partial work survives across firings of the same issue.
- **STANDARD_LABELS bootstrap.** `batman-pr-open` and `agent:large-feature` ship by default; `gh_pr_create` auto-creates ad-hoc labels and surfaces gh stderr on failure.
- **Fleet doctor.** New `bin/fleet-doctor.py` ships four read-only health checks (paused repos, global block, stale worktrees, fleet enable list) → single severity-stripe Slack thread.
- **Runner safety hardening.** Batman and fleet-doctor now acquire the shared lock helper correctly; cleanup scopes `/tmp` sweeping to agent-owned prefixes instead of broad wildcard matches.
- **Release-readiness hardening.** Lucius wraps GitHub issue content as untrusted input, checks issue author association before autonomous code execution, grants Codex the source `.git` directory for worktree commits, and opens salvaged WIP PRs as real GitHub drafts. Drake's daily cap guard now scales its GitHub search limit above the configured cap. Lock-owner checks now validate the recorded agent name when the caller knows it.


#### Engineering agents (`bin/`)

- **lucius** (feature dev): picks the oldest open `agent:implement` issue, claims it via the state machine, opens a worktree, runs `claude -p` with the issue body, pushes a PR labelled `agent:authored`.
- **drake** (planner): files new `agent:implement` issues from specs / roadmap / code-reality grep. Caps per-firing + rolling-24h.
- **bane** (test coverage): picks the lowest-coverage actively-changed file, writes tests, opens a PR.
- **rasalghul** (PR review): multi-axis review on every fresh PR. Posts as comment.
- **nightwing** (review-fix): lands fixes for P0 / P1 reviewer comments on `agent:authored` PRs.
- **robin** (bug triage): classifies severity, asks for repro info, hands off to lucius. Local touched-issues ledger prevents re-triage.
- **huntress** (post-deploy smoke): runs Playwright tests against `ALFRED_HUNTRESS_TARGET_URL`. Optional ECS staging-readiness pre-check + S3 screenshot upload.
- **gordon** (deploy health): daily ECS task-def vs `main` HEAD diff + top-N Sentry issues. Quiet on healthy days.
- **automerge**: squash-merges clean `agent:authored` PRs (CI green, no unresolved P0 reviewer comments, latest review ends "Ship-ready: yes").
- **agent-cleanup**: daily housekeeping (clean stale worktrees, stuck locks, stale `agent:in-flight` claims via `force_release_stale_claim`). Dirty or unknown worktrees are skipped and reported.
- **code-map-refresh**: cross-repo contract scan. Writes `${HERMES_HOME}/state/code-map.json` for other agents.
- **agent-morning-brief**: daily Slack post — yesterday's PRs, in-flight work, doctor status.
- **fleet-recap.sh**: 07:30 + 22:00 Slack digest (per-agent firings / cost / success rate).

Every codename is operator-customisable at install time. Default Batman names; runtime codename via `AGENT_CODENAME` env (set by the launchd plist). Repo lists, AWS profiles, ECS clusters, Sentry orgs all env-driven.

#### Engineering-agent prompts (`prompts/`)

9 role-based prompt templates compatible with `agent_runner.load_prompt()` and `${VAR}` substitution: `feature-dev.md`, `planner.md`, `test-coverage.md`, `code-review.md`, `review-fix.md`, `bug-triage.md`, `ecs-monitor.md`, `post-deploy-smoke.md`, `cross-repo-coordinator.md`. Cross-codename refs use `${FEATURE_DEV_CODENAME}` / `${CODE_REVIEW_CODENAME}` etc. so renaming any agent can stay consistent end-to-end.

#### Substrate (`lib/agent_runner.py`)

- **Issue claim state machine**: `claim_issue` / `release_issue` / `find_stale_claims` / `force_release_stale_claim` / `is_repo_paused` / `set_repo_paused` / `list_paused_repos` / `issue_dedup_check`. Lifecycle labels `agent:in-flight` / `agent:pr-open` / `agent:done` plus operator-override `do-not-pickup`. Full doc at `docs/STATE_MACHINE.md` (with Mermaid stateDiagram).
- **Slack severity routing**: `slack_post(text, severity="info" | "warn" | "alert")`. `info` is back-compat default; `warn` prefixes ⚠️; `alert` prefixes 🚨 + appends `<!here>`.
- **`claude_invoke_streaming()` + `transcript_path()`**: streaming-API-compatible signatures (currently delegate to plain `claude_invoke`; the per-firing JSONL transcript writer ships in a future release).
- **`TRANSCRIPTS_ROOT` + `PROMPTS_ROOT`** module constants.

#### Operator surface

- **`alfred-init`** (`bin/alfred-init.py`): interactive 13-step wizard. Walks Slack-app creation with real test-post; AWS / env-var storage choice; multi-select agent enable; per-role codename prompt with Batman defaults; per-agent repo selection from `gh repo list`; per-agent special prompts (Huntress staging URL, Gordon ECS cluster); generates `agents.conf` + `~/.alfredrc` with banner-marked block; runs `deploy.sh` + `bin/doctor.sh`; smoke-test post. 27 tests covering helpers + doctor sentinel + non-interactive mode.
- **`examples/bin/label_state.py`** (operator CLI example): `claim` / `release` / `dedup-check` / `status-issue` / `repo {pause,resume,list}` / `sweep-claims`.
- **`examples/git-hooks/pre-push`**: refuses pushes that race in-flight agents.
- **`install.sh`**: idempotent fresh-machine bootstrap (brew + npm + dirs + shell rc).

#### Documentation

- `INSTALL.md` (TL;DR + step-by-step) + `BOOTSTRAP.md` (deeper operations guide).
- `docs/AGENTS.md`: codename topology with Batman defaults, customisation story, fleet-map Mermaid diagram, codename-wiring Mermaid diagram, anti-patterns, "adding a new codename" walkthrough.
- `docs/STATE_MACHINE.md`: lifecycle Mermaid stateDiagram + race-resolution + stale-sweep + operator overrides.
- `ARCHITECTURE.md`: per-firing flow Mermaid sequenceDiagram + design rationale.
- `docs/SLACK_SETUP.md`, `docs/AWS_SETUP.md`, `docs/CLAUDE_CODE.md`, `docs/SKILLS.md`, `docs/LINUX.md`, `docs/TUTORIAL.md`.
- Astro Starlight site at `site/`: 16 pages (getting-started / concepts / guides / reference / about), with GitHub Pages publishing gated by `ALFRED_OS_PUBLISH_PAGES`. URL env-overridable.

#### Project hygiene

- CI: `pytest` (3.11 / 3.12 / 3.13) + `ruff check` + `ruff format --check` + `mypy lib/` + `shellcheck` + `python-syntax` + `scrub-check` (refuses known-private patterns).
- `bin/scrub-check.sh`: reusable local + CI scrub scan for host-private paths, fleet identifiers, Slack tokens/webhooks, and AWS access key IDs.
- `docs/RELEASE_CHECKLIST.md`: public release checklist with pre-tag gates, scrub requirements, and GitHub Release flow.
- Release automation: tag → GitHub release with auto-extracted changelog notes + brew-formula sha256 echoed to logs.
- `Formula/alfred-os.rb`: HEAD-only Homebrew formula until the first public release tarball has a checksum.
- `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`, issue templates, PR template, `dependabot.yml`, `pyproject.toml` (ruff + mypy), `.pre-commit-config.yaml`.

### Changed

- Repository renamed `luminik-io/pennyworth` → `luminik-io/alfred-os`. GitHub redirects in place. All env vars `PENNYWORTH_*` → `ALFRED_*` / `ALFRED_OS_*`. Operator config file `~/.pennyworthrc` → `~/.alfredrc`. Operator commands `pennyworth-*` → `alfred-*`.
- `STANDARD_LABELS` includes the lifecycle labels; consumers no longer need to extend it for the state machine to work.
- Per-repo configuration loaded from `~/.alfredrc.d/<codename>.toml` via stdlib `tomllib` (was PyYAML; PyYAML is not stdlib and shouldn't be required for a fresh install).
- Doctor mode runs before env-config IDLE checks across all 12 agents — `bash bin/doctor.sh` now reports all-passing on a fresh install before the operator runs `alfred-init`.
- `bin/doctor.sh` now falls back to the in-repo `bin/` and `lib/` paths before deploy, so a clean checkout can self-check without a pre-existing `$HERMES_HOME`.
- All docs voice-swept: removed audience-marketing intros, outcome-fantasy framing, hire/replace framing, LLM filler vocab, marketing emoji, sign-offs, vanity stats, em-dashes. ~210 lines of marketing prose deleted across 39 files; technical content preserved.

### Removed

- `MORNING.md` operator-brief file (now lives in PR descriptions / chat, not the tree).
- `uv.lock` from version control (auto-generated; consumers run their own `uv sync` against `pyproject.toml`).
- `sso-check-10` / `sso-check-22` from the default `agents.conf`. Operator-convenience reminders, not engineering. Mentioned in `docs/AWS_SETUP.md` for operators who use AWS SSO interactively.

### Deferred (v0.3)

- **Bot token integration** (`xoxb-…`): unlocks `slack_set_channel_topic()`, `chat.postMessage` with `thread_ts` for daily-thread routing of `info`-tier messages, reactions API. Webhooks cannot do these.
- **Drake-style proactive title-token dedup**: runner-level guard before invoking the planner. Catches "two issues, same work."
- **`claim_pr` / `release_pr`**: extend the state machine to PR-level work (review-fix agents racing the same PR).
- **`render-systemd.sh`**: first-class Linux scheduling.
- **Spend dashboards**: weekly recap rendered from per-agent spend files.
- **`alfred new-codename` scaffold**: single command to add a fresh codename agent (script template + agents.conf entry + label registration).
- **MCP server bundling**: expose `claim_issue` / `release_issue` / `slack_post(severity)` as MCP tools.
- **Real per-firing JSONL transcripts**: `claude_invoke_streaming` currently delegates to `claude_invoke`. The streaming impl with transcript file at `${HERMES_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl` ships with the future transcript-viewer command.

## [0.1.0] - 2026-05-02

Initial public framework extraction.

### Added

- `lib/agent_runner.py`: preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table primitives.
- `bin/doctor.sh`: host validator (preflight every agent under `HERMES_DOCTOR=1`).
- `alfred claude`: account-routing helper for two Claude accounts.
- `launchd/_template.plist` + `launchd/render.sh` + `launchd/agents.conf.example`: plist generation.
- `deploy.sh`: copy lib + bin into `$HERMES_HOME`, render plists, bootstrap launchd.
- `examples/bin/hello.py`: minimal codename-agent reference.
- `tests/test_agent_runner.py`: 22 cases covering preflight, doctor_mode, load_prompt, commit_trailer, HandoffTable, EventLog, _full_repo.
- Top-level docs: `README.md`, `ARCHITECTURE.md`, `BOOTSTRAP.md`, `CONTRIBUTING.md`, `LICENSE` (MIT), `docs/INDEX.md`.

[Unreleased]: https://github.com/luminik-io/alfred-os/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/luminik-io/alfred-os/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/luminik-io/alfred-os/compare/0c5d13c673f5954014cb5b5ccf3dc880c9563641...v0.2.0
[0.1.0]: https://github.com/luminik-io/alfred-os/pull/2
