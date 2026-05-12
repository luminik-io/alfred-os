# Hermes integration

Alfred-OS does not require Hermes. The core runtime is local Python, launchd,
GitHub CLI, git worktrees, Slack delivery, and model CLIs such as Claude Code
and Codex.

Hermes is still a useful companion if you already use it as a personal agent
gateway. Treat Hermes as the operator layer and Alfred-OS as the engineering
fleet layer.

## Boundary

| Layer | Owns | Does not own |
|---|---|---|
| Alfred-OS | launchd jobs, role runners, worktrees, issue claims, PR loops, Slack reports, spend guards | Hermes gateway, MCP server registry, gbrain, personal canon |
| Hermes | chat gateway, cron prompts, ACP dispatch, MCP tools, skills, memory, dashboards | Alfred-OS state machine, launchd plists, per-agent worktrees |

`HERMES_HOME` is the shared runtime-root name. In a plain Alfred-OS install it
defaults to `~/.hermes`, but that does not mean Hermes is installed. In a
Hermes-backed setup, both tools can point at the same root:

```sh
export HERMES_HOME="$HOME/.hermes"
export WORKSPACE_ROOT="$HOME/code"
export GH_ORG="your-github-org"
```

## Recommended Layout

```text
~/.hermes/
|-- .env                         # local secrets and runtime env, never commit
|-- bin/                         # Alfred-OS deployed role runners and CLIs
|-- lib/                         # Alfred-OS deployed Python runtime
|-- state/                       # claims, locks, reports, transcripts, caches
|-- logs/                        # Hermes gateway logs and local cron logs
|-- skills/                      # optional Hermes / Claude skill bundles
|-- canon/                       # optional operator canon
`-- hermes-agent/                # optional Hermes checkout / venv
```

Do not copy a whole `~/.hermes` directory between machines. It can contain
Slack webhooks, API keys, OAuth tokens, cookies, private transcripts, and
machine-specific launchd state. Copy only explicit bundles such as skills or
docs after reviewing their contents.

## Install Order

1. Install and authenticate the tools Alfred-OS needs:

   ```sh
   gh auth login
   claude
   codex --version
   ```

2. Install Alfred-OS:

   ```sh
   git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
   cd ~/code/alfred-os
   bash install.sh
   exec "$SHELL"
   ./bin/alfred-init.py
   ```

3. Install Hermes separately only if you want Hermes features:

   - chat gateway to Slack, WhatsApp, or another surface
   - Hermes cron prompts
   - ACP dispatch
   - MCP tools
   - gbrain or another memory layer
   - personal canon or dashboards

4. Point both systems at the same runtime root:

   ```sh
   export HERMES_HOME="$HOME/.hermes"
   bash ~/code/alfred-os/deploy.sh
   ```

5. Run both health checks:

   ```sh
   alfred status
   alfred-doctor
   hermes mcp list
   ```

## Environment

Keep Hermes and Alfred-OS env files boring. Quote values that contain spaces.
Prefer named profiles over direct AWS keys.

```sh
HERMES_HOME="$HOME/.hermes"
WORKSPACE_ROOT="$HOME/code"
GH_ORG="your-github-org"

# ACP / stdio args must be quoted or shell sourcing will split them.
ACP_ARGS="--acp --stdio"

# Prefer profiles for local scheduled work.
AWS_PROFILE_FOR_HERMES="hermes-alfred"

# Optional if a local script calls the Gemini Developer API directly.
GEMINI_API_KEY="..."

# Optional if a local script writes to Google Sheets or GCP.
GOOGLE_APPLICATION_CREDENTIALS="$HOME/.hermes/gcp-key.json"
```

Avoid keeping these active in `~/.hermes/.env` unless a specific script truly
requires them:

```sh
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...
```

Those variables override named AWS profiles and can make a profile-backed agent
fail with stale signatures. Alfred-OS runners that touch AWS strip direct AWS
env leakage before profile-specific calls, but the cleanest local setup is still
profile-first.

## Skills

Alfred-OS does not ship skills. A Hermes-backed operator can keep skills under
`~/.hermes/skills` and expose them through Hermes or Claude Code.

For a teammate handoff, package only the reviewed skills:

```sh
cd ~/.hermes/skills
tar -czf ~/hermes-skills.tar.gz \
  claude-code \
  codex \
  hermes-agent \
  github-auth \
  github-code-review \
  github-issues \
  github-pr-workflow \
  github-repo-management \
  native-mcp \
  systematic-debugging \
  test-driven-development
