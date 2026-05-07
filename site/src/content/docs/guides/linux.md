---
title: Linux
description: Current macOS-only stance, what works on Linux today, the systemd port roadmap.
---

Short answer: not yet. Alfred-OS's scheduling layer is `launchd`, which is macOS-only.

Full doc at [`docs/LINUX.md`](https://github.com/luminik-io/alfred-os/blob/main/docs/LINUX.md). Highlights:

## What works on Linux today

- `lib/agent_runner.py`: every primitive runs unchanged.
- `tests/`: `pytest` runs the full test suite.
- `bin/doctor.sh`, `bin/hermes-claude`: work.
- `examples/bin/label_state.py`, `examples/git-hooks/pre-push`: work.

## What doesn't

- `launchd/render.sh`, `deploy.sh`: depend on `launchctl`.
- `install.sh`: refuses to run unless you set `ALFRED_FORCE_LINUX=1`.

## Two interim options

### Option 1: cron + a wrapper script

```text
*/20 * * * * /usr/bin/env HERMES_HOME=$HOME/.hermes WORKSPACE_ROOT=$HOME/code GH_ORG=myorg python3 $HOME/code/myfleet/bin/lucius.py >> /tmp/lucius.log 2>&1
```

You lose per-agent stdout/stderr separation and the `_paused/` marker pattern, but the framework primitives all work.

### Option 2: hand-rolled systemd user units

```ini
# ~/.config/systemd/user/alfred-os-lucius.service
[Unit]
Description=alfred-os Lucius

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

Enable: `systemctl --user enable --now alfred-os-lucius.timer`.

This is what a `systemd/render.sh` would generate. Until that ships, you're hand-rolling.

## Roadmap for first-class Linux support

See [Roadmap](/alfred-os/about/roadmap/). The structure of the work:

1. `systemd/_template.service` + `systemd/_template.timer`.
2. `systemd/render.sh` mirroring `launchd/render.sh`.
3. `deploy.sh` host detection.
4. `install.sh` Linux branch (apt/dnf/pacman).
5. Round-trip test on Ubuntu LTS + Fedora.

If you want to do this work, see [Contributing](/alfred-os/about/contributing/). PRs reviewed. If you want to fund it, file an issue with your willingness to sponsor.

## WSL2 and Docker

Both work for the framework code; neither is actively tested.

- **WSL2**: same as Linux. Cron or systemd-user. Watch out for cross-filesystem worktree slowness if you mount Windows drives.
- **Docker**: alfred-os is not container-friendly. The host-scheduler dependency would need a real port.

If you want to run agents inside containers (a per-firing image with isolated tooling), that's compatible: write your codename's `bin/<name>.py` to `docker run --rm ... claude -p ...`. The framework doesn't care.
