# Integrations

Alfred core should stay boring: local Python runners, `launchd` scheduling,
git worktrees, GitHub CLI, model CLIs, and optional Slack delivery.

Everything else is an integration. Useful integrations are welcome, but they
should not become required for a clean open-source install.

## Runtime Boundary

`ALFRED_HOME` is the Alfred runtime root. A fresh install defaults it to
`~/.alfred`.

```bash
export ALFRED_HOME="$HOME/.alfred"
export WORKSPACE_ROOT="$HOME/code"
```

The runtime root contains deployed scripts, shared libraries, state,
transcripts, Slack cache files, and per-firing worktrees:

```text
~/.alfred/
|-- bin/
|-- lib/
|-- state/
|-- worktrees/
`-- prompts/
```

Alfred OS uses `ALFRED_HOME` only for its runtime path.

## Bundling Policy

Alfred may include:

- first-party adapters that read or write Alfred's local state
- docs and examples for external tools
- small optional CLIs that fail gracefully when their provider is absent

Alfred should not bundle:

- private memory stores, canon files, transcripts, or operator data
- a required vector database
- a required Hermes install
- third-party skill bundles fetched at runtime without review
- opaque background services that are not visible in `doctor.sh`

This keeps `bash install.sh && bash deploy.sh && bash bin/doctor.sh` explainable
on a new machine.

## Common Profiles

| Profile | Use it when | Required pieces |
| --- | --- | --- |
| Standalone Alfred | You want scheduled engineering agents on one machine. | Alfred, Python, `gh`, `git`, Claude Code or Codex, optional Slack webhook. |
| Alfred + gbrain | You want searchable personal/company memory available to interactive sessions. | Standalone Alfred plus a separately installed gbrain. |
| Alfred + Hermes | You already use Hermes as a chat gateway, cron prompt router, MCP registry, or skills host. | Standalone Alfred plus a separately installed Hermes operator layer. |

The standalone profile is the default and the only path the OSS installer
should assume.

## gbrain

gbrain is a useful companion memory layer, but it is not Alfred state. Keep it
outside `ALFRED_HOME`; point interactive agents at it through your shell,
Claude Code, Hermes, or MCP setup.

Recommended shape:

```text
~/.gbrain/
`-- brain.pglite
```

Alfred runners should continue working if `gbrain` is missing. If a prompt
mentions gbrain, it should phrase it as optional context, not a hard
preflight requirement.

## Hermes

Hermes can wrap Alfred nicely when you want chat commands, MCP servers, skills,
or dashboards around the fleet. It should observe Alfred or call read-only
commands unless you intentionally build a handoff.

Good boundaries:

- Alfred owns issue claims, worktrees, launchd jobs, state files, and Slack
  firing messages.
- Hermes owns chat gateway behavior, skills, MCP server registration, memory,
  and operator dashboards.
- Both tools may read `ALFRED_HOME/state`, but only Alfred runners should mutate
  in-flight issue and worktree state.

See [`HERMES.md`](HERMES.md) for the optional recipe.

## Future Adapters

Good next integrations:

- `alfred mcp serve` exposing read-only fleet status, issue claims, and shipped
  summaries.
- `alfred memory export` producing sanitized JSON for gbrain or another memory
  layer.
- `alfred dashboards export` writing a static fleet snapshot for local or
  hosted dashboards.

These should be additive. A new operator should still be able to run Alfred
without installing any of them.
