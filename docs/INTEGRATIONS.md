# Integrations

Alfred core should stay boring: local Python runners, host-scheduler units,
git worktrees, GitHub CLI, model CLIs, and optional Slack delivery.

Everything else is an integration. Useful integrations are welcome, but they
should not become required for a clean open-source install.

The default model path is also intentionally plain: Alfred uses local Claude
Code / Codex CLI authentication and does not manage provider API keys.

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

Alfred uses `ALFRED_HOME` only for its runtime path.

## Bundling Policy

Alfred may include:

- first-party adapters that read or write Alfred's local state
- docs and examples for external tools
- small optional CLIs that fail gracefully when their provider is absent

Alfred should not bundle:

- private memory stores, canon files, transcripts, or operator data
- a required vector database
- a required external agent runtime
- third-party skill bundles fetched at runtime without review
- opaque background services that are not visible in `doctor.sh`

This keeps `bash install.sh && bash deploy.sh && bash bin/doctor.sh` explainable
on a new machine.

## Common Profiles

| Profile | Use it when | Required pieces |
| --- | --- | --- |
| Standalone Alfred | You want scheduled engineering agents on a local Mac or Linux host. | Alfred, Python, `gh`, `git`, Claude Code or Codex, optional Slack webhook. |
| Alfred + external memory | You want searchable personal/company memory available to interactive sessions. | Standalone Alfred plus a separately installed memory layer. |
| Alfred + operator gateway | You want chat control, MCP registration, skills, or dashboards around Alfred. | Standalone Alfred plus a separately installed gateway/operator layer. |

The standalone profile is the default and the only path the OSS installer
should assume.

## Memory

Memory tools can be useful companion layers, but they are not Alfred state. Keep
them outside `ALFRED_HOME`; point interactive agents at them through your shell,
Claude Code, MCP, or a separate operator gateway.

Recommended shape:

```text
~/.your-memory-tool/
`-- data/
```

Alfred runners should continue working if memory is missing. If a prompt
mentions a memory tool, it should phrase it as optional context, not a hard
preflight requirement.

## Operator gateways

A gateway can wrap Alfred nicely when you want chat commands, MCP servers,
skills, durable task boards, or dashboards around the fleet. It should observe
Alfred or call read-only commands unless you intentionally build a handoff.

Good boundaries:

- Alfred owns issue claims, worktrees, scheduler units, state files, and Slack
  firing messages.
- The gateway owns chat behavior, skills, MCP server registration, memory, and
  operator dashboards.
- Both tools may read `ALFRED_HOME/state`, but only Alfred runners should mutate
  in-flight issue and worktree state.

See [`HERMES.md`](HERMES.md) for the optional Hermes recipe.

## Future Adapters

Good next integrations:

- `alfred mcp serve` exposing read-only fleet status, issue claims, and shipped
  summaries.
- `alfred events export` producing sanitized JSONL for memory tools, dashboards,
  or external task boards.
- `alfred gateway bridge` creating GitHub issues/labels from an external board
  without letting that board mutate Alfred state directly.
- `alfred dashboards export` writing a static fleet snapshot for local or
  hosted dashboards.

These should be additive. A new operator should still be able to run Alfred
without installing any of them.
