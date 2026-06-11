"""Read upcoming scheduled runs from ``launchd/agents.conf``.

``agents.conf`` is the single source of truth for the launchd fleet
(see ``launchd/agents.conf`` and ``render.sh``). Each
non-comment row carries a tab-separated schedule field in one of three
shapes that ``render.sh`` turns into a launchd ``StartInterval`` /
``StartCalendarInterval`` block:

    interval:<seconds>            every N seconds
    cron:<HH>:<MM>                daily at HH:MM (local)
    cron:<weekday>:<HH>:<MM>      weekly on weekday (0=Sun) at HH:MM

This module parses those rows into :class:`ScheduledRun` records and, where
it can do so reliably, computes the next fire time. ``interval:`` rows have
no on-disk "last fired" anchor the read-only server can trust, so they
report a cadence string ("every 15m") instead of a guessed timestamp. ``cron:``
rows have a deterministic next-fire that this module computes from the local
clock.

Pure stdlib. Mirrors the grammar in ``render.sh``; if that grammar grows a
new shape, add it in both places.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Weekday names indexed by launchd's convention (0 = Sunday). Used only to
# render a human cadence string; the next-fire math uses Python weekday math.
_WEEKDAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


@dataclass(frozen=True)
class ScheduledRun:
    """One agent's schedule, as read from ``agents.conf``.

    ``next_fire_at`` is an ISO-8601 local timestamp when this module can
    compute it reliably (``cron:`` rows). For ``interval:`` rows it is
    ``None`` and the caller should show ``cadence`` instead, because the
    read-only server has no trustworthy "last fired" anchor to add the
    interval to.
    """

    codename: str
    role: str
    kind: str  # "interval" | "cron-daily" | "cron-weekly"
    cadence: str  # human string, e.g. "every 15m", "daily 07:00"
    next_fire_at: str | None
    raw_schedule: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def agents_conf_path() -> Path | None:
    """Resolve ``launchd/agents.conf``, or ``None`` if absent.

    An explicit ``ALFRED_REPO`` wins. Otherwise prefer the installed runtime
    home (``ALFRED_HOME`` / ``HERMES_HOME``), because ``deploy.sh`` copies the
    launchd config there. Source-checkout fallbacks remain for development.
    """
    alfred_repo = os.environ.get("ALFRED_REPO")
    if alfred_repo:
        return _first_agents_conf(Path(alfred_repo))

    runtime_home = os.environ.get("ALFRED_HOME") or os.environ.get("HERMES_HOME")
    if runtime_home:
        deployed = _first_agents_conf(Path(runtime_home))
        if deployed is not None:
            return deployed

    workspace = os.environ.get("WORKSPACE_ROOT", os.path.expanduser("~/code"))
    return _first_agents_conf(Path(workspace) / "alfred-os")


def _first_agents_conf(base: Path) -> Path | None:
    for conf in (
        base / "launchd" / "agents.conf",
        base / "infra" / "agents" / "launchd" / "agents.conf",
    ):
        if conf.is_file():
            return conf
    return None


def upcoming_runs(
    conf_path: Path | None = None,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[ScheduledRun]:
    """Parse ``agents.conf`` into the fleet's scheduled runs.

    Returns an empty list (never raises) when the file is missing or
    unreadable so the dashboard degrades to an honest empty state. ``cron:``
    rows are sorted by their computed next-fire (soonest first); ``interval:``
    rows have no timestamp and sort after them, ordered by codename.
    """
    path = conf_path if conf_path is not None else agents_conf_path()
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("could not read agents.conf %s: %s", path, exc)
        return []

    reference = now or datetime.now()
    runs: list[ScheduledRun] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        run = _parse_row(raw_line, reference=reference)
        if run is not None:
            runs.append(run)

    # Soonest next-fire first; interval rows (no timestamp) fall to the end,
    # ordered by codename so the list is stable across polls.
    runs.sort(key=lambda r: (r.next_fire_at is None, r.next_fire_at or "", r.codename))
    return runs[: max(1, limit)]


def _parse_row(raw_line: str, *, reference: datetime) -> ScheduledRun | None:
    fields = raw_line.split("\t")
    if len(fields) < 3:
        return None
    label = fields[0].strip()
    if not label:
        return None
    schedule = fields[2].strip()
    role = fields[6].strip() if len(fields) >= 7 else ""
    codename = _codename_from_label(label)

    if schedule.startswith("interval:"):
        seconds = _coerce_int(schedule[len("interval:") :])
        if seconds is None or seconds <= 0:
            return None
        return ScheduledRun(
            codename=codename,
            role=role,
            kind="interval",
            cadence=_interval_cadence(seconds),
            # No trustworthy "last fired" anchor on the read-only server, so
            # show the cadence rather than a guessed (likely wrong) timestamp.
            next_fire_at=None,
            raw_schedule=schedule,
        )

    if schedule.startswith("cron:"):
        parts = schedule[len("cron:") :].split(":")
        if len(parts) == 2:
            hour, minute = _coerce_int(parts[0]), _coerce_int(parts[1])
            if hour is None or minute is None:
                return None
            next_fire = _next_daily(reference, hour, minute)
            return ScheduledRun(
                codename=codename,
                role=role,
                kind="cron-daily",
                cadence=f"daily {hour:02d}:{minute:02d}",
                next_fire_at=next_fire.isoformat(timespec="seconds"),
                raw_schedule=schedule,
            )
        if len(parts) == 3:
            weekday = _coerce_int(parts[0])
            hour, minute = _coerce_int(parts[1]), _coerce_int(parts[2])
            if weekday is None or hour is None or minute is None:
                return None
            if not 0 <= weekday <= 6:
                return None
            next_fire = _next_weekly(reference, weekday, hour, minute)
            day_name = _WEEKDAY_NAMES[weekday]
            return ScheduledRun(
                codename=codename,
                role=role,
                kind="cron-weekly",
                cadence=f"{day_name} {hour:02d}:{minute:02d}",
                next_fire_at=next_fire.isoformat(timespec="seconds"),
                raw_schedule=schedule,
            )

    # Unknown shape: skip rather than guess.
    logger.debug("unrecognized schedule %r for %s", schedule, codename)
    return None


def _codename_from_label(label: str) -> str:
    return label.rsplit(".", 1)[-1].strip().lower() if "." in label else label.strip().lower()


def _interval_cadence(seconds: int) -> str:
    """Render a launchd interval as a compact human cadence string."""
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"every {days}d" if days > 1 else "every 24h"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours}h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes}m"
    return f"every {seconds}s"


def _next_daily(reference: datetime, hour: int, minute: int) -> datetime:
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= reference:
        candidate += timedelta(days=1)
    return candidate


def _next_weekly(reference: datetime, launchd_weekday: int, hour: int, minute: int) -> datetime:
    # launchd uses 0=Sunday..6=Saturday; Python's weekday() uses
    # 0=Monday..6=Sunday. Map launchd -> Python: (launchd - 1) % 7.
    target_py_weekday = (launchd_weekday - 1) % 7
    candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (target_py_weekday - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= reference:
        candidate += timedelta(days=7)
    return candidate


def _coerce_int(value: str) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
