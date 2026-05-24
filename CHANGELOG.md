# Changelog

Notable changes to Alfred. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Next]

### Added

- `alfred setup-token` (`bin/alfred-setup-token.py`): one-command bootstrap of the long-lived OAuth token. Detects whether `CLAUDE_CODE_OAUTH_TOKEN` is already set in the env or `~/.alfredrc`; if not, spawns `claude setup-token` interactively, parses the printed token, writes a single `export` line to `~/.alfredrc`, and tightens the file to 0600. Re-runs (`--force`) replace the existing block in place so rotation is idempotent. `--check-only` reports status without touching auth. `alfred-init` step 1 now offers to run this automatically when the token is missing.
- `CLAUDE_CODE_OAUTH_TOKEN` is the supported way to authenticate `claude` from launchd / systemd contexts. Run `claude setup-token` once (or `alfred setup-token` for the automated path) to mint a 1-year subscription token, export the value in `~/.alfredrc`, and `claude` reads it directly without touching the macOS Keychain or filesystem credential cache. See `docs/CLAUDE_CODE.md`.
- `ALFRED_FLEET_OVERLAY` hook in `agent_runner/__init__.py`: imports an operator-supplied module (default name `fleet_overlay`) at end of package init so a fleet can populate `GH_REPO_TO_LOCAL`, `STANDARD_LABELS`, and `HANDOFFS` from one place instead of forking every `bin/*.py`. Silently absent when no overlay is on the path.
- `preflight()` now consults `GH_REPO_TO_LOCAL` (with fallback to the slug) when resolving local checkout paths, so multi-repo workspaces with renames (`org/myorg-backend` checked out at `product/backend/`) stop reporting bogus "missing checkout" errors.
- `alfred-init --config` learned `role_repos`, `role_codename`, and `role_schedule`. Each maps an agent (by codename or role-key, case-insensitive) to a per-agent override: which repos that codename operates on, what codename to expose, and what schedule string to write into `agents.conf`. Previously non-interactive mode forced every repo-operating agent to claim every visible repo, which is wrong any time the operator wants codenames scoped to different surfaces (e.g. test-coverage skipping iOS, code-map-refresh only on JS/TS repos). `step_6_codenames` / `step_7_repos` / `step_8_schedule` preserve preset values from `--config` instead of overwriting them. Unknown agent keys, codenames that don't match `^[a-z][a-z0-9-]*$`, and malformed value types are surfaced as `warn` and skipped rather than silently dropped. Cross-platform — pure stdlib, no shell tricks, same behaviour on launchd and `systemd --user` consumers.
- `WORKSPACE_SUBDIR` env var lets operators name (or remove) the segment between `$WORKSPACE_ROOT` and `<repo>`. Default stays `product` for back-compat with `~/code/product/<repo>`; `WORKSPACE_SUBDIR=src` resolves to `$WORKSPACE_ROOT/src/<repo>`; `WORKSPACE_SUBDIR=""` collapses to `$WORKSPACE_ROOT/<repo>` directly. Unblocks operators whose existing layout is `~/repos/<repo>`, `~/Claude Workspace/<repo>`, or similar without symlinking around the previously-hardcoded `product/`. Documented in `docs/WORKSPACE_PATTERNS.md` and `.alfredrc.example`.

### Removed

- `lib/claude_proxy/`, `bin/claude-proxy.py`, `tests/test_claude_proxy_*.py`, `docs/CLAUDE_PROXY.md`, `examples/launchd/luminik.claude-proxy.plist.example`. The proxy daemon shipped in v0.4.0 worked around a macOS Keychain ACL issue that `CLAUDE_CODE_OAUTH_TOKEN` resolves natively. `ALFRED_CLAUDE_PROXY_SOCKET` is no longer read; `claude_invoke_streaming` now always uses the direct subprocess path. Operators using the proxy should `launchctl bootout` it and unset the env var; otherwise no migration is needed.
- `bin/alfred-grant-keychain.sh` and `docs/MACOS_KEYCHAIN.md`. The targeted Keychain ACL grant is no longer the recommended workaround because the OAuth-token path bypasses Keychain entirely.

### Fixed

