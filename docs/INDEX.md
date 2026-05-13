# Alfred docs index

Current map of the public docs. Trust code first, then this index.

## Start Here

- [`../README.md`](../README.md): overview, quick start, repository map, and status.
- [`../INSTALL.md`](../INSTALL.md): from-zero local install.
- [`../BOOTSTRAP.md`](../BOOTSTRAP.md): full operations setup for a first fleet.
- [`TUTORIAL.md`](TUTORIAL.md): build the Echo example agent end-to-end.

## Operating Model

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md): design rationale for launchd scheduling, worktrees, IAM, spend guards, and plan review.
- [`AGENTS.md`](AGENTS.md): default agent roles, codenames, and how custom codenames map to stable role scripts.
- [`STATE_MACHINE.md`](STATE_MACHINE.md): issue claim lifecycle and stale-claim recovery.
- [`CLAUDE_CODE.md`](CLAUDE_CODE.md): Claude Code install, account swap, and quota behavior.
- [`SLACK_SETUP.md`](SLACK_SETUP.md): incoming webhook and optional bot-token setup.
- [`AWS_SETUP.md`](AWS_SETUP.md): per-agent IAM and Secrets Manager setup.
- [`SKILLS.md`](SKILLS.md): recommended Claude Code skills.
- [`HERMES.md`](HERMES.md): optional Hermes, MCP, gbrain, canon, and skills integration.
- [`LINUX.md`](LINUX.md): current macOS-only scheduler stance and Linux/systemd notes.
- [`PUBLISHING.md`](PUBLISHING.md): GitHub Pages, release-site, and custom-domain operations.

## Reference

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
