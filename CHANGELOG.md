# Changelog

Notable changes to Alfred. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Long Ask conversations no longer run out of room. When a chat grows past a configurable length, Alfred keeps the opening task and the most recent turns intact and replaces the middle with one compact, model-written summary, so the original goal and the live working state both survive. If a turn still overflows the model's context, Alfred condenses and retries once instead of failing. Every condensation is written down as an auditable record, short chats are left untouched, and every threshold is environment-overridable.

### Changed

- Alfred Desktop now uses the exact local runtime URL the operator chooses instead of silently retrying or rewriting stale localhost ports. The setup console also shows the current `alfred brain doctor --json` command.
- Batman now marks and closes a fully fanned-out parent `agent:large-feature` issue after every child issue is filed, preventing the same approved bundle from being picked up and duplicated on a later firing.

### Removed

- Removed obsolete Ask history migration code and the stale native memory-check fallback. Ask history is now v2-only, and the memory check runs the current doctor command directly.

## [0.5.3] - 2026-06-24

The signed, notarized macOS desktop app and Linux packages are published on this
release, and `brew install --cask alfred-os` installs the desktop app while
`brew install alfred-os` installs the CLI. This cycle also makes the desktop
surfaces honest and conversational, adds a self-healing reliability core,
strengthens the memory layer, and hardens both install paths.

### Highlights

- Ask is now a real conversation. A plain question like "Who are you?" gets a real answer instead of a saved plan with a "File issue" button. Alfred reads each message, answers directly when you are just talking, and only opens the plan-and-issue flow when you are describing work to build.
- The workflow canvas is the main surface. It now spans the full width, lays the architect to implement to review to ship pipeline out automatically, and opens each agent's detail in a slide-over drawer you can dismiss, so the canvas behind it stays visible.
- Activity tells the real story of a run. Each run opens on a single-line headline (what it did, the key result) and expands to the full step timeline. An "Errors only" switch filters to failures, and a failure shows its real cause rather than a misleading provider message.
- The fleet heals itself instead of dying on the first hiccup. A transient error now retries the same engine with backoff, a real authentication or budget failure is surfaced honestly and never retried, and the fallback to a second engine only fires when an engine genuinely could not do the work.
- Alfred keeps the most useful lessons on its own. A second model reads each candidate lesson, and when it is confident the lesson is sound and worth keeping, the lesson is saved into recall without waiting for you. Anything it is unsure about still waits in the review queue, and every automatic save can be undone.
- A full disk no longer wedges the fleet. When space runs critically low, the emergency cleanup now also clears regenerable build and download caches across the machine, so the agents can keep working instead of getting stuck.

### Added

- Conversational Ask. The desktop Ask surface classifies each message as plain conversation or a build request. Plain conversation gets a direct answer and leaves your draft untouched; a build request co-authors the structured spec as before. The planning capability is kept, offered conversationally rather than forced on every message.
- Self-healing reliability core. A single source of truth classifies each failure into transient, fatal, or capability, then retries the same engine with backoff on transient errors, surfaces fatal errors honestly without retrying, and only falls back to a second engine on a genuine capability gap. It also detects repeated identical attempts so a run cannot loop forever. Existing behaviour is unchanged when nothing is failing.
- Code-memory layer over MCP. Alfred can attach an external code-structure memory server to each Claude run, giving agents code search, call-graph, blast-radius, and ownership lookups while planning a change. It is on by default, opts out with `ALFRED_CODE_MEMORY_MCP=0`, runs as a standalone external binary that is never vendored, and degrades to a clean no-op when the binary is not installed.
- Autonomous lesson capture. When a second model is confident a real, evidence-backed lesson is sound and useful, Alfred now saves it into recall on its own. It stays off until you turn it on, holds anything it is unsure about, and every automatic save is reversible.
- Homebrew cask for the desktop app. `brew install --cask alfred-os` installs the signed, notarized macOS app and pulls in the CLI formula it talks to over `alfred serve`. The CLI alone is still `brew install alfred-os`.

### Changed

