# Linux support

Alfred runs on Linux. The scheduling layer is `systemd --user` timers, the
Linux analogue of macOS `launchd` per-user agents. `install.sh`, `deploy.sh`,
`bin/doctor.sh`, `alfred-status`, and the `alfred` CLI all detect the host OS
and pick the right path. Supported distros: Debian and Ubuntu (apt).

If you are on macOS, you do not need this doc; `install.sh` handles it.

## How the launchd / systemd mapping works

`launchd` and `systemd --user` cover the same operational surface; the port
is a real mapping, not a translation shim:

| launchd | systemd --user | Purpose |
|---|---|---|
| `~/Library/LaunchAgents/<label>.plist` | `~/.config/systemd/user/<label>.{service,timer}` | per-agent unit on disk |
| `StartInterval` / `StartCalendarInterval` | `OnUnitActiveSec=` / `OnCalendar=` | the schedule |
| `RunAtLoad=false` + one-shot exit | `Type=oneshot` | fire-and-forget, no supervisor restart loop |
| `launchctl bootstrap` / `bootout` | `systemctl --user enable --now` / `disable --now` | load / unload a unit |
| `launchctl kickstart -k` | `systemctl --user stop` then `start` on the `.service` | one-shot run, killing any in-flight firing |
| `StandardOutPath` / `StandardErrorPath` | `StandardOutput=append:` / `StandardError=append:` | per-agent stdout/stderr to `/tmp/<stem>.{stdout,stderr}` |
| `EnvironmentVariables` block | `Environment=` lines | per-agent env without polluting the operator shell |

`agents.conf` is the single source of truth for both schedulers: the same
six tab-separated columns (`label`, `script`, `schedule`, `needs_java`,
`log_stem`, `role`) feed `launchd/render.sh` and `systemd/render.sh`.

The rendered systemd units use systemd's `%h` specifier in place of the
operator's literal home directory, so a unit file is host-agnostic.

## Install

```sh
git clone https://github.com/luminik-io/alfred-os.git ~/code/alfred-os
cd ~/code/alfred-os
bash install.sh
```

`install.sh` on Linux:

1. Confirms the host is Debian/Ubuntu (reads `/etc/os-release`).
2. `apt-get install`s `git`, `gh`, `jq`, `python3-venv`, `python3-pip`,
   `nodejs`, `npm`, plus `ca-certificates` / `curl` / `gnupg`.
3. Installs `uv` from the official installer (no apt package), and uses it to
   provision Python 3.11 if the distro ships a newer default.
4. `npm install -g @anthropic-ai/claude-code`.
5. Creates `$ALFRED_HOME` and `$WORKSPACE_ROOT`, and seeds `$ALFRED_HOME/.env`
   for the scheduler, CLI, and native app to load directly.

AWS CLI v2 is **not** auto-installed: apt's `awscli` is v1.x, and scheduled
fleet jobs that touch AWS want v2. Install it manually from Amazon if you
need it.

`gh` falls back to GitHub's official apt repo when the distro does not ship
a `gh` package.

## Deploy

```sh
bash deploy.sh
```

On Linux, `deploy.sh`:

1. Copies `lib/` and `bin/` into `$ALFRED_HOME`, links `alfred` (and
   `claude` / `codex` if present) into `~/.local/bin`.
2. Renders `systemd/` units from `launchd/agents.conf` into
   `systemd/_generated/`.
3. Reaps units for `agents.conf` rows that were removed.
4. Copies the units into `~/.config/systemd/user/`, runs
   `systemctl --user daemon-reload`, and `enable --now`s each timer,
   skipping any agent whose pause marker is set at
   `$ALFRED_HOME/state/_paused/<codename>`.

## Operating the fleet

The `alfred` CLI is OS-agnostic; the same verbs work on Linux:

```sh
alfred agents              # roster, with a systemd-load column
alfred pause lucius        # disable --now the timer, write the pause marker
alfred resume lucius       # clear the marker, enable --now the timer
alfred run lucius          # one-shot: stop + start the .service now
alfred status              # health snapshot; reads the systemd timer roster
alfred doctor         # preflight every agent
```

Raw `systemctl` still works if you prefer it:

```sh
systemctl --user list-timers
systemctl --user status alfred.lucius.timer
journalctl --user -u alfred.lucius -n 50
```

Note that agents also write to `/tmp/<log_stem>.{stdout,stderr}` (the
`StandardOutput=append:` lines), so the macOS grep-and-tail muscle memory
carries over.

## `linger`: keeping the fleet alive across logout

`systemd --user` units only run while the user has an active session unless
**linger** is enabled. For an always-on agent host, enable it once:

```sh
sudo loginctl enable-linger "$USER"
```

Without linger, the timers stop when you log out and resume when you log
back in. With it, they run continuously like macOS `launchd` agents. This is
the one piece `deploy.sh` does **not** do for you. It needs `sudo` and is a
deliberate operator decision.

## Java agents

Agents with `needs_java=yes` in `agents.conf` need a JDK 21 on the host.
`systemd/render.sh` derives `JAVA_HOME` from `command -v java`, falling back
to the Debian/Ubuntu `openjdk-21` layout under `/usr/lib/jvm`. Install it
with:

```sh
sudo apt-get install -y openjdk-21-jdk
```

`openjdk-21-jdk` ships in Ubuntu 24.04+ and Debian 13+. On older releases,
install a JDK 21 manually and put `java` on `PATH`. If a `needs_java=yes`
agent renders with no JDK found, `render.sh` warns and omits `JAVA_HOME`
rather than failing the whole render.

## WSL2

WSL2 on Windows is a Linux kernel; the same path applies. `systemd --user`
works in distros that enable systemd (Ubuntu on WSL2 does by default on
recent builds). Not actively tested in CI, but no part of the framework
should care.

Gotcha: path mapping. `WORKSPACE_ROOT` should be a `~/code`-style Linux
path, not `/mnt/c/Users/...`. Cross-filesystem worktrees are slow and
Windows file-locking semantics confuse `git worktree`.

## Docker

Still not container-friendly today. The launchd/systemd assumption means
hosting the scheduler outside the container and shelling in for each firing,
at which point you have reimplemented the host scheduler poorly. A
"Alfred in a container" pattern would need the framework to expose its
own minimal scheduler and abandon the host-scheduler dependency. Not on the
roadmap.

If you want to run agents inside containers (e.g. a per-firing Docker image
with isolated tooling), that is compatible: write your codename's
`bin/<name>.py` to `docker run --rm ... claude -p ...` instead of calling
`claude` directly. The framework does not care what shell you wrap around
the LLM call.

## Anything else not working on Linux?

The framework primitives in `lib/agent_runner/` (preflight, lock, spend,
claude_invoke, gh, slack, claim_issue/release_issue, severity routing) are
plain Python and Bash and have always run on Linux. `tests/` runs the full
`pytest` suite on Linux CI. If you hit a Linux-specific bug, file an issue.
Linux is a supported host now, so Linux bugs are real bugs.

`alfred claude` works on Linux too. It switches the Claude Code account by
setting `CLAUDE_CONFIG_DIR` in the `systemd --user` manager environment with
`systemctl --user set-environment`. Already-running services keep the
environment they started with, so restart the affected timer or service after a
switch:

```sh
alfred claude secondary
systemctl --user restart alfred.lucius.timer
```

If you prefer static routing, set `CLAUDE_CONFIG_DIR` directly in
`$ALFRED_HOME/.env`; it flows into rendered units through `agent-launch`.
