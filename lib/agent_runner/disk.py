"""Disk-pressure probe for the alfred-os runtime.

This module owns one job: tell the rest of the fleet how much free space
is left on the filesystem that holds ``ALFRED_HOME``, and whether that is
``critical`` (back off now) or ``low`` (getting close).

Why it exists: scheduled agents write worktrees, transcripts, spend
ledgers, and /tmp debug dirs every firing. When the disk fills, the next
``claude`` / ``codex`` invocation hits ``ENOSPC`` and the launchd job
crash-loops — every tick burns a turn, fails, and reschedules. Nothing in
the old code path made an agent *notice* the disk was full and skip the
run. :func:`disk_pressure_status` is the primitive that lets
:func:`agent_runner.preflight` refuse cleanly instead.

Thresholds are read from the environment so an operator can tune them
without editing code:

* ``ALFRED_MIN_FREE_DISK_GB``  — absolute floor in GB (default ``3.0``).
* ``ALFRED_MIN_FREE_DISK_PCT`` — relative floor in percent (default ``5.0``).

``critical`` is True when free space is below *either* threshold;
``low`` is True when free space is within ``1.5x`` of *either* threshold
(an early-warning band that has not yet crossed into ``critical``).

What this module does NOT own:

* The decision to skip a firing -> ``orchestrator.preflight``.
* Reclaiming space -> ``bin/agent-cleanup.py``.
* Slack delivery -> ``notify.slack_post``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TypedDict

from .paths import ALFRED_HOME

# Default thresholds. Deliberately conservative: 3 GB / 5% is enough
# headroom for a worktree checkout plus a transcript stream without
# risking ENOSPC mid-firing, while still letting a mostly-full disk run.
DEFAULT_MIN_FREE_DISK_GB = 3.0
DEFAULT_MIN_FREE_DISK_PCT = 5.0

# Multiplier defining the "low" early-warning band above the critical
# thresholds. Free space within 1.5x of either floor is "low" but not
# yet "critical".
_LOW_BAND_MULTIPLIER = 1.5

_BYTES_PER_GB = 1024**3


class DiskPressure(TypedDict):
    """Result of :func:`disk_pressure_status`.

    ``free_gb`` and ``free_pct`` describe the probed filesystem;
    ``critical`` and ``low`` are the actionable booleans callers branch
    on. ``critical`` implies the firing should back off; ``low`` is an
    early warning that does not by itself stop a run.
    """

    free_gb: float
    free_pct: float
    critical: bool
    low: bool


def _min_free_gb() -> float:
    """Absolute free-space floor in GB from env, clamped to ``>= 0``."""
    return _float_env("ALFRED_MIN_FREE_DISK_GB", DEFAULT_MIN_FREE_DISK_GB)


def _min_free_pct() -> float:
    """Relative free-space floor in percent from env, clamped to ``>= 0``."""
    return _float_env("ALFRED_MIN_FREE_DISK_PCT", DEFAULT_MIN_FREE_DISK_PCT)


def _float_env(name: str, default: float) -> float:
    """Read a non-negative float knob from env; fall back on bad input.

    A typo or negative value in the launchd plist must never make the
    guardian *more* permissive in a surprising way, so the result is
    clamped to ``>= 0`` (a 0 floor simply disables that one threshold).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def disk_pressure_status(path: str | os.PathLike[str] | None = None) -> DiskPressure:
    """Report free space and pressure on the filesystem holding ``path``.

    Uses :func:`shutil.disk_usage` on ``path`` (default: ``ALFRED_HOME``,
    or its nearest existing parent so a not-yet-created home still
    probes the right device). Thresholds come from the environment via
    :func:`_min_free_gb` / :func:`_min_free_pct`.

    Returns a :class:`DiskPressure` mapping. On any OS error reading the
    filesystem the call fails *open* — it reports a healthy, non-critical
    status — so a transient stat failure can never wedge the fleet into a
    permanent skip. The real ENOSPC guard is the firing itself; this
    probe only adds an early, graceful back-off.
    """
    target = Path(path) if path is not None else ALFRED_HOME
    probe = _nearest_existing(target)

    min_gb = _min_free_gb()
    min_pct = _min_free_pct()

    try:
        usage = shutil.disk_usage(str(probe))
    except OSError:
        # Fail open: never let a stat hiccup masquerade as a full disk.
        return DiskPressure(free_gb=float("inf"), free_pct=100.0, critical=False, low=False)

    free_gb = usage.free / _BYTES_PER_GB
    free_pct = (usage.free / usage.total * 100.0) if usage.total else 100.0

    below_gb = free_gb < min_gb
    below_pct = free_pct < min_pct
    critical = below_gb or below_pct

    low_gb = free_gb < min_gb * _LOW_BAND_MULTIPLIER
    low_pct = free_pct < min_pct * _LOW_BAND_MULTIPLIER
    low = (low_gb or low_pct) and not critical

    return DiskPressure(
        free_gb=round(free_gb, 2),
        free_pct=round(free_pct, 2),
        critical=critical,
        low=low,
    )


def _nearest_existing(path: Path) -> Path:
    """Return ``path`` or its nearest existing ancestor.

    ``shutil.disk_usage`` raises on a missing path. ALFRED_HOME may not
    exist yet on a fresh host, but its parent (the home directory) lives
    on the same device we actually care about, so walk up until we find
    something to stat.
    """
    candidate = path
    for _ in range(64):  # bounded: filesystem depth is never this deep
        if candidate.exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate
