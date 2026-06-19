# Hermes Integration

Alfred does not require Hermes.

The OSS runtime is local Python, host scheduler units, git worktrees, GitHub
CLI, model CLIs, and optional Slack delivery. Hermes is a companion operator layer for
people who already want chat gateways, MCP servers, skills, memory, or
dashboards around their local agents.

Use this page only if you already run Hermes or explicitly want Hermes features.
It is not part of the Alfred quick start.

## Current Hermes Landscape

As of the May 2026 public Hermes Agent releases, Hermes has grown into a broad
operator host:

- chat gateways for Telegram, Discord, Slack, WhatsApp, Signal, Email, and
  other channels
- built-in cron scheduling
- persistent `/goal` loops
- a SQLite-backed Kanban board with worker profiles, task comments, retry
  history, and dashboard views
- native MCP client/server support
- skills, memory, profiles, and multiple terminal backends
- Codex/Claude-style worker patterns through Hermes profiles and skills
- Hermes Desktop as a native Mac view over a Hermes host, using SSH and the
  host's real state rather than a separate gateway

That means Hermes can overlap with Alfred in orchestration and visibility.
Hermes Desktop can also be a useful operator console if your fleet already uses
Hermes. The clean boundary is still the same: Alfred owns its GitHub issue state
machine and scheduled engineering-agent execution; Hermes can observe, route,
or create handoffs around it.

## Boundary

| Tool | Owns | Should not own |
| --- | --- | --- |
| Alfred | scheduler units, role runners, worktrees, issue claims, PR loops, Slack firing reports, spend guards, local state under `ALFRED_HOME` | Hermes gateway config, MCP registry, private memory, personal canon |
| Hermes | chat gateway, cron prompts, durable Kanban cards, MCP tools, skills, memory, dashboards | Alfred's scheduler units, issue-claim mutations, per-agent worktrees |

`ALFRED_HOME` is Alfred's runtime root. A fresh install defaults it to
`~/.alfred`.

```bash
export ALFRED_HOME="$HOME/.alfred"
export WORKSPACE_ROOT="$HOME/code"
```

Keep Hermes configuration in Hermes-owned files and pass Alfred paths through
`ALFRED_HOME`.

## Recommended Layout

Keep Alfred and Hermes data separate unless you have a specific reason to
share:

```text
~/.alfred/
|-- bin/
|-- lib/
|-- state/
|-- worktrees/
`-- prompts/

