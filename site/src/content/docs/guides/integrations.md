---
title: Integrations
description: What Alfred bundles, what stays optional, and how to think about companion tools.
---

Alfred core should stay boring: local Python runners, `launchd` scheduling,
git worktrees, GitHub CLI, model CLIs, and optional Slack delivery.

`ALFRED_HOME` is the runtime root. A fresh install defaults to `~/.alfred`.
Alfred uses `ALFRED_HOME` only for its runtime path.

## Bundling policy

Alfred may include adapters, examples, docs, and small optional CLIs that fail
gracefully when a provider is missing.

Alfred should not bundle private memory stores, canon files, transcripts, a
required vector database, a required external agent runtime, or third-party
skills fetched at runtime without review.

## Profiles

| Profile | Use it when | Required pieces |
| --- | --- | --- |
| Standalone Alfred | You want scheduled engineering agents on one machine. | Alfred, Python, `gh`, `git`, Claude Code or Codex, optional Slack webhook. |
| Alfred + external memory | You want searchable memory in interactive sessions. | Standalone Alfred plus a separately installed memory layer. |
| Alfred + operator gateway | You want chat control, MCP registration, skills, durable task boards, or dashboards around Alfred. | Standalone Alfred plus a separately installed gateway/operator layer. |

The standalone profile is the only path the installer assumes.

## Good next adapters

- `alfred mcp serve` for read-only fleet status and shipped summaries.
- `alfred events export` for sanitized memory/dashboard imports.
- `alfred gateway bridge` for GitHub issue/label handoff from an external board.
- `alfred dashboards export` for static fleet snapshots.

All should be additive. New operators should still be able to run Alfred
without installing any of them.
