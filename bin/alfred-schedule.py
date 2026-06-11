#!/usr/bin/env python3
"""Inspect and edit Alfred agent schedules in launchd/agents.conf.

This is the safe write path for schedule changes. It edits the tab-separated
``agents.conf`` source of truth, validates the schedule grammar that
``launchd/render.sh`` understands, and tells the operator to run
``alfred deploy`` to render/reload scheduler units.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEDULE_RE = re.compile(r"^(?P<num>[1-9][0-9]*)(?P<unit>[smhd])$")
TIME_RE = re.compile(r"^(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})$")
WEEKDAYS = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}


class ScheduleError(ValueError):
    """User-fixable schedule input error."""


@dataclass(frozen=True)
class AgentSchedule:
    codename: str
    label: str
    script: str
    schedule: str
    role: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def agents_conf_path(repo: Path | None = None) -> Path:
    repo = repo.expanduser() if repo is not None else _discover_repo_root()
    return _conf_path(repo)


def _discover_repo_root() -> Path:
    raw = os.environ.get("ALFRED_REPO")
    if raw:
        return Path(raw).expanduser()

    for candidate in _repo_candidates():
        if _conf_path(candidate).exists():
            return candidate

    workspace = _workspace_root()
    return workspace / "alfred-os"


def _repo_candidates() -> list[Path]:
    candidates: list[Path] = []

    def add(path: Path) -> None:
        resolved = path.expanduser()
        if resolved not in candidates:
            candidates.append(resolved)

    for parent in (Path.cwd(), *Path.cwd().parents):
        add(parent)
    workspace = _workspace_root()
    add(workspace / "alfred-os")
    add(workspace / "alfred")
    add(workspace / "product" / "alfred")
    script_path = Path(__file__).resolve()
    for parent in (script_path, *script_path.parents):
        add(parent)
    return candidates


def _workspace_root() -> Path:
    raw = os.environ.get("WORKSPACE_ROOT", os.path.expanduser("~/code"))
    return Path(raw).expanduser()


def _conf_path(repo: Path) -> Path:
    public = repo / "launchd" / "agents.conf"
    if public.exists():
        return public
    legacy = repo / "infra" / "agents" / "launchd" / "agents.conf"
    if legacy.exists():
        return legacy
    return public


def canonical_schedule(raw: str) -> str:
    """Normalize a human schedule string to the agents.conf grammar."""
    value = re.sub(r"\s+", " ", (raw or "").strip().lower())
    if not value:
        raise ScheduleError("schedule is required")
    if value.startswith("every "):
        value = value[len("every ") :].strip()

    short = SCHEDULE_RE.match(value)
    if short:
        number = int(short.group("num"))
        unit = short.group("unit")
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return _interval(number * multiplier)

    if value.startswith("interval:"):
        seconds = _int_part(value[len("interval:") :], "interval seconds")
        return _interval(seconds)

    if value.startswith(("daily@", "daily:")):
        time_part = value.split("@", 1)[1] if "@" in value else value.split(":", 1)[1]
        hour, minute = _parse_time(time_part)
        return f"cron:{hour}:{minute:02d}"

    if value.startswith(("weekly@", "weekly:")):
        rest = value.split("@", 1)[1] if "@" in value else value.split(":", 1)[1]
        weekday_part, sep, time_part = rest.partition(":")
        if not sep:
            raise ScheduleError("weekly schedule needs a weekday and HH:MM")
        weekday = _parse_weekday(weekday_part)
        hour, minute = _parse_time(time_part)
        return f"cron:{weekday}:{hour}:{minute:02d}"

    if value.startswith("cron:"):
        return _canonical_cron(value)

    raise ScheduleError(
        "schedule must be interval:<seconds>, 10m, 2h, daily@09:00, weekly@mon:09:00, or cron:<...>"
    )


def load_schedules(path: Path) -> tuple[list[str], list[AgentSchedule]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScheduleError(f"could not read {path}: {exc}") from exc

    schedules: list[AgentSchedule] = []
    for raw in lines:
        row = _parse_conf_row(raw)
        if row is not None:
            schedules.append(row)
    return lines, schedules


def find_schedule(schedules: list[AgentSchedule], agent: str) -> AgentSchedule:
    target = _normalize_agent(agent)
    for item in schedules:
        if item.codename.lower() == target or item.label.lower() == target:
            return item
    raise ScheduleError(f"unknown agent '{agent}'")


def update_schedule(
    path: Path,
    agent: str,
    new_schedule: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    lines, schedules = load_schedules(path)
    current = find_schedule(schedules, agent)
    canonical = canonical_schedule(new_schedule)
    changed = current.schedule != canonical
    out_lines: list[str] = []
    updated = False
    for raw in lines:
        row = _parse_conf_row(raw)
        if row is None or row.label != current.label:
            out_lines.append(raw)
            continue
        fields = raw.split("\t")
        while len(fields) < 7:
            fields.append("")
        fields[2] = canonical
        out_lines.append("\t".join(fields))
        updated = True

    if not updated:
        raise ScheduleError(f"could not update {current.label}")
    if changed and not dry_run:
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    return {
        "agent": current.codename,
        "label": current.label,
        "path": str(path),
        "oldSchedule": current.schedule,
        "newSchedule": canonical,
        "changed": changed,
        "dryRun": dry_run,
    }


def _parse_conf_row(raw: str) -> AgentSchedule | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    fields = raw.split("\t")
    if len(fields) < 3:
        return None
    label = fields[0].strip()
    if not label:
        return None
    codename = _codename_from_label(label)
    return AgentSchedule(
        codename=codename,
        label=label,
        script=fields[1].strip(),
        schedule=fields[2].strip(),
        role=fields[6].strip() if len(fields) >= 7 else "",
    )


def _normalize_agent(agent: str) -> str:
    return (agent or "").strip().lower()


def _codename_from_label(label: str) -> str:
    return label.rsplit(".", 1)[-1].strip().lower() if "." in label else label.strip().lower()


def _interval(seconds: int) -> str:
    if seconds <= 0:
        raise ScheduleError("interval seconds must be positive")
    return f"interval:{seconds}"


def _canonical_cron(value: str) -> str:
    parts = value[len("cron:") :].split(":")
    if len(parts) == 2:
        hour = _int_part(parts[0], "hour")
        minute = _int_part(parts[1], "minute")
        _check_time(hour, minute)
        return f"cron:{hour}:{minute:02d}"
    if len(parts) == 3:
        weekday = _parse_weekday(parts[0])
        hour = _int_part(parts[1], "hour")
        minute = _int_part(parts[2], "minute")
        _check_time(hour, minute)
        return f"cron:{weekday}:{hour}:{minute:02d}"
    raise ScheduleError("cron schedule must be cron:HH:MM or cron:W:HH:MM")


def _parse_time(value: str) -> tuple[int, int]:
    match = TIME_RE.match(value.strip())
    if not match:
        raise ScheduleError("time must be HH:MM")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    _check_time(hour, minute)
    return hour, minute


def _check_time(hour: int, minute: int) -> None:
    if not 0 <= hour <= 23:
        raise ScheduleError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ScheduleError("minute must be 0-59")


def _parse_weekday(value: str) -> int:
    raw = str(value).strip().lower()
    if raw in WEEKDAYS:
        return WEEKDAYS[raw]
    weekday = _int_part(raw, "weekday")
    if not 0 <= weekday <= 6:
        raise ScheduleError("weekday must be 0-6, where 0 is Sunday")
    return weekday


def _int_part(value: str, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{label} must be an integer") from exc


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_list(args: argparse.Namespace) -> int:
    path = agents_conf_path(Path(args.repo).expanduser() if args.repo else None)
    _lines, schedules = load_schedules(path)
    rows = [item.to_dict() for item in schedules]
    if args.json:
        _print_json({"path": str(path), "schedules": rows})
        return 0
    for item in schedules:
        role = f"  {item.role}" if item.role else ""
        print(f"{item.codename:<28} {item.schedule:<18}{role}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = agents_conf_path(Path(args.repo).expanduser() if args.repo else None)
    _lines, schedules = load_schedules(path)
    item = find_schedule(schedules, args.agent)
    if args.json:
        _print_json({"path": str(path), "schedule": item.to_dict()})
        return 0
    print(f"{item.codename}: {item.schedule}")
    if item.role:
        print(f"  role: {item.role}")
    print(f"  config: {path}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    path = agents_conf_path(Path(args.repo).expanduser() if args.repo else None)
    result = update_schedule(
        path,
        args.agent,
        args.schedule,
        dry_run=args.dry_run,
    )
    if args.json:
        _print_json(result)
        return 0
    verb = "would update" if args.dry_run else "updated"
    if not result["changed"]:
        verb = "unchanged"
    print(
        f"alfred schedule: {verb} {result['agent']} "
        f"{result['oldSchedule']} -> {result['newSchedule']}"
    )
    print(f"  config: {result['path']}")
    if result["changed"] and not args.dry_run:
        print("  next: run `alfred deploy` to render and reload scheduler units.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred schedule",
        description="Inspect and edit Alfred agent schedules.",
    )
    parser.add_argument("--repo", help="override Alfred repo root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list configured agent schedules")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one agent schedule")
    p_show.add_argument("agent")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_set = sub.add_parser("set", help="change one agent schedule")
    p_set.add_argument("agent")
    p_set.add_argument("schedule")
    p_set.add_argument("--dry-run", action="store_true")
    p_set.add_argument("--json", action="store_true")
    p_set.set_defaults(func=cmd_set)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ScheduleError as exc:
        print(f"alfred schedule: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
