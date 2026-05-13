# Install

Fresh Mac to a working Alfred fleet skeleton, ~30 minutes.

For AWS IAM-per-agent, Slack, and troubleshooting, read [`BOOTSTRAP.md`](BOOTSTRAP.md) after this.

## TL;DR

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub auth
claude                            # Claude Code first-run auth
./bin/alfred-init.py              # choose agents, repos, codenames, Slack
```

The rest of this doc explains what each step does and what to do when something fails.

## What `install.sh` does

Idempotent (safe to re-run). On a fresh Mac:

1. Verifies macOS. Linux support tracked but not shipped; `launchd` is the scheduling layer.
2. Installs Homebrew if missing.
3. `brew install`s `git`, `gh`, `jq`, `awscli`, `python@3.11`, `node`, `uv`.
4. `npm install -g @anthropic-ai/claude-code`.
5. Creates `$HERMES_HOME` (default `~/.hermes`) and `$WORKSPACE_ROOT` (default `~/code`).
6. Drops `~/.alfredrc` from `.alfredrc.example`, prompts for `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`.
7. Appends a source-line to your shell rc (`~/.zshrc` / `~/.bashrc`) so every new shell loads `~/.alfredrc`.
8. Reports auth status for `gh`, `aws`, `claude`.

What it does **not** do (deliberately):

- Authenticate `gh` / `aws` / `claude`. Interactive flows you should see.
- Create AWS IAM users, secrets, or Slack webhooks. One-time human decisions.
- Choose which agents should run. Use `./bin/alfred-init.py` for that.
- Run `deploy.sh`. That side-effects `launchd`; you should know what's about to load.
- Touch existing `~/.hermes` content.
- Install a separate Hermes agent. In Alfred, `HERMES_HOME` is the runtime
  root name. Install Hermes separately only if your fleet uses Hermes skills,
  MCP, gbrain, canon, or dashboarding.

If you want a non-interactive run:

```sh
ALFRED_NONINTERACTIVE=1 GH_ORG=myorg OPERATOR_NAME='Your Name' \
  OPERATOR_EMAIL=you@example.com bash install.sh
```

## Step-by-step

### 1. Clone

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
```

### 2. Bootstrap

```sh
bash install.sh
```

Watch for two things:

- Homebrew install prompts for sudo.
- The script asks for GitHub org, display name, email. Defaults are fine; edit `~/.alfredrc` later.

### 3. Reload your shell

```sh
exec $SHELL
```

Confirms `HERMES_HOME` and `WORKSPACE_ROOT` are set in this session:

```sh
echo "$HERMES_HOME $WORKSPACE_ROOT"
```

### 4. Authenticate the CLIs

GitHub:

```sh
gh auth login
```

Pick HTTPS, log in via web, grant `repo` + `workflow` scopes. The agents push branches and open PRs.

Claude Code:

```sh
claude
```

First-run opens a browser to authenticate against your Anthropic account. Requires Pro or Max. The framework runs `claude -p` against your subscription's quota, no API key.

AWS (optional: only if you want Secrets Manager for Slack/credentials):

```sh
aws configure --profile <agent-name>-cron
```

See [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md) for the recommended IAM policies.

### 5. Slack webhook

The framework's `slack_post()` resolves a webhook URL via env → cache → AWS Secrets. The simplest path is the env var:

```sh
echo 'SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../...' >> ~/.alfredrc
```

For a full walkthrough of creating the Slack app + webhook, read [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md).

### 6. Configure the fleet

Run the wizard to choose agents, repos, codenames, Slack settings, and schedules:

```sh
./bin/alfred-init.py
```

`alfred-init.py` writes `launchd/agents.conf`, updates `~/.alfredrc`, runs `bash deploy.sh`, then runs `bash bin/doctor.sh`.

### 7. Framework-only deploy + verify

If you want to install the framework without enabling any agents yet:

```sh
bash deploy.sh
bash bin/doctor.sh
```

`deploy.sh` copies `lib/` and `bin/` into `$HERMES_HOME`. In a fresh checkout
there is no `launchd/agents.conf`, so this is framework-only and nothing
fires. After `alfred-init.py` creates `launchd/agents.conf`, deploy also
renders plists from `launchd/_template.plist` and bootstraps them via
`launchctl bootstrap`.

`doctor.sh` runs every agent's preflight under `HERMES_DOCTOR=1` to confirm env vars, CLI binaries, and auth chains resolve before any real firing burns Claude turns. On a clean install with the default `agents.conf` you should see `0 passed, 0 failed`.

### 8. Your first custom agent

Read `examples/bin/hello.py`, the smallest possible codename agent. Copy it to `bin/your-codename.py`, edit, add a line to `launchd/agents.conf`:

```
my.fleet.your-codename	your-codename.py	interval:3600	no
```

`bash deploy.sh` again. `bash bin/doctor.sh` again. Should show `1 passed, 0 failed`.

Then [`BOOTSTRAP.md`](BOOTSTRAP.md) for the full pattern: per-agent IAM, Slack reporting, `agent_runner` primitives, label state machine, prompt engineering.

## Troubleshooting `install.sh`

**"Refusing to install on non-macOS host."**
You're on Linux. The `launchd` scheduling layer is macOS-only today. A `systemd` port is on the roadmap but not shipped. Override with `ALFRED_FORCE_LINUX=1` only if you are reading code, running tests, or manually driving agents. See [`docs/LINUX.md`](docs/LINUX.md).

**"npm not found; skipping Claude Code install."**
The `node` brew install should bring `npm` along. If you skipped brew (`--skip-brew`), install Node manually then run install.sh again with `--skip-brew`.

**"sed: -i: requires an extension argument" or similar on a non-macOS host.**
This script uses BSD `sed` syntax. macOS only.

**Permissions errors on Homebrew install.**
Apple Silicon installs to `/opt/homebrew`; Intel to `/usr/local`. Both prompt for sudo on first install. If you cancel mid-flow, run install.sh again. It's idempotent.

**`gh auth login` opens browser but never completes.**
Run `gh auth login --hostname github.com --git-protocol https --web` explicitly. If your browser doesn't open, copy the device code from the terminal and visit github.com/login/device manually.

**`claude` CLI installed but `claude` not on PATH.**
The npm global install dir might not be on PATH. Run `npm config get prefix`, append `<that>/bin` to your PATH in `~/.zshrc`.

## Files install.sh writes

| Path | What it is | Safe to delete |
|---|---|---|
| `~/.alfredrc` | Operator config: sourced by every shell | After re-running install.sh |
| `~/.hermes/` | Runtime root (state, worktrees, deployed bin/lib) | Yes, `deploy.sh` repopulates |
| `~/code/` | Default workspace root | If you set a different `WORKSPACE_ROOT` |
| `~/.zshrc` (or `.bashrc`) | One source-block appended | Manually edit to remove |

Everything else lives inside the cloned repo and is removed by `rm -rf ~/code/alfred-os`.

## Where to go next

- [`BOOTSTRAP.md`](BOOTSTRAP.md): AWS IAM-per-agent, Slack, prompt sync, troubleshooting.
- [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md): Slack app + webhook + (optional) bot token.
- [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md): IAM users, scoped policies, Secrets Manager layout.
- [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md): Pro vs Max, switching accounts, `alfred claude`.
- [`docs/SKILLS.md`](docs/SKILLS.md): recommended Claude Code skills for an autonomous fleet.
- [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md): issue claim lifecycle and dedup primitives.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): design rationale.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to propose changes.
