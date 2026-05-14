"""Host-scheduler abstraction for the alfred-os operator CLI.

alfred-os schedules its agent fleet with the host's per-user scheduler:
``launchd`` on macOS, ``systemd --user`` on Linux. The two have different
binaries and unit shapes, but ``alfred pause`` / ``resume`` / ``run`` want
the same four primitives regardless of OS:

  1. is this unit currently loaded into the scheduler?
  2. unload it (the stopping half of pause)
  3. load it (the starting half of resume)
  4. kick a one-shot run, killing any in-flight firing first

On macOS those map to ``launchctl print`` / ``bootout`` / ``bootstrap`` /
``kickstart -k``. On Linux they map to ``systemctl --user`` ``is-active`` /
``disable`` / ``enable`` against the ``.timer`` (which then triggers the
``.service``), plus an explicit ``stop`` before ``start`` on the
``.service`` itself for ``kickstart -k`` semantics.

Pure stdlib. The operator reads this file when something breaks — keep it
that way.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# Host detection
# --------------------------------------------------------------------------
_SYSTEM = platform.system()
if _SYSTEM == "Darwin":
    SCHEDULER = "launchd"
    UNIT_EXT = "plist"
    UNIT_DIR = Path(
        os.environ.get("ALFRED_LAUNCH_DIR", os.path.expanduser("~/Library/LaunchAgents"))
    )
elif _SYSTEM == "Linux":
    SCHEDULER = "systemd"
    UNIT_EXT = "timer"
    UNIT_DIR = Path(
        os.environ.get("ALFRED_SYSTEMD_USER_DIR", os.path.expanduser("~/.config/systemd/user"))
    )
else:
    SCHEDULER = "none"
    UNIT_EXT = ""
    UNIT_DIR = Path()


def _uid() -> int:
    return os.getuid()


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)


# --------------------------------------------------------------------------
# Scheduler primitives
# --------------------------------------------------------------------------
def supported() -> bool:
    """True when the host has a scheduler this module can drive."""
    return SCHEDULER in ("launchd", "systemd")


def unit_file(label: str) -> Path | None:
    """Where this label's primary scheduler artifact lives on disk.

    Used to detect "not deployed yet". ``None`` on unsupported hosts.
    """
    if not supported() or not UNIT_EXT:
        return None
    return UNIT_DIR / f"{label}.{UNIT_EXT}"


def unit_loaded(label: str) -> bool:
    """Is this label currently loaded into the scheduler?"""
    if SCHEDULER == "launchd":
        return _run(["launchctl", "print", f"gui/{_uid()}/{label}"]).returncode == 0
    if SCHEDULER == "systemd":
        return (
            _run(["systemctl", "--user", "is-active", "--quiet", f"{label}.timer"]).returncode == 0
        )
    return False


def unit_unload(label: str) -> None:
    """Unload a unit (the stopping half of pause). Best-effort."""
    if SCHEDULER == "launchd":
        _run(["launchctl", "bootout", f"gui/{_uid()}/{label}"])
    elif SCHEDULER == "systemd":
        _run(["systemctl", "--user", "disable", "--now", f"{label}.timer"])


def unit_load(label: str, unit_path: Path) -> str:
    """Load a unit (the starting half of resume).

    Returns the first line of scheduler output so a bootstrap/enable failure
    surfaces its one-line reason, mirroring the launchd behavior.
    """
    if SCHEDULER == "launchd":
        res = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(unit_path)])
    elif SCHEDULER == "systemd":
        res = _run(["systemctl", "--user", "enable", "--now", f"{label}.timer"])
    else:
        return "no scheduler on this host"
    out = (res.stdout + res.stderr).strip().splitlines()
    return out[0] if out else ""


def unit_kickstart(label: str) -> str:
    """Force a one-shot run, killing any in-flight firing first.

    ``launchctl kickstart -k`` and the systemd stop+start sequence both have
    those semantics. Returns the first line of scheduler output, if any.
    """
    if SCHEDULER == "launchd":
        res = _run(["launchctl", "kickstart", "-k", f"gui/{_uid()}/{label}"])
        out = (res.stdout + res.stderr).strip().splitlines()
        return out[0] if out else ""
    if SCHEDULER == "systemd":
        # The .timer schedules the .service; a one-shot run targets the
        # .service directly. Stop first so a still-running firing is killed.
        _run(["systemctl", "--user", "stop", f"{label}.service"])
        res = _run(["systemctl", "--user", "start", f"{label}.service"])
        out = (res.stdout + res.stderr).strip().splitlines()
        return out[0] if out else ""
    return "no scheduler on this host"


def loaded_labels() -> set[str]:
    """Every agent label currently loaded into the scheduler.

    One scheduler-side scan is much cheaper than probing each label. Errors
    (e.g. the scheduler binary missing) yield an empty set so callers can
    treat "nothing loaded" uniformly.
    """
    if SCHEDULER == "launchd":
        res = _run(["launchctl", "list"])
        if res.returncode != 0:
            return set()
        labels: set[str] = set()
        for line in res.stdout.splitlines():
            parts = line.split()
            if parts:
                labels.add(parts[-1])
        return labels
    if SCHEDULER == "systemd":
        res = _run(
            [
                "systemctl",
                "--user",
                "list-units",
                "--type=timer",
                "--state=active",
                "--no-legend",
            ]
        )
        if res.returncode != 0:
            return set()
        labels = set()
        for line in res.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".timer"):
                labels.add(parts[0][: -len(".timer")])
        return labels
    return set()
