#!/usr/bin/env python3
"""``alfred-metrics``: weekly per-agent firings/cost/turns/tool-use roll-up.

Reads three on-disk state directories under ``$ALFRED_STATE_DIR``
(default ``$ALFRED_HOME/state``, default ``~/.alfred/state``):

* ``<codename>/spend-YYYY-MM-DD.json``: per-day SpendState files
* ``transcripts/<codename>/<YYYY-MM>/*.jsonl``: stream-JSON firings
* ``codex/<codename>/<YYYY-MM>/*.stdout.txt``: Codex run stdout dumps

Answers the operator's recurring questions:

* Who is burning the most turns / cost?
* Who is failing without producing PRs?
* Which skills are actually being used?
* Which tools get spammed (pointing at prompt-tightening opportunities)?

Exit codes:
  0 success
  1 user error (bad ``--since``, unknown codename)
  2 system error (state dir missing or unreadable)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Make ``lib/`` importable whether this script is run from the repo
# checkout or from ``$ALFRED_HOME/bin``.
_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from metrics import (  # noqa: E402
    FleetReport,
    discover_codenames,
    fleet_metrics,
    parse_since,
)
from transcripts import default_state_dir  # noqa: E402

logger = logging.getLogger("alfred-metrics")


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def render_table(report: FleetReport) -> str:
    lines: list[str] = []
    ts = report.generated_at.strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"alfred-metrics - last {report.days} days @ {ts}")
    lines.append("")
    header = (
        f"{'codename':<22} {'firings':<8} {'ok':<5} {'fail':<5} {'turns':<7} "
        f"{'codex':<6} {'ctok':<8} {'cost':<8} {'tools':<7} {'edits':<6} {'reads':<6} "
        f"{'top tool':<22} {'skills'}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    totals = {
        "firings": 0,
        "ok": 0,
        "fail": 0,
        "turns": 0,
        "cost": 0.0,
        "tools": 0,
        "edits": 0,
        "reads": 0,
        "codex": 0,
        "ctok": 0,
    }
    rows_shown = 0
    for m in report.metrics:
        if m.is_empty():
            continue
        rows_shown += 1
        top_tool = ""
        if m.tool_calls:
            name, count = max(m.tool_calls.items(), key=lambda kv: kv[1])
            top_tool = f"{name}x{count}"
        skill_str = ", ".join(
            f"{k}x{v}" for k, v in sorted(m.skills.items(), key=lambda kv: -kv[1])[:3]
        )
        lines.append(
            f"{m.codename:<22} "
            f"{m.spend.firings:<8} "
            f"{m.spend.successes:<5} "
            f"{m.spend.failures:<5} "
            f"{m.spend.turns:<7} "
            f"{m.codex_runs:<6} "
            f"{m.codex_tokens:<8} "
            f"${m.spend.cost_usd:<7.2f} "
            f"{m.tool_calls_total:<7} "
            f"{m.files_edited:<6} "
            f"{m.files_read:<6} "
            f"{top_tool:<22} "
            f"{skill_str or '-'}"
        )
        totals["firings"] += m.spend.firings
        totals["ok"] += m.spend.successes
        totals["fail"] += m.spend.failures
        totals["turns"] += m.spend.turns
        totals["cost"] += m.spend.cost_usd
        totals["codex"] += m.codex_runs
        totals["ctok"] += m.codex_tokens
        totals["tools"] += m.tool_calls_total
        totals["edits"] += m.files_edited
        totals["reads"] += m.files_read

    if not rows_shown:
        lines.append("(no firings or transcripts in window)")
    else:
        lines.append("-" * len(header))
        lines.append(
            f"{'TOTAL':<22} "
            f"{totals['firings']:<8} {totals['ok']:<5} {totals['fail']:<5} "
            f"{totals['turns']:<7} {totals['codex']:<6} {totals['ctok']:<8} "
            f"${totals['cost']:<7.2f} "
            f"{totals['tools']:<7} {totals['edits']:<6} {totals['reads']:<6}"
        )
        completed = totals["ok"] + totals["fail"]
        lines.append("")
        if completed:
            rate = totals["ok"] / completed * 100
            noops = totals["firings"] - completed
            lines.append(
                f"fleet success rate: {rate:.1f}%  "
                f"(over {completed} completed firings; {noops} no-ops not counted)"
            )
        elif totals["firings"]:
            lines.append(
                f"fleet success rate: pending  "
                f"({totals['firings']} firings, none yet labelled ok/fail)"
            )

    return "\n".join(lines)


def render_by_day(report: FleetReport, state_dir: Path) -> str:
    """Daily breakdown across the window. Reads spend files directly."""
    from collections import defaultdict
    from datetime import datetime, timedelta

    days_map: dict[str, dict[str, float]] = defaultdict(
        lambda: {"firings": 0, "ok": 0, "fail": 0, "turns": 0, "cost": 0.0}
    )
    cutoff = datetime.now().date() - timedelta(days=max(0, report.days - 1))
    if state_dir.is_dir():
        for entry in state_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            for path in entry.glob("spend-*.json"):
                try:
                    day = datetime.strptime(path.stem.replace("spend-", ""), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff:
                    continue
                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                key = day.isoformat()
                days_map[key]["firings"] += int(data.get("firings_today") or 0)
                days_map[key]["ok"] += int(data.get("successes_today") or 0)
                days_map[key]["fail"] += int(data.get("failures_today") or 0)
                days_map[key]["turns"] += int(data.get("turns_today") or 0)
                days_map[key]["cost"] += float(data.get("cost_usd_today") or 0)

    lines = [
        f"alfred-metrics --by-day - last {report.days} days",
        "",
        f"{'date':<12} {'firings':<8} {'ok':<5} {'fail':<5} {'turns':<7} {'cost':<8}",
        "-" * 50,
    ]
    if not days_map:
        lines.append("(no spend files in window)")
        return "\n".join(lines)
    for key in sorted(days_map):
        v = days_map[key]
        lines.append(
            f"{key:<12} {int(v['firings']):<8} {int(v['ok']):<5} {int(v['fail']):<5} "
            f"{int(v['turns']):<7} ${v['cost']:<7.2f}"
        )
    return "\n".join(lines)


def render_json(report: FleetReport) -> str:
    payload = report.to_dict()
    # Drop empty rows in JSON too, matching the table behaviour.
    payload["metrics"] = [
        m for m in payload["metrics"] if m["spend"]["firings"] or m["tool_calls_total"]
    ]
    return json.dumps(payload, indent=2, default=str)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-metrics",
        description="Roll up agent firings/cost/turns/tool-use across a window.",
    )
    p.add_argument(
        "--since",
        default="7d",
        help="window size: 7, 7d, 48h, 2w, 1m (default: 7d)",
    )
    p.add_argument(
        "--days",
        type=int,
        help="window size in days (overrides --since)",
    )
    p.add_argument(
        "--codename",
        action="append",
        help="restrict to one codename (repeatable)",
    )
    p.add_argument(
        "--by-agent",
        action="store_true",
        help="default view: one row per agent",
    )
    p.add_argument(
        "--by-day",
        action="store_true",
        help="break down totals by day instead of by agent",
    )
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument(
        "--json", action="store_true", dest="json_out", help="machine-readable JSON output"
    )
    fmt.add_argument("--table", action="store_true", help="tabular text output (default)")
    p.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="override state directory (default: $ALFRED_STATE_DIR or $ALFRED_HOME/state)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(message)s",
    )

    state_dir = args.state_dir or default_state_dir()
    if not state_dir.exists():
        print(
            f"alfred-metrics: state directory {state_dir} does not exist. "
            "Set ALFRED_STATE_DIR or run the fleet at least once.",
            file=sys.stderr,
        )
        return 2

    try:
        days = args.days if args.days is not None else parse_since(args.since)
    except ValueError as exc:
        print(f"alfred-metrics: {exc}", file=sys.stderr)
        return 1

    if args.codename:
        known = set(discover_codenames(state_dir))
        unknown = [c for c in args.codename if c not in known]
        if unknown and not known:
            # State dir has nothing yet; still try, empty rows will surface.
            logger.debug("no codenames discovered under %s", state_dir)
        elif unknown:
            print(
                f"alfred-metrics: unknown codename(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(known)) or '(none yet)'}",
                file=sys.stderr,
            )
            return 1
        report = fleet_metrics(state_dir, days=days, codenames=args.codename)
    else:
        report = fleet_metrics(state_dir, days=days)

    if args.json_out:
        print(render_json(report))
        return 0
    if args.by_day:
        print(render_by_day(report, state_dir))
        return 0
    print(render_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
