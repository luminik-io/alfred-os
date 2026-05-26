#!/usr/bin/env python3
"""``alfred brain`` — operator CLI for the fleet-brain memory layer.

Subcommands:

    alfred-brain.py status
        Print row counts, db path, and schema version.

    alfred-brain.py lessons <codename> <repo> [--query Q] [--limit N]
        List recall-able lessons most-recent first. Either positional
        may be ``-`` to widen the scope (e.g. all lessons for one repo
        across every codename: ``alfred-brain.py lessons - your-org/api``).

    alfred-brain.py reflect <codename> <repo> <body>
        Manually file a lesson from the shell. Useful for seeding the
        brain with operator knowledge before any agent has fired.

    alfred-brain.py firings [--codename C] [--status S] [--limit N]
        List firing audit rows.

    alfred-brain.py files <repo> [--codename C] [--path P] [--limit N]
        List recent files the fleet touched in a repo.

    alfred-brain.py forget <id>
        Delete one lesson by id. Use ``alfred-brain.py forget --before 30d``
        to GC anything older than 30 days.

    alfred-brain.py export [--out PATH]
        Write a JSON snapshot to PATH (default: stdout).

Pure stdlib. The brain is local-only: this CLI never makes a network
call. If something here writes outside ``$ALFRED_FLEET_BRAIN_DB`` /
``$ALFRED_HOME``, it's a bug.

The wrapper ``bin/alfred`` exposes this as ``alfred brain status``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC
from pathlib import Path

# Resolve lib/ relative to this script regardless of how it was invoked.
_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fleet_brain import FleetBrain, default_db_path  # noqa: E402


def _build_brain(args: argparse.Namespace) -> FleetBrain:
    db_path = args.db or os.environ.get("ALFRED_FLEET_BRAIN_DB")
    return FleetBrain(db_path=db_path) if db_path else FleetBrain()


def cmd_status(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    s = brain.stats()
    db_path = args.db or os.environ.get("ALFRED_FLEET_BRAIN_DB") or str(default_db_path())
    print(f"alfred-brain: db = {db_path}")
    print(f"  lessons     {s['lessons']}")
    print(f"  firings     {s['firings']}")
    print(f"  file_touches {s['file_touches']}")
    print(f"  repo_notes  {s['repo_notes']}")
    print(f"  tags        {s['tags']}")
    print(f"  codenames   {s['codenames']}")
    print(f"  repos       {s['repos']}")
    return 0


def cmd_lessons(args: argparse.Namespace) -> int:
    codename = None if args.codename == "-" else args.codename
    repo = None if args.repo == "-" else args.repo
    brain = _build_brain(args)
    lessons = brain.recall(codename=codename, repo=repo, query=args.query, limit=args.limit)
    if args.json:
        payload = [
            {
                "id": L.id,
                "codename": L.codename,
                "repo": L.repo,
                "body": L.body,
                "tags": L.tags,
                "severity": L.severity,
                "firing_id": L.firing_id,
                "created_at": L.created_at.astimezone(UTC).isoformat(),
            }
            for L in lessons
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not lessons:
        print("alfred-brain: no lessons match", file=sys.stderr)
        return 0
    for L in lessons:
        ts = L.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        tag_str = ("[" + ",".join(L.tags) + "] ") if L.tags else ""
        sev_str = "" if L.severity == "info" else f"({L.severity}) "
        print(f"{L.id}  {ts}  {L.codename}/{L.repo}")
        print(f"  {sev_str}{tag_str}{L.body}")
    return 0


def cmd_reflect(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    tags = [t.strip() for t in (args.tag or [])]
    lesson = brain.reflect(
        codename=args.codename,
        repo=args.repo,
        body=args.body,
        tags=tags,
        severity=args.severity,
        firing_id=args.firing_id,
    )
    print(f"alfred-brain: reflected lesson {lesson.id}")
    return 0


def cmd_firings(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    firings = brain.list_firings(
        codename=args.codename,
        status=args.status,
        limit=args.limit,
    )
    if not firings:
        print("alfred-brain: no firings recorded", file=sys.stderr)
        return 0
    for F in firings:
        started = F.started_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        repo_str = f" {F.repo}" if F.repo else ""
        pr_str = f" {F.pr_url}" if F.pr_url else ""
        print(f"{F.firing_id}  {started}  {F.codename}{repo_str}  status={F.status}{pr_str}")
        if F.summary:
            print(f"  {F.summary}")
    return 0


def cmd_files(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    touches = brain.list_file_touches(
        repo=None if args.repo == "-" else args.repo,
        codename=args.codename,
        path=args.path,
        limit=args.limit,
    )
    if args.json:
        payload = [
            {
                "id": T.id,
                "repo": T.repo,
                "path": T.path,
                "codename": T.codename,
                "firing_id": T.firing_id,
                "pr_url": T.pr_url,
                "change_type": T.change_type,
                "touched_at": T.touched_at.astimezone(UTC).isoformat(),
            }
            for T in touches
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not touches:
        print("alfred-brain: no file touches recorded", file=sys.stderr)
        return 0
    for T in touches:
        touched = T.touched_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        pr_str = f" {T.pr_url}" if T.pr_url else ""
        firing_str = f" firing={T.firing_id}" if T.firing_id else ""
        print(f"{touched}  {T.codename}/{T.repo}  {T.change_type}  {T.path}{firing_str}{pr_str}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    if args.before:
        days = _parse_duration_days(args.before)
        if days is None:
            print(f"alfred-brain: cannot parse --before {args.before!r}", file=sys.stderr)
            return 2
        deleted = brain.forget_before(days=days)
        print(f"alfred-brain: deleted {deleted} lesson(s) older than {days}d")
        return 0
    if not args.id:
        print("alfred-brain: forget needs an id, or --before <duration>", file=sys.stderr)
        return 2
    ok = brain.forget(args.id)
    if ok:
        print(f"alfred-brain: forgot {args.id}")
        return 0
    print(f"alfred-brain: no lesson with id {args.id}", file=sys.stderr)
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    brain = _build_brain(args)
    payload = brain.export()
    text = json.dumps(payload, indent=2, default=str)
    if args.out:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"alfred-brain: exported {len(payload['lessons'])} lesson(s) to {out_path}")
        return 0
    print(text)
    return 0


def _parse_duration_days(value: str) -> int | None:
    """Accept ``30d``, ``30``, ``2w``, ``6h`` (rounded down). Returns days."""
    m = re.fullmatch(r"\s*(\d+)\s*([dwh]?)\s*", value.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "d"
    if unit == "d":
        return n
    if unit == "w":
        return n * 7
    if unit == "h":
        # Hour granularity rounded down to days; one-hour TTL doesn't
        # make sense for a memory layer.
        return max(0, n // 24)
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-brain",
        description="Operator CLI for the fleet-brain memory layer.",
    )
    p.add_argument(
        "--db",
        help="Path to the SQLite brain file. Defaults to "
        "$ALFRED_FLEET_BRAIN_DB or $ALFRED_HOME/fleet-brain.db.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="row counts and db path")
    p_status.set_defaults(func=cmd_status)

    p_lessons = sub.add_parser("lessons", help="recall lessons for a codename / repo")
    p_lessons.add_argument("codename", help="codename or '-' to widen")
    p_lessons.add_argument("repo", help="repo full_name or '-' to widen")
    p_lessons.add_argument("--query", help="literal substring filter on body")
    p_lessons.add_argument("--limit", type=int, default=20)
    p_lessons.add_argument("--json", action="store_true")
    p_lessons.set_defaults(func=cmd_lessons)

    p_reflect = sub.add_parser("reflect", help="file a lesson from the shell")
    p_reflect.add_argument("codename")
    p_reflect.add_argument("repo")
    p_reflect.add_argument("body")
    p_reflect.add_argument("--tag", action="append", help="tag (repeatable)")
    p_reflect.add_argument("--severity", choices=["info", "warning", "blocker"], default="info")
    p_reflect.add_argument("--firing-id", dest="firing_id")
    p_reflect.set_defaults(func=cmd_reflect)

    p_firings = sub.add_parser("firings", help="list firing audit rows")
    p_firings.add_argument("--codename")
    p_firings.add_argument("--status", choices=["ok", "blocked", "partial", "silent"])
    p_firings.add_argument("--limit", type=int, default=20)
    p_firings.set_defaults(func=cmd_firings)

    p_files = sub.add_parser("files", help="list recent file touches")
    p_files.add_argument("repo", help="repo full_name or '-' to widen")
    p_files.add_argument("--codename")
    p_files.add_argument("--path", help="exact repo-relative path")
    p_files.add_argument("--limit", type=int, default=50)
    p_files.add_argument("--json", action="store_true")
    p_files.set_defaults(func=cmd_files)

    p_forget = sub.add_parser("forget", help="delete a lesson or GC old ones")
    p_forget.add_argument("id", nargs="?", help="lesson id to delete")
    p_forget.add_argument("--before", help="GC older than e.g. '30d', '2w'")
    p_forget.set_defaults(func=cmd_forget)

    p_export = sub.add_parser("export", help="JSON snapshot of the brain")
    p_export.add_argument("--out", help="write to PATH instead of stdout")
    p_export.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("ALFRED_BRAIN_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