- The workflow canvas was rebuilt to be the primary surface: full-width, automatic left-to-right DAG layout, fit-to-view and zoom controls, a status-colored minimap, and richer status-colored node cards. The agent detail is now a dismissible slide-over drawer built on the existing Sheet primitive, so it never permanently eats canvas space.
- The Activity run view now defaults to a one-line headline and expands to the full per-run step timeline already captured by the fleet. Idle runs stay quiet, failures are loud, and the failure cause is classified from the real outcome rather than the downstream fallback message, so an authentication failure reads as authentication, not a misleading rate limit.
- The first-run setup wizard tells the truth. The progress stepper is anchored to the furthest step you have actually reached, so a machine with everything pre-detected opens on Welcome at "0 of 6 done" and climbs honestly as you advance. The value is stated once per step instead of three times, and the no-API-keys trust point is stated up front.
- Scheduled runs read auth from one place. The sign-in token and runtime config now live in a single store (`$ALFRED_HOME/.env`), the scheduler loads it, and an early auth preflight surfaces a real authentication failure as authentication. This closes the silent-auth-failure class where a token written to the wrong file caused every run to fall back and report a misleading rate limit.
- The confidence reviewer is now the main gate for saving a lesson automatically. Any lesson with real evidence behind it reaches the reviewer, which decides whether to keep it. When the reviewer is turned off, Alfred falls back to a stricter, conservative rule so it still keeps very few lessons by hand.
- The local memory server is now a plain store for lessons. The small on-device model no longer rewrites, merges, or reshapes saved lessons, so the memory Alfred recalls stays true to what was written. The smarts that decide what to keep live in Alfred, not in the store.
- The emergency disk cleanup now reclaims regenerable build and download caches across the machine, not just Alfred's own leftovers, so a full disk no longer stalls the fleet off work.
- The Homebrew CLI formula now points at the current `v0.5.3` source tarball.
- Alfred standardizes on a single home-directory setting. The runtime reads `ALFRED_HOME` only across the runtime, the desktop client, and the launchers. Existing setups already export `ALFRED_HOME`, so nothing changes for them.
- The memory documentation now matches how Alfred really works: the local memory server as the main place lessons are recalled from, the confidence reviewer deciding what gets saved, and the server kept as a plain lesson store.

### Removed

- Dropped the optional companion-tool integration guide and its mentions from the docs and site. Alfred core runs standalone, and the generic control-gateway guidance in `docs/INTEGRATIONS.md` covers wrapping the fleet with any companion tool.

### Fixed

- Planning notes no longer clutter the lessons queue. Draft plans and specs are kept out of the lesson candidates, so what you review is real lessons from real runs.
- The reviewer's instructions now match what it actually does. Its prompt and notes describe confidence as the safety check and treat behaviour change as a plain label, so the reviewer and the code agree on when a lesson is saved.

## [0.5.2] - 2026-06-22

### Highlights

- Alfred's native app now ships with the refreshed dark interface from the current public source. Inbox, Ask, Work, Agents, and Setup use the same design in the internal and OSS repos.
- Redis Agent Memory is now the default local memory layer, with FleetBrain kept as the review and reliability layer. Docs and install copy now match that runtime path.
- Slack planning threads are easier to use: the first message needs the Alfred mention, and follow-up replies in the thread keep the conversation attached to the same plan.
- The public site now has fresher impact proof, cleaner mobile hero layouts, and launch copy that explains Alfred's real value: coding agents keep work moving from Slack, GitHub, or a rough plan while you are away.

### Added

- Repo graph support in the code-map flow, giving Alfred a stronger base for understanding codebase relationships before planning work.
- Visual QA tooling for the desktop client, including screenshot parity checks and pixel-sweep hardening for local desktop routes.

### Changed

- Proof telemetry now reports anonymous aggregate counts to the hosted collector by default unless disabled with `ALFRED_TELEMETRY_ENABLED=0`; the Worker remains self-hostable for forks and private deployments.
- Impact proof seed data was refreshed from current GitHub activity so the site has useful fallback numbers even before the live counter loads.
- Docs, README, and site copy now use “Alfred” consistently and keep Batman-first agent hierarchy language aligned with the product.
- Install and integration docs now describe Redis Agent Memory and FleetBrain as bundled local components rather than optional external services.
- Homepage, install, impact, and download hero layouts now avoid cramped CTA grids and narrow-screen word wrapping.

### Fixed

- Workflow validation now resolves `actionlint` from common local install paths, so scheduled agents do not fail when launchd has a narrower PATH.
- Hosted telemetry counters now handle missing or stale live data more defensively.
- Desktop visual QA now uses isolated browser contexts, a validated route timeout, and the configured timeout for navigation.

## [0.5.1] - 2026-06-17

### Highlights

- Alfred's signed desktop app is now easy to find from the public site. The new download page links to stable latest-release asset names for macOS and Linux, and the homepage points users there directly.
- The native client and `alfred serve` now agree on port 7010 by default, avoiding macOS Control Center's use of port 7000 while still migrating stale saved 7000 URLs safely.
- The public docs and site now match the shipped native-client status: signed and notarized macOS artifacts, Linux AppImage/deb artifacts, and current Inbox / Ask / Work / Agents / Setup navigation.

### Added

- Public `/download/` page with signed desktop artifact links for `Alfred.dmg`, `Alfred.app.zip`, `Alfred.AppImage`, and `Alfred.deb`, plus homepage download calls to action.
- Proof telemetry plumbing for public aggregate install/use counters, including a self-hostable Worker and an environment switch to disable reporting.

### Changed

- `alfred serve` now defaults to `127.0.0.1:7010`; the desktop app no longer probes the legacy 7000 endpoint after a 7010 failure.
- Saved localhost URLs on port 7000 are treated as stale local config and normalized to 7010 before browser or Tauri requests.
- The desktop status pill and tooltip now use the human label "Needs attention" for non-ok fleet reliability states instead of surfacing raw enum text.
- README, install-tier docs, native-client docs, desktop-client docs, release docs, and the docs site were refreshed for the signed desktop package, stable latest asset names, and the current five-tab app IA.

### Fixed