~/.hermes/
|-- .env
|-- skills/
|-- canon/
|-- logs/
`-- hermes-agent/

~/.your-memory-tool/
`-- data/
```

Do not copy a whole `~/.alfred` or `~/.hermes` directory between machines. Both
can contain tokens, transcripts, cached webhooks, local worktrees, and
operator-specific state. Treat memory stores the same way.

## Install Order

1. Install Alfred first and verify it works:

   ```bash
   bash install.sh
   exec "$SHELL"
   bash deploy.sh
   bash bin/doctor.sh
   ```

2. Install Hermes separately only if you want Hermes features:

   - chat gateway
   - Kanban boards or persistent `/goal` loops
   - MCP tool registry
   - skill bundles
   - memory/canon layers
   - dashboards or phone/chat control

3. Wire Hermes to read from Alfred, not replace Alfred:

   ```bash
   alfred status
   alfred shipped --since 1d
   python "$ALFRED_HOME/bin/fleet-doctor.py"
   ```

For write-heavy roles, keep Alfred's host scheduler as the owner and let Hermes observe or
create GitHub handoffs.

## Env Hygiene

Keep env files boring. Quote values that contain spaces.

```bash
# ~/.alfredrc
ALFRED_HOME="$HOME/.alfred"
WORKSPACE_ROOT="$HOME/code"
GH_ORG="your-org"
SLACK_HOME_CHANNEL="alfred"
ACP_ARGS="--acp --stdio"
```

Avoid direct long-lived AWS access keys in shared env files. Prefer named
profiles:

```bash
AWS_PROFILE_FOR_HERMES="your-aws-profile"
```

If both Alfred and Hermes need cloud credentials, give them separate least-privilege
profiles. This keeps a chat-gateway task from inheriting a write-heavy agent
role by accident.

## Skills

Alfred does not ship Hermes skills. If you use Hermes as a skills host, keep
skills in the Hermes-managed directory or your Claude Code skills directory.

Good practice:

- review third-party skills before installing them
- pin skill versions where possible
- do not include private canon, transcripts, or customer data in public bundles
- treat skills as executable instructions, not docs

## MCP, Memory, and Canon

Hermes MCP servers and memory layers are optional. Alfred agents should still
work when they are offline.

Clean shape:

1. Keep canonical company/product context outside Alfred's mutable state.
2. Let your memory layer or Hermes index that context.
3. Let interactive sessions query it.
4. Keep scheduled Alfred runners deterministic: GitHub issue, repo checkout,
   prompt, state files, Slack result.

If your private setup exposes a memory MCP server, keep it optional and test it
from the Hermes side:

```bash
hermes mcp list
hermes mcp test <server-name>
```

If Hermes reports `StdioServerParameters is not defined`, the local MCP Python
package is usually stale:

```bash
hermes update
~/.hermes/hermes-agent/venv/bin/pip install -U mcp
hermes mcp test <server-name>
```

That is a Hermes repair, not an Alfred runtime requirement.

## Scheduling Rules

Good:

- Alfred's host scheduler fires Alfred agents.
- Hermes posts daily operator summaries.
- Hermes calls read-only Alfred commands such as `alfred status` or
  `alfred shipped --since 1d`.
- Hermes links to Alfred Slack threads or dashboards.
- Hermes creates a GitHub issue or label that Alfred picks up later.

Risky:

- Alfred's host scheduler and Hermes cron both fire the same feature-dev runner.
- Hermes shells into an Alfred worktree while a runner owns the issue claim.
- Hermes mutates `ALFRED_HOME/state` directly.
- Hermes Kanban treats a task as done while the matching Alfred issue/PR is
  still in flight.

If you need Hermes to start real Alfred work, prefer a GitHub label or issue
comment that Alfred picks up on its next scheduled run.

## Best Integration Shape

The best bridge is additive and boring:

1. `alfred mcp serve` exposes read-only status, recent firings, shipped
   summaries, and safe issue/PR lookup.
2. `alfred events export` writes sanitized JSONL from `ALFRED_HOME/state` for
   Hermes dashboards or memory ingestion.
3. A Hermes skill can create a GitHub issue with `agent:implement` or
   `agent:large-feature`, then Alfred owns the GitHub state path. For
   `agent:large-feature`, Batman can draft the bundle plan, wait for approval,
   and file child issues. The repo-specific agents still claim and implement
   those child issues through the normal queue.
4. Optional: a Hermes Kanban card can link to the GitHub issue/PR, but the
   GitHub state remains the source of truth for Alfred work.

Do not make Hermes a transitive install dependency of Alfred. Operators who do
not need a chat gateway, Kanban, or memory should never see it during setup.

## Troubleshooting

**`ALFRED_HOME` is unset.** Source `~/.alfredrc` or export it before invoking an
Alfred script directly.

```bash
source ~/.alfredrc
bash bin/doctor.sh
```

**Hermes cannot see Alfred status.** Confirm Hermes shells inherit the same
`ALFRED_HOME`, `WORKSPACE_ROOT`, and `GH_ORG` values as your operator shell.

**Memory is unavailable.** Scheduled Alfred runners should keep working. Fix the
memory/Hermes side separately and avoid making memory availability a hard
preflight for engineering agents.

**Hermes falls back to a different model.** That is an operator-layer decision.
Alfred's model choice lives in Alfred env vars and per-agent engine state.

## What Not To Commit

Do not commit:

- `~/.alfred/state`
- `~/.alfred/worktrees`
- `~/.hermes/.env`
- `~/.hermes/logs`
- memory-store data directories
- private canon, transcripts, issue data, or Slack exports

Public Alfred docs can mention Hermes and memory tools as optional integrations,
but must not imply either is installed by default.
