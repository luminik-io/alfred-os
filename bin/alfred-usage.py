#!/usr/bin/env python3
"""alfred-usage - operator usage + remaining limits for the local engines.

Alfred drives Claude Code and Codex through their local subscription CLIs (not
API keys), so there is no billing API to query. This command instead reads the
state files those CLIs persist on disk and reports the operator's usage and
remaining headroom for the rolling 5-hour and weekly limit windows.

Local state read (all under the operator's home, overridable via env):
  - Claude Code session transcripts  ~/.claude/projects/**/*.jsonl
  - Claude usage-limit cache          ~/.claude/usage-limits.json
  - Codex session rollouts            ~/.codex/sessions/**/*.jsonl
                                      (and ~/.codex/archived_sessions/)

Usage:
    alfred usage            # human-readable table
    alfred usage --json     # machine-readable {"claude": {...}, "codex": {...}}

Numbers are never fabricated: a provider whose local state cannot be read is
reported as ``available: false`` with a reason, and any single limit figure the
CLI does not persist is shown as ``-`` (``null`` in JSON).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Resolve lib/ regardless of how the script was invoked. In the installed fleet
# layout the script lives in ``$ALFRED_HOME/bin/`` and the library in
# ``$ALFRED_HOME/lib/``; in a checkout the script lives in ``<repo>/bin/`` and
# the library in ``<repo>/lib/``. Probe both. (Mirrors bin/alfred-serve.py.)
_HERE = Path(__file__).resolve().parent
_runtime_home = os.environ.get("ALFRED_HOME") or ""
for _candidate in (_HERE.parent / "lib", Path(_runtime_home) / "lib"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred usage",
        description=(
            "Show local Claude Code + Codex subscription usage and remaining "
            "5-hour / weekly limits, read from the engines' own local state files."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help='emit the raw {"claude": {...}, "codex": {...}} payload',
    )
    return p


def _fmt_pct(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}%"
    return "-"


def _fmt_minutes(value: Any) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        return "-"
    if value <= 0:
        return "now"
    hours, minutes = divmod(value, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _window_line(label: str, window: Any) -> str:
    if not isinstance(window, dict):
        return f"    {label:<8} unavailable"
    used = _fmt_pct(window.get("used_percent"))
    remaining = _fmt_pct(window.get("remaining_percent"))
    resets = _fmt_minutes(window.get("minutes_to_reset"))
    reset_at = window.get("reset_at")
    reset_at = reset_at if isinstance(reset_at, str) else "-"
    return (
        f"    {label:<8} used {used:<6} remaining {remaining:<6} "
        f"resets in {resets:<6} (at {reset_at})"
    )


def _render_provider(name: str, provider: Any) -> list[str]:
    lines = [f"{name}:"]
    if not isinstance(provider, dict) or not provider.get("available"):
        reason = ""
        if isinstance(provider, dict):
            reason = provider.get("unavailable_reason") or ""
        lines.append(f"    unavailable{(' - ' + reason) if reason else ''}")
        return lines
    lines.append(_window_line("5-hour", provider.get("five_hour")))
    lines.append(_window_line("weekly", provider.get("weekly")))
    plan = provider.get("plan_type")
    if isinstance(plan, str) and plan:
        lines.append(f"    plan     {plan}")
    return lines


def _render_human(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.extend(_render_provider("Claude Code", payload.get("claude")))
    lines.append("")
    lines.extend(_render_provider("Codex", payload.get("codex")))
    if not payload.get("available"):
        lines.append("")
        err = payload.get("error")
        if err:
            lines.append(f"No local usage state could be read: {err}")
        else:
            lines.append(
                "No local usage state could be read for either engine "
                "(looked under ~/.claude and ~/.codex)."
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        from server.usage import build_provider_usage
    except ImportError as exc:
        sys.stderr.write(f"alfred usage: could not load the usage reader: {exc}\n")
        return 2

    payload = build_provider_usage()

    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(_render_human(payload) + "\n")

    # Exit 0 even when usage is unavailable: an absent local CLI state is a
    # valid, reportable condition, not a command failure. The payload's
    # ``available`` flag carries that signal for scripts that care.
    return 0


if __name__ == "__main__":
    sys.exit(main())