- `agent_runner/process.run()` TimeoutExpired path: Python 3.14 returns `bytes` for `e.stdout` even when `text=True` was passed to `subprocess.run`. Callers passing the result to `Path.write_text` (notably rasalghul caching `gh pr diff`) crashed with `TypeError: data must be str, not bytes`. The wrap site now decodes bytes -> utf-8 with `errors="replace"`. Regression test patches `subprocess.run` to force the bytes case on every Python version.
- `lib/agent_runner/github.py`: added `agent:implement` to `LIFECYCLE_LABELS` so `claim_issue`'s first-call `ensure_labels` creates the entry-point label alongside the in-flight / pr-open / done / sticky-modifier labels. `labels.py` already lists it in `LIFECYCLE_LABEL_SET`; the runner list was the odd one out. Test `test_label_constants_match_agent_runner_existing_values` updated to assert `IMPLEMENT in runner_names`. (`alfred-init.py` step_10_labels still handles the operator-facing bootstrap for wizard-configured repos; this change covers programmatic / non-wizard consumers.)
- `.gitignore`: added `launchd/agents.conf` and `launchd/_generated/`. Both are per-operator artefacts (the conf names this host's fleet; the rendered plists hard-code `$ALFRED_HOME` paths). Without them gitignored, a fresh operator's first `git status` shows tracked-looking host-private files and `bin/scrub-check.sh` trips on the `/Users/<name>/.alfred` paths inside the rendered plists.
- `lib/agent_runner/github.py`: `ensure_labels` cache rewritten from `set[str]` (per repo) to `dict[str, set[str]]` (per repo + per label name). The old key meant a first call with `LIFECYCLE_LABELS` (e.g. from `claim_issue`) silently no-opped every later call with `STANDARD_LABELS` (e.g. from `gh_issue_edit` / `gh_pr_create`) on the same repo, leaving labels like `batman-pr-open`, `agent:large-feature`, `done-already` uncreated. Downstream `gh label add` then failed with "could not add label" and the runner surfaced "PR open failed" with no obvious cause. Two regression tests in `tests/test_gh_pr_create_labels.py` lock the per-label-name cache behaviour: a second call with a different catalogue creates the missing labels; a repeat call with the same catalogue is still a no-op.

## [0.4.0] - 2026-05-23

Substrate, observability, planning, approval, memory, and connector primitives. The largest single release since 0.1.0; lays down building blocks the next two quarters of roadmap items will compose.

### Added

#### Runner and observability

- `lib/agent_runner.py` decomposed from a single monolith into a 10-file `lib/agent_runner/` package: preflight, lock, spend, engines, gh, slack, event-log, commit-trailer, transcripts, dedup. Public import surface preserved. 50 new unit tests under `tests/unit/agent_runner/` cover the split modules; full suite grew from 689 to 749.
- `alfred metrics` (`bin/alfred-metrics.py`): per-agent rollup of firings, cost, turns, tool-use, and Codex tokens. `--since 7d`, `--codename`, `--by-day`, `--json`. Reads `$ALFRED_STATE_DIR` only.
- `alfred logs` (`bin/alfred-logs.py`): tail and filter per-firing stream-JSON transcripts. `--last N`, `--firing-id ID`, `--show-tool-calls`, `--json`. See `docs/CLI.md`.
- `lib/transcripts.py` and `lib/metrics.py`: `TranscriptReader` and `MetricsAggregator` protocols + filesystem-backed implementations, used by the two new CLIs and exposed for downstream code.

#### State machine and multi-repo

- `lib/labels.py`: `LabelClient` protocol + `GhCliLabelClient` implementation. Atomic transitions across the issue-claim state machine (`agent:queued` to `agent:implement` to `agent:in-flight` to `agent:pr-open` to `agent:done`), with race resolution and conflict detection.
- `lib/cross_repo_pr.py`: cross-repo PR coordinator. Opens stacked PRs across multiple repos with a shared spec id, links them via PR-body cross-references, marks the spec done only when all PRs merge.
- `lib/multi_worktree.py`: managed pool of git worktrees under `$ALFRED_HOME/wt`. Per-firing reservation, completion cleanup, crash recovery.
- `bin/alfred-label-state`: operator-facing CLI for the issue-claim state machine. `claim`, `release`, `dedup-check`, `status-issue`, `repo pause/resume/list`, `sweep-claims`. Pre-push hook recipe in `docs/STATE_MACHINE.md`.

#### Planning and execution

- Damian spec-bundle planner (`lib/damian_planner.py` + `bin/damian.py`): walks a spec directory, identifies multi-repo features, files `agent:bundle:<slug>` siblings across the affected repos. All-or-nothing per bundle. Caps at 3 bundles per firing. Single-repo work is left to drake.
- Batman now executes approved plans (`lib/batman.py` from 505 to 1383 lines; `bin/batman.py` from 261 to 472 lines). Once a Damian-style plan is approved (Slack reaction, label transition, or `BATMAN_AUTO_EXECUTE=1`), Batman drives the fan-out implementation across the listed repos. Previously Batman halted at plan-only. The `BATMAN_AUTO_EXECUTE` env contract: `0` = always ask, `approval-gate` = read approval signals, `1` = always execute. See `docs/BATMAN.md`.

#### Approvals

- `lib/slack_approval.py` + `docs/SLACK_APPROVAL.md`: reaction-based approval gate. An agent posts a proposal, the operator reacts with the configured emoji, the agent proceeds. `ApprovalGate` is a `typing.Protocol` so the same call site can swap Slack for any other channel. New env vars: `ALFRED_OPERATOR_SLACK_USER_ID`, `ALFRED_APPROVAL_EMOJI` (defaults to `:white_check_mark:`).

#### Quality gates

- `lib/slop_detector.py` + `bin/slop-detector.py` + `bin/curator.py`: PR-time linter for AI-authored prose patterns. 21 default rules covering banned vocabulary (seamless, unlock, leverage, transform), em-dashes, hedged numbers, marketing fluff. Rules are JSON-configurable; see `examples/slop-rules.json` and `docs/SLOP_DETECTOR.md`.

#### Memory

- `lib/fleet_brain/`: v1 SQLite-backed memory store. Per-codename and per-repo `recall` / `reflect`, atomic writes, ULID ids via the standard library, zero external dependencies. 948 lines of package code, 33 tests. Architecture and the v2 path (PGLite + Apache AGE + pgvector) in `docs/FLEET_BRAIN.md`. CLIs: `bin/alfred-brain.py`, `bin/fleet-ingest.py`.
- `lib/memory/`: `MemoryProvider` Protocol + `FleetBrainProvider`, `ChainedMemoryProvider`, and `NullMemoryProvider` implementations. Optional read-only `gbrain` subprocess shim for operators with a personal knowledge base. Chain order is env-driven: `ALFRED_MEMORY_PROVIDERS=fleet,gbrain`; default is fleet-brain only; `null` disables memory. See `docs/MEMORY_PROVIDERS.md`.

#### Connectors

- `lib/connectors/`: `Connector` Protocol + reference Linear and Sentry implementations. Pull-mode adapters from non-GitHub sources into the engineering fleet's `agent:implement` queue. Linear uses a stdlib GraphQL POST; Sentry uses a stdlib REST GET; both rely on env-only credentials (`LINEAR_API_KEY`, `SENTRY_AUTH_TOKEN`). One bad connector cannot break the sync. See `docs/CONNECTORS.md`, `bin/connector-sync.py`, `examples/connectors.yaml`.

#### Dashboards and proof

- `alfred serve` v1 (`bin/alfred-serve.py` + `lib/server/`): localhost-only, read-only FastAPI dashboard over `$ALFRED_HOME/state`. Three views: fleet status with HTMX auto-refresh, recent firings, single-firing detail. Reader injected as `typing.Protocol`. New `[serve]` optional dependency group for `fastapi`, `uvicorn`, `jinja2`. See `docs/SERVE.md`.
- `bin/alfred-shipped-public.py`: self-host emitter that reads `$ALFRED_HOME/state`, applies a public field allowlist + partner-name redaction table, and writes a `weekly.json` operators can publish on their own site. See `docs/SHIPPED_EMITTER.md`.

#### Fleet diagnostic + cleanup hardening

- Pause-marker honoring under launchd via `$ALFRED_HOME/state/_paused/<codename>` (paused agents stay paused across firings, not just at boot).
- Fail-streak / pause-marker sync at every self-pause site (lucius, drake, batman, rasalghul, nightwing).
- `ALFRED_CLEANUP_EXTRA_PATHS` env var: sweep operator-managed worktree pools outside `$ALFRED_HOME/worktrees`.
- Status-cache TTL stops stale reads when `alfred status` is invoked in quick succession.
- `ALFRED_PREFLIGHT_SLACK_MIN_MINUTES` throttles repeated preflight Slack alerts.
- `fleet-doctor` distinct alert for concurrent engine-auth failures (separates "claude not logged in" from generic firing errors).

#### Documentation

- Three new concept pages: state and memory, engine routing, operating the fleet. Mirrored across `docs/` (GitHub-rendered) and `site/src/content/docs/` (Starlight). Linked into the sidebar under Concepts and Getting Started.
- ROADMAP rewritten into a four-tier model: Shipped, In flight, Next, Horizon. Mirrored in `site/src/content/docs/about/roadmap.md`.

### Changed

- Core dependencies: `slack-sdk>=3.27` and `boto3>=1.34` moved from optional `[slack]` and `[aws]` extras into the base `dependencies` list. Slack and AWS are integral enough that the optional-extras split was adding install friction for new operators with no payoff.
- `pyproject.toml` adds the new `[serve]` optional-dependency group (FastAPI + uvicorn + Jinja2).
- `.gitignore` adds `.claude/` and `screenshots/` so per-agent worktrees, launch configs, and local verification screenshots stay out of the public repo.
- `.gitallowed` added so `git secrets` pre-commit hooks understand that `bin/scrub-check.sh` and CI workflows reference secret-pattern regexes by design.

### Fixed

- `lib/labels.py`: added `PLAN_PENDING_APPROVAL` constant (`agent:plan-pending-approval`) plus a backward-compat `LABEL_AGENT_PLAN_PENDING_APPROVAL` alias for code that imports the long-form name. Required by `lib/slack_approval.py` and `lib/batman.py`.
- `tests/unit/__init__.py` and `tests/unit/agent_runner/__init__.py`: promote the agent-runner unit test directory to a package so pytest can disambiguate `tests/test_transcripts.py` from `tests/unit/agent_runner/test_transcripts.py`.
- `docs/BATMAN.md`: replaced operator-specific channel literal with `#your-fleet-channel` placeholder per the private-to-public boundary policy.

### Verification

- 689 tests pass on Python 3.11.
- `bash bin/scrub-check.sh` returns `scrub-check: clean`.
- `cd site && npm run build` builds 45 pages with 0 errors and 0 content warnings.

## [0.3.0] - 2026-05-21

### Added

- `--dry-run` / `ALFRED_DRY_RUN` mode: run a full agent firing lifecycle (pick, claim, worktree, invoke, act, release, report) with every side-effecting boundary stubbed. No LLM call, no spend, no Slack post, no GitHub or git mutation. Works with zero host config so a developer can watch an agent fire end-to-end before configuring anything. Threaded through `lib/agent_runner.py` behind a single `is_dry_run()` seam; supported by `examples/bin/hello.py`, `examples/bin/echo_summarise.py`, and `bin/lucius.py`. See `docs/DRY_RUN.md`.
- Linux support via `systemd --user` timers. `install.sh` now has a Debian/Ubuntu apt lane alongside the macOS Homebrew lane, `deploy.sh` renders and installs systemd units on Linux hosts, and a new `systemd/` directory holds `_template.service`, `_template.timer`, and `render.sh` (same `agents.conf` schema as the launchd renderer).
- `alfred pause` / `alfred resume` / `alfred run` operator verbs, backed by a host-scheduler abstraction (`lib/scheduler.py`) that drives launchd on macOS and `systemd --user` on Linux.
- `alfred agents` now shows a real scheduler-load column (launchd or systemd), distinct from the configured on/off column.
- `bin/doctor.sh --dev` flag: dev-install mode treats host-config preflight gaps as non-fatal while still failing hard on code defects. `install.sh` passes `--dev` on Linux.
- Publishing guide for GitHub Pages workflow mode, release-site verification, and optional custom-domain setup.
- `alfred claude probe` for a first-class Claude Code auth smoke test.
- `alfred codex status/probe` and `alfred auth status/probe` for first-class
  Codex CLI and combined provider-auth diagnostics.
- `alfred-init.py --repos`, `--slack-webhook`, and `--skip-label-setup` for AI-driven setup against one repo without guessing through the interactive wizard.
- Batman is now visible in the `alfred-init` catalog as an opt-in, plan-only cross-repo coordinator.
- `docs/CODEX_PROVIDER.md` for Codex engine modes, runtime contract, and billing posture.

### Changed

- `alfred-init.py` now defaults to the recommended starter fleet (Drake, Lucius, Ras al Ghul, agent-cleanup) instead of enabling every discovered agent on Enter or in non-interactive mode.
- `alfred-init.py` seeds prompt templates into `~/.alfred/prompts/<codename>.md`, creates standard GitHub labels on selected repos, and refuses multi-repo non-interactive setup unless `--repos` is explicit.
- Robin is correctly described and wired as bug triage in the installer catalog.
- `alfred-status` and `bin/doctor.sh` now read the `systemd --user` timer roster on Linux, falling back to the same agent-discovery logic the launchd path uses.
- `docs/LINUX.md` rewritten: Linux is now a supported host, not a set of interim workarounds.
- Documentation now consistently distinguishes host scheduling, Claude account
  routing, and Claude/Codex engine routing.
- Refreshed README, roadmap, docs site status, and release checklist for the public docs launch.
- Switched the public docs URL to `https://alfred.luminik.io/` and made docs-site links root-relative for the custom domain.
- Moved Claude account routing fully into `alfred claude`; the standalone helper is no longer shipped.
- Standardized the public runtime root on `ALFRED_HOME` / `~/.alfred` across code, examples, tests, docs, and the docs site.

### Fixed

- Batman bundle scans now stay inside the selected repository scope instead of broadening across every configured repo.
- `alfred auth status` now returns nonzero when the Codex CLI status path fails, so scheduled-agent preflight catches missing Codex installs.

## [0.2.1] - 2026-05-12

Public launch hardening release.

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
- **Runner-level fleet gate file.** New `$ALFRED_HOME/state/fleet/enabled.txt` plus `is_agent_enabled` / `enable_agent` / `disable_agent` helpers. Listed codenames are enabled; missing codenames fall back to each runner's default so opt-in agents can be gated without making normal launchd agents look disabled. New `bin/alfred` CLI ships `alfred enable / disable / agents / enabled-agents`.
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
- **code-map-refresh**: cross-repo contract scan. Writes `${ALFRED_HOME}/state/code-map.json` for other agents.
- **agent-morning-brief**: daily Slack post covering yesterday's PRs, in-flight work, doctor status.
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
- Doctor mode runs before env-config IDLE checks across all 12 agents: `bash bin/doctor.sh` now reports all-passing on a fresh install before the operator runs `alfred-init`.
- `bin/doctor.sh` now falls back to the in-repo `bin/` and `lib/` paths before deploy, so a clean checkout can self-check without a pre-existing `$ALFRED_HOME`.
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
- **Real per-firing JSONL transcripts**: `claude_invoke_streaming` currently delegates to `claude_invoke`. The streaming impl with transcript file at `${ALFRED_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl` ships with the future transcript-viewer command.

## [0.1.0] - 2026-05-02

Initial public framework extraction.

### Added

- `lib/agent_runner.py`: preflight, lock, spend, claude_invoke, gh, slack, event-log, commit-trailer, handoff-table primitives.
- `bin/doctor.sh`: host validator (preflight every agent under `ALFRED_DOCTOR=1`).
- `alfred claude`: account-routing helper for two Claude accounts.
- `launchd/_template.plist` + `launchd/render.sh` + `launchd/agents.conf.example`: plist generation.
- `deploy.sh`: copy lib + bin into `$ALFRED_HOME`, render plists, bootstrap launchd.
- `examples/bin/hello.py`: minimal codename-agent reference.
- `tests/test_agent_runner.py`: 22 cases covering preflight, doctor_mode, load_prompt, commit_trailer, HandoffTable, EventLog, _full_repo.
- Top-level docs: `README.md`, `ARCHITECTURE.md`, `BOOTSTRAP.md`, `CONTRIBUTING.md`, `LICENSE` (MIT), `docs/INDEX.md`.

[Next]: https://github.com/luminik-io/alfred-os/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/luminik-io/alfred-os/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/luminik-io/alfred-os/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/luminik-io/alfred-os/compare/0c5d13c673f5954014cb5b5ccf3dc880c9563641...v0.2.0
[0.1.0]: https://github.com/luminik-io/alfred-os/pull/2