- Cleared high-severity frontend audit findings by updating affected transitive packages across the desktop and site lockfiles.
- Silenced `astro-mermaid` browser console noise on pages without diagrams.

## [0.5.0] - 2026-06-15

### Highlights

- Alfred now has a signed native desktop app for Mac and Linux. Download it, open it, and you get a real window into your agents (Inbox, Ask, Work, Agents, and Setup) instead of a browser tab. (You can still run everything from the command line and Slack.)
- The app shows your live Claude and Codex subscription usage. It reads the usage left in your rolling 5-hour and weekly limit windows straight from each tool's own local state, so there are no surprise pay-per-token API bills and nothing made up: a window it cannot confirm reads "not synced" rather than a fake number.
- Single-repo work now waits for your go-ahead. When Alfred plans a change to one repo, the issue is held with an approval label and nobody picks it up until you approve it, the same human gate that already protected multi-repo work.
- A disk guardian keeps your agents from crash-looping when the disk fills up. If free space drops below a safe floor, Alfred cleans up its own leftover files first, and if space is still tight it skips that run cleanly (no crash) and sends you one quiet heads-up.
- An approved Slack plan can become real work in one step. In a planning thread, a trusted teammate's explicit approval turns the draft into a labeled GitHub issue your agents then pick up. It is off by default, needs both a trusted person and a clear approval word, and only files an issue: it never runs code on its own.
- Your agents remember what they learn, with you in the loop. Lessons from real runs are saved as review-first suggestions you can approve or reject from Slack or the app before they shape future work, plus an optional connection to a Redis memory server if you already run one.
- Smaller setup, fewer snags: one command sets up a long-lived sign-in token so scheduled agents stay logged in, and the website gained search-engine basics and privacy-respecting analytics that stay off until a visitor agrees.

### Added

