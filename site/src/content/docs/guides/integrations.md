---
title: Integrations
description: What Alfred bundles, what stays optional, and how to think about companion tools.
---

Alfred core should stay boring: local Python runners, host-scheduler units,
git worktrees, GitHub CLI, model CLIs, local Redis Agent Memory, FleetBrain,
and optional Slack delivery.

The default model path uses local Claude Code / Codex CLI authentication. Alfred
does not manage provider API keys.

`ALFRED_HOME` is the runtime root. A fresh install defaults to `~/.alfred`.
Alfred uses `ALFRED_HOME` only for its runtime path.

## Bundling policy

Alfred may include adapters, examples, docs, and small optional CLIs that fail
gracefully when a provider is missing.

Alfred should not bundle private memory stores, canon files, transcripts, a
hosted vector database, a required external agent runtime, or third-party skills
fetched at runtime without review.

## Profiles

| Profile | Use it when | Required pieces |
| --- | --- | --- |
| Standalone Alfred | You want scheduled engineering agents on a local Mac or Linux host. | Alfred, Python, `gh`, `git`, Claude Code or Codex, local Redis Agent Memory, optional Slack webhook. |
| Alfred + personal memory | You want Alfred to consult your own notes or knowledge base as a fallback. | Standalone Alfred plus a read-only provider such as `gbrain`. |
| Alfred + control gateway | You want chat control, MCP registration, skills, durable task boards, or dashboards around Alfred. | Standalone Alfred plus a separately installed gateway/control layer. |

The standalone profile is the only path the installer assumes.

## Good next adapters

- `alfred mcp serve` for read-only fleet status and shipped summaries.
- `alfred events export` for sanitized memory/dashboard imports.
- `alfred gateway bridge` for GitHub issue/label handoff from an external board.
- `alfred dashboards export` for static fleet snapshots.

All should be additive. New users should still be able to run Alfred
without installing any of them.