```

Install on the new machine:

```sh
mkdir -p ~/.hermes/skills
tar -xzf ~/hermes-skills.tar.gz -C ~/.hermes/skills
hermes skills list
```

If a skill came from a third-party repository, pin or record the commit you
reviewed. Skills can run commands through the agent, so treat them like code
dependencies.

## MCP, gbrain, And Canon

Hermes MCP servers are optional. Alfred-OS agents should keep working without
them, but a richer local fleet can use MCP for context lookups.

Recommended pattern:

1. Keep product facts in code and docs the agent can read directly.
2. Keep canonical operator facts in a separate canon file or memory system.
3. Let Hermes expose MCP tools to interactive sessions.
4. Let scheduled Alfred-OS agents receive only the narrow context they need.

Example checks:

```sh
hermes mcp list
hermes mcp test gbrain
gbrain query "what is the current product positioning?"
```

If Hermes reports `StdioServerParameters is not defined`, the local MCP Python
package is usually stale or broken:

```sh
hermes update
~/.hermes/hermes-agent/venv/bin/pip install -U mcp
hermes mcp test gbrain
```

## Scheduling Patterns

Use one scheduler for each agent role.

Good:

- Alfred-OS launchd fires engineering agents.
- Hermes cron posts daily operator summaries.
- Hermes cron calls `alfred shipped --since 1d` for a human report.

Risky:

- launchd and Hermes cron both fire the same feature-dev runner.
- Hermes cron shells into an Alfred-OS worktree while the launchd runner owns
  the issue claim.

If you want Hermes to drive an Alfred-OS command, prefer read-only commands:

```sh
alfred status
alfred agents
alfred shipped --since 1d
python "$HERMES_HOME/bin/fleet-doctor.py"
```

For write-heavy agent roles, keep launchd as the owner and let Hermes observe.

## Observability

Useful local checks:

```sh
alfred status
launchctl list | grep -i alfred
tail -n 100 "$HERMES_HOME/logs/gateway.log"
find "$HERMES_HOME/state" -maxdepth 3 -type f | sort | tail
```

If Slack messages do not appear, check:

1. `SLACK_WEBHOOK_URL` or the AWS secret backing it.
2. `$HERMES_HOME/state/slack-webhook.cache`.
3. Hermes gateway connection logs, if Hermes owns chat delivery.
4. Alfred-OS runner logs and per-firing Slack helper output.

## Troubleshooting

**`HERMES_HOME` is unset.** Source `~/.alfredrc` or export it before invoking a
runner manually:

```sh
source ~/.alfredrc
```

**Shell sourcing fails around ACP args.** Quote the value:

```sh
ACP_ARGS="--acp --stdio"
```

**AWS profile fails even after login.** Remove stale direct AWS env vars from
the active shell and env files:

```sh
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN
aws sts get-caller-identity --profile hermes-alfred
```

**Hermes falls back from Claude to Gemini.** That is expected when the Claude
subscription cap is exhausted. Treat OAuth refresh and subscription quota as
separate issues.

**A teammate is missing skills.** Do not send the whole runtime directory.
Package the reviewed skill directories, unpack them into `~/.hermes/skills`,
then run `hermes skills list`.

## Public Repo Rule

Public Alfred-OS examples must stay generic. Do not commit:

- personal workspace paths
- company-only repo names
- Slack channel IDs or webhook URLs
- AWS account IDs, access keys, or secret names tied to a private fleet
- private canon, gbrain exports, transcripts, or issue data

Use `bash bin/scrub-check.sh` before opening a release PR.