- First native Mac and Linux desktop app (`clients/desktop`, built with Tauri). It wraps the local `alfred serve` JSON APIs with Inbox, Ask, Work, Agents, and Setup surfaces; uses the Alfred brand fonts and logo; keeps local plan and firing detail in native inspector panes; opens explicit Slack/GitHub links outside the app; and restricts native API reads to localhost Alfred endpoints. Builds native installers (`.app`/`.dmg`, `.AppImage`/`.deb`) from the Tauri bundle config. The Inbox decision queue groups repeated reliability signals, planning drafts, and memory candidates into action queues; the primary nav adapts between full labels, icons, and a second-row tab strip so smaller windows do not require horizontal scrolling. Setup has a local server URL field for custom ports, and upgraded installs pinned to the legacy `7000` default now probe the preferred `7010` runtime automatically. Documented in `docs/DESKTOP_CLIENT.md`.
- Live Claude + Codex usage in the native app. A local-state usage reader (`lib/server/usage.py`, exposed at `GET /api/usage` and `GET /api/usage/providers` and via the `alfred usage` CLI / `bin/alfred-usage.py`) reports subscription usage left from the Claude Code and Codex CLI state on the host, not the API list-price of tokens (which is meaningless under a Max/Pro subscription). The desktop Home rail renders the current Claude and Codex usage inline; a window the local state cannot confirm reads "not synced" rather than a fabricated number.
- Cinematic agent roster (default) with a dense-list toggle. The Agents view opens on a themed card deck: one card per agent carrying its accent, status, cadence, runs-today, latest signal, and a monogram identity mark. A Cinematic / List toggle (persisted to `localStorage`) keeps the compact rail one click away. Hover lift and entrance motion respect `prefers-reduced-motion`, and the cards are real buttons so screen readers announce them as actionable controls.
- Step-level run events. `lib/agent_runner/agent_events.py` emits real lifecycle steps (`repo_picked`, `plan_created`, `pre_push_checks_passed`, `branch_pushed`, and more) from the matching points in `lucius.py` / `batman.py`, each with an operator-visible detail string, so the client timeline tells the run's story instead of only its start and stop. Progress is never fabricated.
- Single-repo operator-approval gate for planned issues. A planned single-repo issue now lands with `agent:plan-pending-approval` and is held from autonomous pickup until the operator approves it. `lib/issue_queue.py` releases the gate on approval (resiliently, even when that label was never created in the repo) and `lib/issue_assignment.py` enforces it, so even single-repo work waits for a human go-ahead.
- Slack approved-draft-to-issue bridge (`lib/slack_issue_bridge.py` + wiring in `lib/slack_listener.py`): turns an *approved* Slack planning draft into a labeled GitHub issue your agents pick up. In a registered draft thread, a trusted user replying with an explicit approval token (default `ship it` / `create issue` / `go` / `/ship`, configurable via `ALFRED_BRIDGE_APPROVAL_PHRASES`) or reacting with `:white_check_mark:` / `:rocket:` files one issue via `gh issue create` carrying the pickup label (`ALFRED_BRIDGE_LABEL`, default `agent:implement`). Two independent conditions are both required (a trusted user *and* an explicit approval token), and a non-trusted user can never trigger it. The target repo is validated against an allowlist (`ALFRED_BRIDGE_REPOS`); a repo outside the list is refused. Conversion is idempotent (a draft converts once; a second approval reports the existing issue), and the created issue link is posted back in the thread. Off by default (`ALFRED_BRIDGE_ENABLED`). CRITICAL: the bridge only creates a labeled issue. It never runs code, opens worktrees, or spawns an agent. The existing autonomous agents (Lucius/Batman) then claim the issue through every existing gate (claim-lock, spend caps, review, Batman's multi-repo approval), reusing the safety machinery instead of bypassing it. Covered by `tests/test_slack_issue_bridge.py` (trusted+explicit yields an issue with correct repo+label; non-trusted yields nothing; ambiguous yields refine only; repo not in allowlist is refused; double-approval yields a single issue; reaction approval; disabled-by-default). Operator setup and the safety model are documented in `docs/SLACK_SETUP.md`.
- Slack planning listener: optional Socket Mode listener for trusted DMs, app mentions, and registered Alfred plan/report threads. It stores local planning drafts under `$ALFRED_HOME/state/planning-drafts/`, records feedback under `$ALFRED_HOME/state/slack-threads/feedback/`, ignores events when no trusted users are configured, and keeps reaction approval as the only execution gate.
- Slack follow-up capture primitives: trusted replies after Batman reports or PR links are classified as `change`, `fix`, `test`, `question`, `scope`, or notes, rendered as Slack acknowledgements, and available as Markdown context for the next plan or PR pass without granting merge approval.
- Disk guardian (ENOSPC back-off). `agent_runner.disk_pressure_status()` probes free space on the filesystem holding `ALFRED_HOME` against env-tunable floors (`ALFRED_MIN_FREE_DISK_GB`, default 3.0; `ALFRED_MIN_FREE_DISK_PCT`, default 5.0) and reports `critical` / `low`. `preflight()` now gates on it: a `critical` reading fires `agent-cleanup.py --emergency` once to reclaim Alfred-owned space, re-probes, and if still critical raises `PreflightFailed` so the agent SKIPS the run cleanly (exit 0, never crashes) and posts one throttled Slack warning (`ALFRED_DISK_SLACK_MIN_HOURS`, default 6h). This is what stops the agents crash-looping on a full disk. `PreflightSpec` gains `min_free_disk_gb` / `min_free_disk_pct` / `check_disk` fields so every agent inherits the guard with safe defaults; the cleanup agent sets `check_disk=False` so it runs *despite* low disk. `fleet-doctor` gains a `disk-pressure` check (green/yellow/red) so `alfred doctor` surfaces the same status. Probe fails open on stat errors so a transient hiccup can never wedge the agents into a permanent skip.
- `agent-cleanup.py --emergency`: aggressive reclamation for disk-pressure recovery. Lowers the abandoned-worktree age gate (2h to 15min fleet pool, 48h to 1h extra pools), shortens transcript/event retention to an emergency floor (default 3 days, clamped down only), and clears Alfred's own `/tmp` debug dirs regardless of the 1-day gate. All reclamation stays 100% Alfred-owned with the identical dirty-skip + recovery-ref safety as a normal sweep; spend ledgers are never shortened.
- `agent-cleanup.py` auto-discovers `.worktrees` pools under `WORKSPACE` (bounded depth at most 3, skipping `node_modules`/`.git`) and sweeps them with the same dirty-skip rules as the fleet pool. Closes the incident class where manual Claude Code sessions left per-project `product/<repo>/.worktrees` pools full of `node_modules` (~20GB) that the old `ALFRED_CLEANUP_EXTRA_PATHS` list only swept if set by hand. Opt out with `ALFRED_CLEANUP_AUTODISCOVER=0`.
- fleet-brain reliability tools: reviewable memory candidates (`alfred brain propose/candidates/promote/reject`), normalized failure-event history (`alfred brain failures`), read-only health checks (`alfred brain doctor`), `alfred brain harvest` for repeated-failure lesson candidates, and a read-only JSON-RPC stdio bridge (`alfred mcp serve`) that exposes allowlisted memory summaries to local MCP clients.
- Optional Redis Agent Memory Server provider: operators who already run Redis AMS can set `ALFRED_MEMORY_PROVIDERS=fleet,redis` plus `ALFRED_REDIS_MEMORY_URL` to consult it as a fallback memory source without adding a default dependency. `alfred brain redis-status` checks the server and `alfred brain redis-sync` mirrors reviewed local lessons into Redis explicitly.
- Slack memory curation: trusted users can run `memory`, `memory harvest`, `remember [repo:] <lesson>`, and `memory redis` from Slack to inspect review queues and stage candidates. Operator-only `memory promote <id>` / `memory reject <id>` decide what enters future recall, `memory harvest now` queues repeated-failure candidates, and `memory sync` previews Redis AMS sync before the explicit `memory sync now` write path.
- Scheduled memory harvest: `bin/memory-harvest.py` queues reviewable repeated-failure candidates from `alfred brain harvest --apply --json`, posts to Slack only when candidates are queued or the run fails, and never promotes lessons or syncs Redis by itself.
- Runtime memory reflection now defaults to `ALFRED_MEMORY_REFLECTION_MODE=candidate`, so engine-written memories queue for review before they can affect future recall. Operators who intentionally want direct lesson writes can set `ALFRED_MEMORY_REFLECTION_MODE=direct`.
- `alfred spec`: template, lint, and readiness helpers for specs-driven development. `alfred spec new` writes a repo-scoped Markdown template; `alfred spec lint` checks for acceptance criteria, test plan, non-goals, rollout, repo scope, and open questions; `alfred spec assess` turns a structured issue draft into a readiness verdict and GitHub-ready issue body.
- `alfred serve` Planning tab: local issue/spec intake for operators and teammates. It scores drafts, asks concrete scope questions, renders a GitHub-ready issue body, recalls advisory planning memory from promoted lessons, embeds prompt-safe hints in saved specs, and queues reviewable spec-to-issue memory candidates without creating GitHub issues.
- `alfred setup-token` (`bin/alfred-setup-token.py`): one-command bootstrap of the long-lived OAuth token. Detects whether `CLAUDE_CODE_OAUTH_TOKEN` is already set in the env or `~/.alfredrc`; if not, spawns `claude setup-token` interactively, parses the printed token, writes a single `export` line to `~/.alfredrc`, and tightens the file to 0600. Re-runs (`--force`) replace the existing block in place so rotation is idempotent. `--check-only` reports status without touching auth. `alfred-init` step 1 now offers to run this automatically when the token is missing.
- `alfred setup-token --token <value>`: paste-back path that skips the Ink-based `claude setup-token` spawn and writes the supplied token straight to `~/.alfredrc` with the same shape validation, shlex quoting, idempotent block, and 0600 perms as the interactive path. Unblocks AI-assisted installs (Claude Code, Codex, automation) that can't drive a TUI: operator runs `claude setup-token` in their own terminal, copies the printed token, and the assistant runs `alfred setup-token --token <value>` to persist it. The default (no `--token`) path now also detects non-TTY stdin up front and exits with a clean three-path message instead of surfacing Ink's `Raw mode is not supported` stack trace. `docs/AI_ASSISTED_INSTALL.md` gains an *OAuth Token Setup Needs a Real Terminal* section. Closes #110.
- `CLAUDE_CODE_OAUTH_TOKEN` is the supported way to authenticate `claude` from launchd / systemd contexts. Run `claude setup-token` once (or `alfred setup-token` for the automated path) to mint a 1-year subscription token, export the value in `~/.alfredrc`, and `claude` reads it directly without touching the macOS Keychain or filesystem credential cache. See `docs/CLAUDE_CODE.md`.
- `$ALFRED_HOME/venv` Python interpreter for scheduled agents. `install.sh` now provisions `$ALFRED_HOME/venv` via `uv venv --python 3.11` and installs the base deps (`slack-sdk>=3.27`, `boto3>=1.34`) into it; `bin/agent-launch` prefers `$ALFRED_HOME/venv/bin/python` for *Python* targets (`.py` extension or a `python` shebang) and otherwise execs the target through its own shebang so shell-script agents like `fleet-recap.sh` keep working. `ALFRED_PYTHON` env override takes precedence for operators who want a different Python interpreter. `bin/doctor.sh` asserts `import slack_sdk, boto3` against the venv interpreter so a venv-installed-but-broken state surfaces at preflight, not mid-run. `install.sh --skip-python-venv` / `ALFRED_SKIP_PYTHON_VENV=1` opt out for hosts that already provision deps another way; venv provisioning is independent of `--skip-brew` so brew-skipping operators still get base Python deps. Regression tests under `tests/test_agent_launch_interpreter.py` lock the shell-vs-Python target detection. Closes #96.
- `ALFRED_FLEET_OVERLAY` hook in `agent_runner/__init__.py`: imports an operator-supplied module (default name `fleet_overlay`) at end of package init so a fleet can populate `GH_REPO_TO_LOCAL`, `STANDARD_LABELS`, and `HANDOFFS` from one place instead of forking every `bin/*.py`. Silently absent when no overlay is on the path.
- `alfred-init --config` learned `role_repos`, `role_codename`, and `role_schedule`. Each maps an agent (by codename or role-key, case-insensitive) to a per-agent override: which repos that codename operates on, what codename to expose, and what schedule string to write into `agents.conf`. Previously non-interactive mode forced every repo-operating agent to claim every visible repo, which is wrong any time the operator wants codenames scoped to different surfaces (e.g. test-coverage skipping iOS, code-map-refresh only on JS/TS repos). `step_6_codenames` / `step_7_repos` / `step_8_schedule` preserve preset values from `--config` instead of overwriting them. Unknown agent keys, codenames that don't match `^[a-z][a-z0-9-]*$`, and malformed value types are surfaced as `warn` and skipped rather than silently dropped. Cross-platform: pure stdlib, no shell tricks, same behaviour on launchd and `systemd --user` consumers.
- `WORKSPACE_SUBDIR` env var lets operators name (or remove) the segment between `$WORKSPACE_ROOT` and `<repo>`. Default stays `product` for back-compat with `~/code/product/<repo>`; `WORKSPACE_SUBDIR=src` resolves to `$WORKSPACE_ROOT/src/<repo>`; `WORKSPACE_SUBDIR=""` collapses to `$WORKSPACE_ROOT/<repo>` directly. Unblocks operators whose existing layout is `~/repos/<repo>`, `~/work area/<repo>`, or similar without symlinking around the previously-hardcoded `product/`. Documented in `docs/WORKSPACE_PATTERNS.md` and `.alfredrc.example`.
- `docs/GOALS.md`: durable goal contract for larger Alfred work. Goals capture outcome, verification, constraints, human gates, blocked conditions, evaluator evidence, and memory hooks across Slack, CLI, the native app, and engines. Engine-native goal modes can be used as execution hints, but Alfred keeps the Slack thread, operator gates, and evidence ledger as the source of truth.
- `docs/BATMAN_PARENT_ISSUE_TEMPLATE.md`: validated minimal body format for `agent:large-feature` parent issues that the lifecycle parser (`parse_parent_issue`) accepts. Documents the four required sections (`Bundle:` / `Repos:` / `Children:` / `Done when:`), the hard requirement that `Repos:` entries be full `owner/repo` slugs (per #116), the ~50-char bundle-label length limit (per #117), a worked example validated end-to-end on a 3-repo fleet, and a copy-paste Python parser-validation snippet so operators can verify the body shape before filing the GitHub issue. Catches the silent `children=0` failure class at draft time instead of after a wasted Slack approval cycle (per #107).
- Nightwing `[NIGHTWING-NO-COMMIT]` diagnostic upgrade. The log line now includes `git status --porcelain` from the worktree and a pointer to the per-run transcript path under `${ALFRED_HOME}/state/transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl`, so the operator can tell *which* of the five no-commit failure modes happened (engine described the fix in prose without invoking a write tool / engine wrote files but didn't commit / pre-commit hook rejected / wrong branch / files written outside the worktree) without grepping the run log themselves.
- Nightwing `(PR, comment)` no-commit-streak escalation. On the same `(PR, comment_id)` tuple, after `ALFRED_NIGHTWING_ESCALATE_AFTER` (default 3) consecutive no-commits Nightwing posts a Slack alert and adds the `nightwing:human-needed` label to the PR, then stops retrying that comment. Operators clear the streak by adding `nightwing:reset` to the PR or by deleting the entry from `${ALFRED_HOME}/state/nightwing/no-commit-streaks.json`. Streak state survives daemon restarts; a successful fix on a comment drops its streak entry. Regression tests in `tests/test_nightwing_no_commit.py`. Closes #109.
- `site/`: `@astrojs/sitemap` integration emits `/sitemap-index.xml` + `/sitemap-0.xml` at build covering all 43 marketing + Starlight pages. `public/robots.txt` already points at the index so search engines and AI crawlers pick it up without autodiscovery.
- `site/`: Google Analytics 4 (`G-Y157X0YLN4`) loader wired into both the Starlight docs (`astro.config.mjs` head) and the marketing layout (`src/layouts/MarketingLayout.astro`), behind Google Consent Mode v2 (`analytics_storage` defaults to `denied`). `PUBLIC_ALFRED_GA4_ID` overrides the property for forks and staging.
- `site/`: cookie-consent banner (`.alfred-cookie-banner`) shipped with both layouts. First-visit dialog with Reject / Accept; Accept flips Consent Mode `analytics_storage` to `granted` and persists `alfred-cookie-consent=allow` in `localStorage`. Reject persists `deny` and leaves consent denied. Styles in `site/src/styles/custom.css`.
- Docs + site coverage for the latest shipped surfaces: new validated mermaid diagrams in `docs/ARCHITECTURE.md` (full Slack conversational flow, desktop control plane, install and distribution), a new `docs/DESKTOP_CLIENT.md`, control-command and thread-sync sections in `docs/SLACK_SETUP.md`, new Astro concept pages for plain mode and the desktop client, and refreshed README + `docs/INDEX.md` pointers.

### Changed

- `lib/batman.py` `parse_parent_issue` (the lifecycle plan parser, used by `BatmanLifecycle.plan()` from `BATMAN_PARENT_REPO`-set fleets) now emits a single warning under the `alfred.batman.lifecycle` logger when both the canonical `Repos:` / `Children:` blocks AND the loose `## Affected Repos` / `## Acceptance Criteria` H2 markers come up empty. Operators see it in `/tmp/alfred.batman.stderr` on the first failed run instead of after wasted cycles. When the loose `## Affected Repos` / `## Acceptance Criteria` shape IS present but the canonical blocks are not, the parser auto-falls-back to `parse_plan_from_issue`, synthesizes one child per affected repo (title `<repo>: implement <slug>`), and uses the per-repo acceptance criteria as the done-when summary. Gated on explicit H2 markers so a truly-empty body still hits the EXEC_NO_CHILDREN warning rather than picking up the default rollout-order from `parse_plan_from_issue`. `docs/BATMAN.md` gains a *Parent issue body template* subsection documenting both shapes side-by-side. Closes #107.
- `lib/batman.py` `_parse_repo_lines` (the lifecycle parser's `Repos:` block reader) now qualifies bare repo names with `GH_ORG` when set, instead of silently dropping them. Operators' natural shorthand (`palette`, `palette-web`) just works on single-org fleets. Without `GH_ORG`, bare names get a `BATMAN-PARSE-WARN` per line on stderr so the cause is visible on the first failed run rather than after a wasted Slack approval cycle. Closes #116.
- `lib/batman.py` `SubprocessGitHubChildIssueClient.create_issue` now opportunistically creates per-bundle labels (`agent:bundle:<slug>`, purple `5319e7` to match `batman-pr-open`) on each target repo before invoking `gh issue create`, mirroring the `gh_pr_create` pattern. Without this, the first cross-repo Batman execute failed with `could not add label: 'agent:bundle:<slug>' not found` for every child issue and the operator was left with an approved plan and zero filed children. Label creation is best-effort: a `gh label create` failure (rate limit, transient network) does not block issue creation. Closes #117.
- Batman's lifecycle path is now **idempotent across runs while a plan awaits approval**. The runner sets `agent:plan-pending-approval` on the parent issue after posting the first plan, persists the Slack `(channel_id, message_ts)` to `${ALFRED_HOME}/state/batman/pending-approvals/<owner>__<repo>__<num>.json`, and on subsequent runs re-uses the existing message (resume polling) instead of drafting + posting a fresh plan. Approval / rejection / transport-down clear the label and the state file; a plain timeout keeps both so the next run resumes the same poll. `ALFRED_BATMAN_APPROVAL_MAX_AGE_HOURS` (default 24h) drops aged-out state so an abandoned plan does not hold a parent issue hostage. Operators stop seeing one plan post per run in their fleet channel. Closes #115.
- Batman plan Slack messages now show a clearer title, parent issue link, readiness verdict, execution scope, child issue list, done-when checks, reply commands, and what Alfred will do after approval. Child filing is blocked when parsed child scopes are placeholders.
- Batman approval-thread repo commands now update execution scope before child issues or worktrees are created; `remove repo:` no longer leaves the removed repo in the run as a note-only amendment.
- `alfred serve` now has a cleaner local dashboard layout with Fleet / Firings / Plans / Planning tabs, mobile card rendering for tables, human-readable timestamps with raw UTC values in titles, and a saved Alfred plan inbox sourced from `$ALFRED_HOME/batman-plans`. Fleet opens with a strip for planning work, reviewing plans, triaging attention items, and inspecting recent firings so the local client answers "what needs attention?" before historical tables. The header stays sticky, tables wrap in responsive scroll/card shells across viewport sizes, and external issue/PR links open in a new tab.
- `preflight()` now consults `GH_REPO_TO_LOCAL` (with fallback to the slug) when resolving local checkout paths, so multi-repo workspaces with renames (`org/myorg-backend` checked out at `product/backend/`) stop reporting bogus "missing checkout" errors.
- The shipped-work digest no longer repeats the agent name when a badge already shows it (client `derive.ts`), and the Ask intake header uses a stable "New request" eyebrow instead of a mode-named label that duplicated the plain-language toggle.
- `Formula/alfred-os.rb` now points at the `v0.4.0` source tarball and checksum instead of the older `v0.3.0` release.

### Fixed

- `agent_runner/process.run()` TimeoutExpired path: Python 3.14 returns `bytes` for `e.stdout` even when `text=True` was passed to `subprocess.run`. Callers passing the result to `Path.write_text` (notably rasalghul caching `gh pr diff`) crashed with `TypeError: data must be str, not bytes`. The wrap site now decodes bytes to utf-8 with `errors="replace"`. Regression test patches `subprocess.run` to force the bytes case on every Python version.
- fleet-brain failure-pattern classification no longer includes the codename text when detecting setup/auth/provider/timeout causes, so a future codename like `playwright-runner` cannot turn unrelated provider failures into false local-setup blockers.
- `lib/agent_runner/github.py`: added `agent:implement` to `LIFECYCLE_LABELS` so `claim_issue`'s first-call `ensure_labels` creates the entry-point label alongside the in-flight / pr-open / done / sticky-modifier labels. `labels.py` already lists it in `LIFECYCLE_LABEL_SET`; the runner list was the odd one out. Test `test_label_constants_match_agent_runner_existing_values` updated to assert `IMPLEMENT in runner_names`. (`alfred-init.py` step_10_labels still handles the operator-facing bootstrap for wizard-configured repos; this change covers programmatic / non-wizard consumers.)
- `lib/agent_runner/github.py`: `ensure_labels` cache rewritten from `set[str]` (per repo) to `dict[str, set[str]]` (per repo + per label name). The old key meant a first call with `LIFECYCLE_LABELS` (e.g. from `claim_issue`) silently no-opped every later call with `STANDARD_LABELS` (e.g. from `gh_issue_edit` / `gh_pr_create`) on the same repo, leaving labels like `batman-pr-open`, `agent:large-feature`, `done-already` uncreated. Downstream `gh label add` then failed with "could not add label" and the runner surfaced "PR open failed" with no obvious cause. Two regression tests in `tests/test_gh_pr_create_labels.py` lock the per-label-name cache behaviour: a second call with a different catalogue creates the missing labels; a repeat call with the same catalogue is still a no-op.
- `lib/batman.py`: `Reporter.post_plan` now returns `(channel_id, message_ts)` instead of just `message_ts`. `BatmanLifecycle.request_approval` previously built `ApprovalEnvelope(channel=self.config.slack_channel, ...)` using the configured channel NAME. Downstream `slack_approval.SlackApproval.await_approval` passes that to `reactions.get`, which fails with `channel_not_found` on private channels and some bot-scope combinations, even when the channel name itself is correct (live repro: bot with `incoming-webhook, chat:write, reactions:read, channels:history`, channel name `alfred-palette`, private). The fix propagates the channel ID Slack's `chat.postMessage` echoes back into the envelope so `reactions.get` gets the ID it needs. `FakeReporter` in `tests/test_batman_execute.py` updated to return a `(id, ts)` tuple. Two regression tests pin the contract: `ApprovalEnvelope.channel` is the ID Slack resolved, not the name the operator configured.
- `bin/fleet-doctor.py`, `bin/alfred-status.py`, `bin/agent-morning-brief.py`: spend-file readers now derive the day key from UTC to match `SpendState`'s UTC writer (PR #99 follow-up). On non-UTC hosts during local/UTC date-skew windows, the local-time key would look for `spend-<local-day>.json` while writes were still landing on `spend-<utc-day>.json`, producing false "no spend today" output. All three readers now use `datetime.now(UTC).strftime("%Y-%m-%d")`.
- `site/src/layouts/MarketingLayout.astro`: `PUBLIC_ALFRED_GA4_ID=""` now disables GA4 entirely on marketing pages, matching the Starlight docs behaviour (PR #103 follow-up). The previous `||` fallback couldn't distinguish "unset" from "explicitly empty", so forks that wanted no analytics had to fork the layout. The fix uses `??` for the default fallback and conditionally renders the entire GA block so an empty string opts out cleanly.
- `bin/nightwing.py` `pick_target`: PRs carrying `nightwing:human-needed` (set by the no-commit-streak escalation in PR #111) are now skipped until the operator dual-labels with `nightwing:reset`. Without this, the escalation cleared the streak entry but the next run re-picked the same comment and burned turns retrying it. When both labels are present, the PR re-enters the pool and the inner reset handler clears both labels plus the streak state. New regression tests in `tests/test_nightwing_no_commit.py`.
- `.gitignore`: added `launchd/agents.conf` and `launchd/_generated/`. Both are per-operator artefacts (the conf names this host's fleet; the rendered plists hard-code `$ALFRED_HOME` paths). Without them gitignored, a fresh operator's first `git status` shows tracked-looking host-private files and `bin/scrub-check.sh` trips on the host `.alfred` paths inside the rendered plists.

### Removed

- `lib/claude_proxy/`, `bin/claude-proxy.py`, `tests/test_claude_proxy_*.py`, `docs/CLAUDE_PROXY.md`, `examples/launchd/luminik.claude-proxy.plist.example`. The proxy daemon shipped in v0.4.0 worked around a macOS Keychain ACL issue that `CLAUDE_CODE_OAUTH_TOKEN` resolves natively. `ALFRED_CLAUDE_PROXY_SOCKET` is no longer read; `claude_invoke_streaming` now always uses the direct subprocess path. Operators using the proxy should `launchctl bootout` it and unset the env var; otherwise no migration is needed.
- `bin/alfred-grant-keychain.sh` and `docs/MACOS_KEYCHAIN.md`. The targeted Keychain ACL grant is no longer the recommended workaround because the OAuth-token path bypasses Keychain entirely.

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
- Batman now executes the approved plan flow (`lib/batman.py` from 505 to 1383
  lines; `bin/batman.py` from 261 to 472 lines). Once a Damian-style plan is
  approved (Slack reaction, label transition, or `BATMAN_AUTO_EXECUTE=1`),
  Batman files the scoped child issues across the listed repos and reports
  status. Previously Batman halted at plan-only. The `BATMAN_AUTO_EXECUTE` env
  contract: `0` = always ask, `approval-gate` = read approval signals, `1` =
  always execute. See `docs/BATMAN.md`.

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

- `alfred-init.py` now defaults to the recommended starter fleet (Drake, Lucius, Ra's al Ghul, agent-cleanup) instead of enabling every discovered agent on Enter or in non-interactive mode.
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

[Unreleased]: https://github.com/luminik-io/alfred-os/compare/v0.5.3...HEAD
[0.5.3]: https://github.com/luminik-io/alfred-os/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/luminik-io/alfred-os/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/luminik-io/alfred-os/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/luminik-io/alfred-os/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/luminik-io/alfred-os/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/luminik-io/alfred-os/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/luminik-io/alfred-os/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/luminik-io/alfred-os/compare/0c5d13c673f5954014cb5b5ccf3dc880c9563641...v0.2.0
[0.1.0]: https://github.com/luminik-io/alfred-os/pull/2
