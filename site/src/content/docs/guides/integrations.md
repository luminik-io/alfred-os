---
title: Integrations
description: What Alfred bundles, what stays optional, and how to think about Hermes, gbrain, MCP, and dashboards.
---

Alfred core should stay boring: local Python runners, `launchd` scheduling,
git worktrees, GitHub CLI, model CLIs, and optional Slack delivery.

`ALFRED_HOME` is the runtime root. A fresh install defaults to `~/.alfred`.
Alfred OS uses `ALFRED_HOME` only for its runtime path.

## Bundling policy

Alfred may include adapters, examples, docs, and small optional CLIs that fail
gracefully when a provider is missing.

Alfred should not bundle private memory stores, canon files, transcripts, a
required vector database, a required Hermes install, or third-party skills
fetched at runtime without review.

## Profiles

| Profile | Use it when | Required pieces |
| --- | --- | --- |
| Standalone Alfred | You want scheduled engineering agents on one machine. | Alfred, Python, `gh`, `git`, Claude Code or Codex, optional Slack webhook. |
| Alfred + gbrain | You want searchable memory in interactive sessions. | Standalone Alfred plus separately installed gbrain. |
| Alfred + Hermes | You want a chat gateway, MCP registry, skills, or dashboards around Alfred. | Standalone Alfred plus separately installed Hermes. |

The standalone profile is the only path the installer assumes.

## Good next adapters

- `alfred mcp serve` for read-only fleet status and shipped summaries.
- `alfred memory export` for sanitized gbrain imports.
- `alfred dashboards export` for static fleet snapshots.

All should be additive. New operators should still be able to run Alfred
without installing any of them.
