# Linux support

Short answer: not yet. Alfred-OS's scheduling layer is `launchd`, which is macOS-only. The framework code itself (`agent_runner.py`, the helper scripts) is Python and Bash and runs fine on Linux. But without a scheduling layer that mirrors `launchd`'s per-user agent semantics, the fleet isn't a fleet.

## Why launchd specifically

Per-firing isolation depends on a few `launchd` properties:

- **Per-user agents**, not system services. Operator can edit/reload plists without sudo.
- **`KeepAlive`-free fire-and-forget.** `StartInterval` / `StartCalendarInterval` triggers a one-shot run; the process exits and `launchd` is happy. No process-supervisor restarting a crashing agent in a tight loop.
- **`bootstrap` / `bootout` semantics**. Paused agents stay paused across operator login/logout cycles via the marker file at `$HERMES_HOME/state/_paused/<agent>`.
- **stdout / stderr to per-agent files** at `/tmp/<label>.{stdout,stderr}`. Operator's grep-and-tail muscle memory.
- **`EnvironmentVariables` block** in the plist. Per-agent env without polluting the operator's shell.

systemd user units cover most of this (`Type=oneshot`, `OnCalendar=`, no `Restart=on-failure`), but the operational surface is different enough that supporting both well requires a real port, not a translation layer.

## What works on Linux today

If you want to read the code, write your own agents, run the test suite, or use the `agent_runner` primitives in a manually-driven script, all of that works on Linux:

- `lib/agent_runner.py`: every primitive (preflight, lock, spend, claude_invoke, gh, slack, claim_issue/release_issue, severity routing) works unchanged.
- `tests/`: `pytest` runs the full test suite on Linux.
- `bin/doctor.sh`: works (bash + grep).
- `bin/hermes-claude`: works (symlink swapping).
- `examples/bin/label_state.py`: works.
- `examples/git-hooks/pre-push`: works (operator-side, runs in your shell).

What doesn't work:

- `launchd/render.sh`: generates `.plist` files. Linux doesn't have plists.
- `deploy.sh`: calls `launchctl bootstrap`. Fails with command-not-found.
- `install.sh` macOS check: refuses to run unless you set `ALFRED_FORCE_LINUX=1`.

## Running alfred-os-shaped agents on Linux today

Until the systemd port lands, two options:

### Option 1: cron + a wrapper script

Skip the framework's launchd bits entirely. Write each agent as a bash script:

```text
# crontab -e
*/20 * * * * /usr/bin/env HERMES_HOME=$HOME/.hermes WORKSPACE_ROOT=$HOME/code GH_ORG=myorg python3 $HOME/code/myfleet/bin/lucius.py >> /tmp/lucius.log 2>&1
```

You lose the per-agent stdout/stderr separation and the `_paused/` marker pattern, but the framework primitives all work. Shape your `bin/<codename>.py` exactly as the macOS examples show.

### Option 2: systemd user units (manually written)

Drop a unit + timer per agent in `~/.config/systemd/user/`:

```ini
# ~/.config/systemd/user/alfred-os-lucius.service
[Unit]
Description=alfred-os Lucius (feature-dev agent)

[Service]
Type=oneshot
EnvironmentFile=%h/.alfredrc
ExecStart=/usr/bin/env python3 %h/.hermes/bin/lucius.py
StandardOutput=append:%h/.hermes/logs/lucius.stdout
StandardError=append:%h/.hermes/logs/lucius.stderr
```

```ini
# ~/.config/systemd/user/alfred-os-lucius.timer
[Unit]
Description=alfred-os Lucius timer

[Timer]
OnUnitActiveSec=20min
Unit=alfred-os-lucius.service

[Install]
WantedBy=timers.target
```

Enable + start:

```sh
systemctl --user daemon-reload
systemctl --user enable --now alfred-os-lucius.timer
```

Pause:

```sh
systemctl --user disable --now alfred-os-lucius.timer
```

Status:

```sh
systemctl --user list-timers
journalctl --user -u alfred-os-lucius -n 50
```

This is what a `systemd/render.sh` would generate. Until that ships, you're hand-rolling.

## Roadmap for first-class Linux support

Not committed to a date, but the structure of the work is clear:

1. **`systemd/_template.service` + `systemd/_template.timer`**: analogous to `launchd/_template.plist`.
2. **`systemd/render.sh`**: same TSV input as `launchd/agents.conf`, different output format.
3. **`deploy.sh` host detection**: branch on `uname -s` and call the right renderer.
4. **`bin/alfred-style` wrapper**: shell helper that wraps `systemctl --user` calls in operator-friendly commands (`alfred pause` etc.).
5. **`install.sh` Linux branch**: apt/dnf/pacman package install paths instead of brew.
6. **Test the round-trip** on at least Ubuntu LTS and Fedora.

If you want to do this work, see [`CONTRIBUTING.md`](../CONTRIBUTING.md). PRs reviewed. If you want to fund the work, file an issue with your willingness to sponsor and we'll scope it together.

## WSL2

WSL2 on Windows is a Linux kernel; the same constraints apply. Cron works, systemd-user works (in distros that enable it). Not actively tested, but no part of the framework should care.

Gotcha: path mapping. `WORKSPACE_ROOT` should be a `~/code` style Linux path, not `/mnt/c/Users/...`. Cross-filesystem worktrees are slow and Windows file-locking semantics confuse `git worktree`.

## Docker

Not container-friendly today. The launchd/systemd assumption means hosting the scheduler outside the container and shelling into it for each firing, at which point you've reimplemented `launchctl kickstart` poorly. A "alfred-os in a container" pattern would need the framework to expose its own minimal scheduler and abandon the host-scheduler dependency. Not on the roadmap.

If you want to run agents inside containers (e.g. a per-firing Docker image with isolated tooling), that's compatible: write your codename's `bin/<name>.py` to `docker run --rm ... claude -p ...` instead of calling `claude` directly. The framework doesn't care what shell you wrap around the LLM call.
