# Alfred docs index

Current map of the public docs. Trust code first, then this index.

## Start Here

- [`../README.md`](../README.md): overview, quick start, repository map, and status.
- [`../INSTALL.md`](../INSTALL.md): from-zero local install.
- [`AI_ASSISTED_INSTALL.md`](AI_ASSISTED_INSTALL.md): copy-paste prompt and guardrails for Claude Code, Codex, or another local coding assistant to install Alfred.
- [`INSTALL_TIERS.md`](INSTALL_TIERS.md): the three install tiers (`core`, `client`, `slack`) and how the CLI and fleet run fully standalone.
- [`WORKSPACE_PATTERNS.md`](WORKSPACE_PATTERNS.md): one-repo, multi-repo, specs-led, and Batman planning layouts.
- [`MONOREPO.md`](MONOREPO.md): running Alfred against a pnpm, Turborepo, or Cargo workspace.
- [`MULTI_REPO_WORKED_EXAMPLE.md`](MULTI_REPO_WORKED_EXAMPLE.md): one feature shipped across three repos using Batman plus the full fleet.
- [`SPECS_DRIVEN_DEVELOPMENT.md`](SPECS_DRIVEN_DEVELOPMENT.md): turning specs into issue queues, Batman plans, and reviewable PRs.
- [`INSTALL_TIME.md`](INSTALL_TIME.md): honest read on existing-setup (30 min) and fresh-machine (60 to 120 min) install duration.
- [`../BOOTSTRAP.md`](../BOOTSTRAP.md): full operations setup for a first fleet.
- [`TUTORIAL.md`](TUTORIAL.md): build the Echo example agent end-to-end.
- [`DRY_RUN.md`](DRY_RUN.md): watch a side-effect-safe firing lifecycle before trusting scheduled work.

## Operating Model

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md): design rationale for host scheduling, worktrees, IAM, spend guards, and plan review.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): the diagram companion. Mermaid diagrams for the agent lifecycle, model dispatch and tiers, distributed locking, the Slack conversational flow, the desktop app, the disk guardian, and the layered install and distribution.
- [`AGENTS.md`](AGENTS.md): default agent roles, stable runtime codenames, and custom display-name themes.
- [`STATE_MACHINE.md`](STATE_MACHINE.md): issue claim lifecycle and stale-claim recovery.
- [`STATE_AND_MEMORY.md`](STATE_AND_MEMORY.md): what Alfred remembers between firings, where every state file lives, and the local fleet-brain memory layer.
- [`FLEET_BRAIN.md`](FLEET_BRAIN.md): local memory schema, reviewable lesson candidates, failure history, CLI, and read-only MCP bridge.
- [`MEMORY_PROVIDERS.md`](MEMORY_PROVIDERS.md): Redis Agent Memory, FleetBrain's local ledger role, provider chaining, and optional read-only fallback stores.
- [`SLACK_UX.md`](SLACK_UX.md): Slack-native message shape, planning replies, approval flow, and anti-patterns.
- [`DESKTOP_CLIENT.md`](DESKTOP_CLIENT.md): Alfred Desktop design rationale and tab-by-tab tour, the Slack-native boundary, the `alfred serve` API and native allowlist, and building native installers.
- [`DESIGN.md`](DESIGN.md): the visual language for the native app and the site. Color tokens, the Instrument Sans plus Quicksand plus Fragment Mono type stack, glass surfaces, motion and `prefers-reduced-motion`, and accessibility.
- [`GOALS.md`](GOALS.md): durable goal contract for Slack, CLI, client, planning readiness, evaluator, and memory integration.
- [`PLAIN_MODE.md`](PLAIN_MODE.md): the non-technical intake profile (`ALFRED_INTAKE_PROFILE=plain`).
- [`ENGINE_ROUTING.md`](ENGINE_ROUTING.md): per-codename Claude, Codex, or hybrid routing; precedence chain; default matrix; multi-engine roadmap.
- [`OPERATING_THE_FLEET.md`](OPERATING_THE_FLEET.md): week-two runbook. Daily Slack rhythm, CLI recipes, sentinels, logs, "fleet went quiet" troubleshooting.
- [`CLAUDE_CODE.md`](CLAUDE_CODE.md): Claude Code and Codex install, account routing, engine routing, and quota behavior.
- [`CAPABILITIES.md`](CAPABILITIES.md): read-only local inventory for code graph memory, context compression, and engineering skill packs.
- [`BENCHMARKS.md`](BENCHMARKS.md): reproducible self-benchmark harness. The fixed task suite, the four metric families read from existing telemetry, how to run before/after, and cost framed as a share of subscription quota.
- [`CODEX_PROVIDER.md`](CODEX_PROVIDER.md): Codex engine modes, diagnostics, runtime contract, and billing posture.
- [`SLACK_SETUP.md`](SLACK_SETUP.md): incoming webhook, optional bot-token setup, planning listener, trusted control commands, the issue bridge, and in-thread fleet-progress thread-sync.
- [`SLACK_APPROVAL.md`](SLACK_APPROVAL.md): reaction approval gate, trusted feedback users, and Socket Mode listener boundary.
- [`AWS_SETUP.md`](AWS_SETUP.md): per-agent IAM and Secrets Manager setup.
- [`SKILLS.md`](SKILLS.md): recommended Claude Code skills.
- [`INTEGRATIONS.md`](INTEGRATIONS.md): what Alfred does and does not bundle.
- [`LINUX.md`](LINUX.md): running the fleet on Debian/Ubuntu via `systemd --user` timers. Install, deploy, operate, `linger`.
- [`PUBLISHING.md`](PUBLISHING.md): GitHub Pages, release-site, and custom-domain operations.

## Reference

- [`OUTPUT_SAMPLES.md`](OUTPUT_SAMPLES.md): every shape of Slack post, doctor run, issue body, PR, and state JSON in one place.
- [`GLOSSARY.md`](GLOSSARY.md): one-sentence definitions for every codename, label, sentinel, and runtime concept.
- [`../lib/agent_runner/`](../lib/agent_runner/__init__.py): shared runtime library (package; public API in `__init__.py`).
- [`../lib/slack_format.py`](../lib/slack_format.py): Slack Block Kit formatting helpers.
- [`../lib/batman.py`](../lib/batman.py): multi-repo bundle primitives.
- [`../bin/`](../bin/): Alfred CLI, init wizard, doctor, deploy helpers, and reference agent runners.
- [`../launchd/`](../launchd/): plist template, renderer, and `agents.conf.example`.
- [`../examples/`](../examples/): minimal example agents, label-state CLI, and pre-push hook.

## Project

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
- [`../ROADMAP.md`](../ROADMAP.md)
- [`../CHANGELOG.md`](../CHANGELOG.md)
- [`../SECURITY.md`](../SECURITY.md)
- [`THREAT_MODEL.md`](THREAT_MODEL.md): what one run can and cannot do, the containment boundaries, and how to verify the privacy claim yourself.
- [`MACOS_PERMISSIONS.md`](MACOS_PERMISSIONS.md): every macOS prompt explained, plus the permissions Alfred never requests.
- [`../SUPPORT.md`](../SUPPORT.md)
- [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md)
- [`RELEASING.md`](RELEASING.md): the tag-to-publish release process, including the draft-release gate for desktop assets.

## Tests

Run the whole suite with:

```sh
python3 -m pytest tests/
```

Use `bash bin/scrub-check.sh` before public releases.
