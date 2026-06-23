# Install

Fresh Mac or Debian/Ubuntu host to a working Alfred fleet skeleton, ~30 minutes.

For AWS IAM-per-agent, Slack, and troubleshooting, read [`BOOTSTRAP.md`](BOOTSTRAP.md) after this.

## Two ways to install

**Desktop app, no terminal.** Download the signed Alfred app for macOS or Linux from [alfred.luminik.io/download](https://alfred.luminik.io/download/), open it, and follow the guided Setup tab. It connects GitHub and Claude, helps you pick repos and agents, and starts the local runtime for you, with no terminal commands. This is the simplest path on a single machine.

**Command line.** Use the steps below when you want a headless Linux host, to work from `main`, to script the install, or because you prefer the terminal. On macOS you can install from source or with Homebrew; on Linux use the source checkout. Either way the command line installs the `core` runtime only; the desktop app is a separate, optional download, so a command-line install never adds a desktop client you did not ask for. You can still download the app later and point it at the same runtime.

Both paths install the `core` tier: the fleet, the operator CLI, the host scheduler, and the `alfred serve` JSON API. Core is fully standalone and runs headless on Linux. The optional desktop `client` and Slack `slack` tiers layer on top of it; see [`docs/INSTALL_TIERS.md`](docs/INSTALL_TIERS.md).

## TL;DR

Source checkout path:

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub auth
claude                            # Claude Code first-run auth
./bin/alfred-init.py              # choose agents, repos, codenames, Slack
```

macOS Homebrew path, if you prefer package-manager installs:

```sh
brew tap luminik-io/alfred-os https://github.com/luminik-io/alfred-os
brew install alfred-os
alfred-install
exec $SHELL                       # pick up ~/.alfredrc
gh auth login                     # GitHub auth
claude                            # Claude Code first-run auth
alfred-init                       # choose agents, repos, codenames, Slack
```

The Homebrew formula installs the latest tagged release and puts the operator
commands on your PATH: `alfred`, `alfred-init`, `alfred-install`,
`alfred-deploy`, and `alfred-doctor`. Use the source checkout path when you
want to work from `main`, edit the framework, or install on Linux.

Starter fleet for one repo or an explicit comma-separated repo list, suitable
for an AI coding tool to run end to end:

```sh
./bin/alfred-init.py \
  --non-interactive \
  --agents starter \
  --repos your-org/api,your-org/web \
  --slack-webhook skip
```

The repo owner must match `GH_ORG`; the runtime agents store the bare repo name
in `~/.alfredrc` and build `GH_ORG/repo` at firing time.

If you want Claude Code, Codex, or another local coding assistant to drive these
steps, use [`docs/AI_ASSISTED_INSTALL.md`](docs/AI_ASSISTED_INSTALL.md). It has
a copy-paste prompt with the correct guardrails for one-repo and multi-repo
setup. For checkout layout choices, read
[`docs/WORKSPACE_PATTERNS.md`](docs/WORKSPACE_PATTERNS.md).

The rest of this doc explains what each step does and what to do when something fails.

## What `install.sh` does

Idempotent (safe to re-run). On a fresh Mac or a fresh Debian/Ubuntu box:

1. Detects the host: macOS (launchd scheduling) or Debian/Ubuntu Linux (`systemd --user` scheduling). Other hosts are refused. See [`docs/LINUX.md`](docs/LINUX.md) for the Linux specifics.
2. macOS: installs Homebrew if missing. Linux: uses `apt-get`.
3. macOS: `brew install`s `git`, `gh`, `jq`, `awscli`, `python@3.11`, `node`, `uv`. Linux: `apt-get install`s the equivalents; `uv` comes from its official installer and AWS CLI v2 is left to the operator.
4. `npm install -g @anthropic-ai/claude-code`.
5. Creates `$ALFRED_HOME` (default `~/.alfred`) and `$WORKSPACE_ROOT` (default `~/code`).
6. Drops `~/.alfredrc` from `.alfredrc.example`, prompts for `GH_ORG`, `OPERATOR_NAME`, `OPERATOR_EMAIL`.
7. Appends a source-line to your shell rc (`~/.zshrc` / `~/.bashrc`) so every new shell loads `~/.alfredrc`.
8. Reports auth status for `gh`, `aws`, `claude`.

What it does **not** do (deliberately):

- Authenticate `gh` / `aws` / `claude`. Interactive flows you should see.
- Create AWS IAM users, secrets, or Slack webhooks. One-time human decisions.
- Choose which agents should run. Use `./bin/alfred-init.py` for that.
- Run `deploy.sh`. That side-effects the host scheduler (`launchd` on macOS,
  `systemd --user` on Linux); you should know what's about to load.
- Touch runtime data outside `~/.alfred`.
- Install an external agent gateway, memory database, MCP server, dashboard, or
  skill bundle. Those are optional companion integrations; see
  [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

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

Confirms `ALFRED_HOME` and `WORKSPACE_ROOT` are set in this session:

```sh
echo "$ALFRED_HOME $WORKSPACE_ROOT"
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

First-run opens a browser to authenticate against your Anthropic account. For the default setup, use Claude Code through a Pro or Max subscription login. Alfred runs `claude -p` against the CLI account you authenticated and does not require an Anthropic API key.

If `ANTHROPIC_API_KEY` is set in your shell or `~/.alfredrc`, Claude Code may prefer API billing over subscription auth. Unset it for subscription-backed Alfred runs.

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

Pressing Enter at the agent-selection step chooses the recommended starter
fleet: Drake, Lucius, Ras al Ghul, and agent-cleanup. Use `all` only when you
want the full engineering roster. If multiple repos are visible, pick the repo
numbers explicitly; the wizard no longer silently assigns every repo to every
agent.

`alfred-init.py` now does the boring setup work for you:

- Writes `launchd/agents.conf`, the shared scheduler manifest, and updates
  `~/.alfredrc`.
- Copies starter prompts from `prompts/` into `~/.alfred/prompts/<codename>.md`
  without overwriting your edits.
- Creates the standard GitHub labels on the selected repos, including
  `agent:implement`, `agent:authored`, lifecycle labels, bug-triage labels, and
  Batman's `agent:large-feature` label.
- Runs `bash deploy.sh`, then `bash bin/doctor.sh`.

Batman is included in the catalog as the opt-in architect for cross-repo work.
The default mode posts a bundle plan and stops. `BATMAN_AUTO_EXECUTE=approval-gate`
files child issues only after configured approval; `BATMAN_AUTO_EXECUTE=1`
files them immediately for teams that want that level of automation.

**When do I need Batman?**

| Your fleet has | Do you need Batman? |
| --- | --- |
| 1 repo | No. Drake + Lucius + Ras al Ghul handle single-repo work fine on their own. |
| 2 to 3 related repos (frontend + backend, or app + web + mobile) | Yes, highly recommended. The single-issue fans-out-to-N-PRs pattern is the daily payoff. |
| 5+ repos with mostly-independent work | Yes for cross-repo features. Batman no-ops on firings that find nothing cross-cutting, so the cost is the firing itself. |
| Strict approval gates required before any cross-repo work lands | Yes. Batman's `BATMAN_AUTO_EXECUTE=approval-gate` is the cleanest checkpoint pattern Alfred ships. |

Enable Batman during `alfred-init` if any of the above match, or add it later
with `alfred enable batman`. Adding it later is a no-cost change; it stays
disabled at the runner gate until you arm it.

### 7. Framework-only deploy + verify

If you want to install the framework without enabling any agents yet:

```sh
bash deploy.sh
bash bin/doctor.sh
```

`deploy.sh` copies `lib/` and `bin/` into `$ALFRED_HOME`. In a fresh checkout
there is no `launchd/agents.conf`, so this is framework-only and nothing
fires. After `alfred-init.py` creates `launchd/agents.conf`, deploy renders
host scheduler units: launchd plists on macOS, systemd user services/timers on
Linux.

`doctor.sh` runs every agent's preflight under `ALFRED_DOCTOR=1` to confirm env vars, CLI binaries, and auth chains resolve before any real firing burns Claude turns. On a clean install with the default `agents.conf` you should see `0 passed, 0 failed`.

### 8. Your first custom agent

Read `examples/bin/hello.py`, the smallest possible codename agent. Copy it to `bin/your-codename.py`, edit, add a line to `launchd/agents.conf`:

```
my.fleet.your-codename	your-codename.py	interval:3600	no	my.fleet.your-codename	Custom agent
```

`bash deploy.sh` again. `bash bin/doctor.sh` again. Should show `1 passed, 0 failed`.

Then [`BOOTSTRAP.md`](BOOTSTRAP.md) for the full pattern: per-agent IAM, Slack reporting, `agent_runner` primitives, label state machine, prompt engineering.

## Troubleshooting `install.sh`

**"Unsupported host" or "Only Debian/Ubuntu Linux is supported."**
`install.sh` runs on macOS and Debian/Ubuntu Linux. Other Linux distros are not supported by the installer, but the framework itself is distro-agnostic, so you can install the prerequisites by hand and then run `bash deploy.sh` directly. See [`docs/LINUX.md`](docs/LINUX.md).

**"npm not found; skipping Claude Code install."**
On macOS the `node` brew install brings `npm` along; on Linux it comes from the `nodejs` / `npm` apt packages. If you skipped package install (`--skip-brew`), install Node manually then re-run `install.sh` with `--skip-brew`.

**`openjdk-21-jdk` is not available on this Linux host.**
`openjdk-21-jdk` ships in Ubuntu 24.04+ and Debian 13+. `install.sh` does not install the JDK; only `needs_java=yes` agents need it. On an older release, install a JDK 21 manually and put `java` on `PATH` before running `deploy.sh`. See [`docs/LINUX.md`](docs/LINUX.md).

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
| `~/.alfred/` | Runtime root (state, worktrees, deployed bin/lib) | Yes, `deploy.sh` repopulates |
| `~/code/` | Default workspace root | If you set a different `WORKSPACE_ROOT` |
| `~/.zshrc` (or `.bashrc`) | One source-block appended | Manually edit to remove |

Everything else lives inside the cloned repo and is removed by `rm -rf ~/code/alfred-os`.

## Where to go next

- [`BOOTSTRAP.md`](BOOTSTRAP.md): AWS IAM-per-agent, Slack, prompt sync, troubleshooting.
- [`docs/INSTALL_TIERS.md`](docs/INSTALL_TIERS.md): the three install tiers (`core`, optional `client`, optional `slack`). The CLI and fleet are fully standalone; this walkthrough installs `core`.
- [`docs/AI_ASSISTED_INSTALL.md`](docs/AI_ASSISTED_INSTALL.md): assistant-driven setup with Claude Code, Codex, or another local coding assistant.
- [`docs/WORKSPACE_PATTERNS.md`](docs/WORKSPACE_PATTERNS.md): one-repo, multi-repo, specs-led, and Batman planning layouts.
- [`docs/SLACK_SETUP.md`](docs/SLACK_SETUP.md): Slack app + webhook + (optional) bot token.
- [`docs/AWS_SETUP.md`](docs/AWS_SETUP.md): IAM users, scoped policies, Secrets Manager layout.
- [`docs/CLAUDE_CODE.md`](docs/CLAUDE_CODE.md): Pro vs Max, switching accounts, `alfred claude`.
- [`docs/SKILLS.md`](docs/SKILLS.md): recommended Claude Code skills for an autonomous fleet.
- [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md): optional companion-tool boundaries.
- [`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md): issue claim lifecycle and dedup primitives.
- [`ARCHITECTURE.md`](ARCHITECTURE.md): design rationale.
- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to propose changes.
