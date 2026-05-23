# Alfred docs index

Current map of the public docs. Trust code first, then this index.

## Start Here

- [`../README.md`](../README.md): overview, quick start, repository map, and status.
- [`../INSTALL.md`](../INSTALL.md): from-zero local install.
- [`AI_ASSISTED_INSTALL.md`](AI_ASSISTED_INSTALL.md): copy-paste prompt and guardrails for Claude Code, Codex, or another local coding assistant to install Alfred.
- [`WORKSPACE_PATTERNS.md`](WORKSPACE_PATTERNS.md): one-repo, multi-repo, specs-led, and Batman planning layouts.
- [`MONOREPO.md`](MONOREPO.md): running Alfred against a pnpm, Turborepo, or Cargo workspace.
- [`MULTI_REPO_WORKED_EXAMPLE.md`](MULTI_REPO_WORKED_EXAMPLE.md): one feature shipped across three repos using Batman plus the starter fleet.
- [`SPECS_DRIVEN_DEVELOPMENT.md`](SPECS_DRIVEN_DEVELOPMENT.md): turning specs into issue queues, Batman plans, and reviewable PRs.
- [`INSTALL_TIME.md`](INSTALL_TIME.md): honest read on warm-start (30 min) and cold-start (60 to 120 min) install duration.
- [`../BOOTSTRAP.md`](../BOOTSTRAP.md): full operations setup for a first fleet.
- [`TUTORIAL.md`](TUTORIAL.md): build the Echo example agent end-to-end.
- [`DRY_RUN.md`](DRY_RUN.md): watch a full firing lifecycle with no LLM call, no spend, and no side effects.

## Operating Model

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md): design rationale for host scheduling, worktrees, IAM, spend guards, and plan review.
- [`AGENTS.md`](AGENTS.md): default agent roles, codenames, and how custom codenames map to stable role scripts.
- [`STATE_MACHINE.md`](STATE_MACHINE.md): issue claim lifecycle and stale-claim recovery.
- [`CLAUDE_CODE.md`](CLAUDE_CODE.md): Claude Code and Codex install, account routing, engine routing, and quota behavior.
- [`CODEX_PROVIDER.md`](CODEX_PROVIDER.md): Codex engine modes, diagnostics, runtime contract, and billing posture.
- [`SLACK_SETUP.md`](SLACK_SETUP.md): incoming webhook and optional bot-token setup.
- [`AWS_SETUP.md`](AWS_SETUP.md): per-agent IAM and Secrets Manager setup.
- [`SKILLS.md`](SKILLS.md): recommended Claude Code skills.
- [`INTEGRATIONS.md`](INTEGRATIONS.md): what Alfred does and does not bundle.
- [`HERMES.md`](HERMES.md): optional Hermes/operator-gateway recipe.
- [`LINUX.md`](LINUX.md): running the fleet on Debian/Ubuntu via `systemd --user` timers. Install, deploy, operate, `linger`.
- [`PUBLISHING.md`](PUBLISHING.md): GitHub Pages, release-site, and custom-domain operations.

## Reference

- [`OUTPUT_SAMPLES.md`](OUTPUT_SAMPLES.md): every shape of Slack post, doctor run, issue body, PR, and state JSON in one place.
- [`GLOSSARY.md`](GLOSSARY.md): one-sentence definitions for every codename, label, sentinel, and runtime concept.
- [`../lib/agent_runner.py`](../lib/agent_runner.py): shared runtime library.
- [`../lib/slack_format.py`](../lib/slack_format.py): Slack Block Kit formatting helpers.
- [`../lib/batman.py`](../lib/batman.py): multi-repo bundle primitives.
- [`../bin/`](../bin/): operator CLI, init wizard, doctor, deploy helpers, and reference agent runners.
- [`../launchd/`](../launchd/): plist template, renderer, and `agents.conf.example`.
- [`../examples/`](../examples/): minimal example agents, label-state CLI, and pre-push hook.

## Project

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
- [`../ROADMAP.md`](../ROADMAP.md)
- [`../CHANGELOG.md`](../CHANGELOG.md)
- [`../SECURITY.md`](../SECURITY.md)
- [`../SUPPORT.md`](../SUPPORT.md)
- [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md)

## Tests

Run the whole suite with:

```sh
python3 -m pytest tests/
```

Use `bash bin/scrub-check.sh` before public releases.
