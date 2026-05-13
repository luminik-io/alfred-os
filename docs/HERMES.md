# Hermes Integration

Alfred does not require Hermes.

The OSS runtime is local Python, `launchd`, git worktrees, GitHub CLI, model
CLIs, and optional Slack delivery. Hermes is a companion operator layer for
people who already want chat gateways, MCP servers, skills, memory, or
dashboards around their local agents.

## Boundary

| Tool | Owns | Should not own |
| --- | --- | --- |
| Alfred | launchd jobs, role runners, worktrees, issue claims, PR loops, Slack firing reports, spend guards, local state under `ALFRED_HOME` | Hermes gateway config, MCP registry, private memory, personal canon |
| Hermes | chat gateway, cron prompts, ACP dispatch, MCP tools, skills, memory, dashboards | Alfred's launchd plists, issue-claim mutations, per-agent worktrees |

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

~/.gbrain/
`-- brain.pglite
```

Do not copy a whole `~/.alfred` or `~/.hermes` directory between machines. Both
can contain tokens, transcripts, cached webhooks, local worktrees, and
operator-specific state.

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
   - MCP tool registry
   - skill bundles
   - gbrain or another memory layer
   - dashboards or phone/chat control

3. Wire Hermes to read from Alfred, not replace Alfred:

   ```bash
   alfred status
   alfred shipped --since 1d
   python "$ALFRED_HOME/bin/fleet-doctor.py"
   ```

For write-heavy roles, keep `launchd` as the owner and let Hermes observe.

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
AWS_PROFILE_FOR_HERMES="hermes-alfred"
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

## MCP, gbrain, and Canon

Hermes MCP servers are optional. Alfred agents should still work when MCP is
offline.

Clean shape:

1. Keep canonical company/product context outside Alfred's mutable state.
2. Let gbrain or Hermes index that context.
3. Let interactive sessions query it.
4. Keep scheduled Alfred runners deterministic: GitHub issue, repo checkout,
   prompt, state files, Slack result.

Examples:

```bash
hermes mcp list
hermes mcp test gbrain
gbrain query "what is the current product positioning?"
```

If Hermes reports `StdioServerParameters is not defined`, the local MCP Python
package is usually stale:

```bash
hermes update
~/.hermes/hermes-agent/venv/bin/pip install -U mcp
hermes mcp test gbrain
```

That is a Hermes repair, not an Alfred runtime requirement.

## Scheduling Rules

Good:

- launchd fires Alfred agents.
- Hermes posts daily operator summaries.
- Hermes calls read-only Alfred commands such as `alfred status` or
  `alfred shipped --since 1d`.
- Hermes links to Alfred Slack threads or dashboards.

Risky:

- launchd and Hermes cron both fire the same feature-dev runner.
- Hermes shells into an Alfred worktree while a runner owns the issue claim.
- Hermes mutates `ALFRED_HOME/state` directly.

If you need Hermes to start real Alfred work, prefer a GitHub label or issue
comment that Alfred picks up on its next scheduled run.

## Troubleshooting

**`ALFRED_HOME` is unset.** Source `~/.alfredrc` or export it before invoking an
Alfred script directly.

```bash
source ~/.alfredrc
bash bin/doctor.sh
```

**Hermes cannot see Alfred status.** Confirm Hermes shells inherit the same
`ALFRED_HOME`, `WORKSPACE_ROOT`, and `GH_ORG` values as your operator shell.

**gbrain is unavailable.** Scheduled Alfred runners should keep working. Fix the
gbrain/Hermes side separately and avoid making memory availability a hard
preflight for engineering agents.

**Hermes falls back to a different model.** That is an operator-layer decision.
Alfred's model choice lives in Alfred env vars and per-agent engine state.

## What Not To Commit

Do not commit:

- `~/.alfred/state`
- `~/.alfred/worktrees`
- `~/.hermes/.env`
- `~/.hermes/logs`
- `~/.gbrain`
- private canon, transcripts, issue data, or Slack exports

Public Alfred docs can mention Hermes and gbrain as optional integrations, but
must not imply either is installed by default.
