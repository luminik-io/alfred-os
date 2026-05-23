#!/usr/bin/env python3
"""``alfred-logs`` — stream-JSON transcript inspector.

Reads firing transcripts under
``$ALFRED_STATE_DIR/transcripts/<codename>/<YYYY-MM>/<firing_id>.jsonl``
(default state root: ``$ALFRED_HOME/state``, default ``~/.alfred/state``)
and renders them for an operator scanning recent activity.

Usage:
    alfred logs <codename>                       last 10 firings (summary)
    alfred logs <codename> --last N              last N firings
    alfred logs <codename> --firing-id <ID>      dump one firing pretty-printed
    alfred logs <codename> --show-tool-calls     aggregate tool/skill calls
                                                 across the last N firings
    alfred logs <codename> --firing-id <ID> --show-tool-calls
                                                 tool calls for one firing
    alfred logs <codename> --json                machine-readable output

Exit codes:
  0 — success
  1 — user error (unknown codename, missing firing id)
  2 — system error (state dir missing)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from transcripts import (  # noqa: E402
    FiringRef,
    TranscriptSummary,
    default_state_dir,
    find_firing,
    list_codenames,
    list_firings,
    render_firing_jsonl,
    transcript_summary,
    transcripts_root,
)

logger = logging.getLogger("alfred-logs")


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _pretty_path(path: Path) -> str:
    home = str(Path.home())
    s = str(path)
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _fmt_ts(ref: FiringRef) -> str:
    return ref.timestamp.strftime("%Y-%m-%d %H:%M:%SZ")


def _summary_row(ref: FiringRef) -> dict[str, object]:
    s = transcript_summary(ref.path)
    result = s.result
    tool_top = sorted(s.tool_calls_by_name.items(), key=lambda kv: -kv[1])[:3]
    tool_top_str = ", ".join(f"{n}x{c}" for n, c in tool_top) or "(no tool calls)"
    return {
        "firing_id": ref.firing_id,
        "ts": _fmt_ts(ref),
        "result": {
            "subtype": result.subtype if result else None,
            "num_turns": result.num_turns if result else None,
            "total_cost_usd": result.total_cost_usd if result else None,
            "stop_reason": result.stop_reason if result else None,
        },
        "tool_calls_total": s.tool_calls_total,
        "tool_top": tool_top_str,
        "skills_invoked": s.skills_invoked,
        "files_edited": len(s.files_edited),
        "files_read": len(s.files_read),
        "path": str(ref.path),
    }


def cmd_summary(state_dir: Path, codename: str, last: int, json_out: bool) -> int:
    firings = list_firings(state_dir, codename)[:last]
    if not firings:
        if json_out:
            print(json.dumps({"codename": codename, "firings": []}))
        else:
            print(f"no transcripts under {transcripts_root(state_dir)}/{codename}/")
        return 0

    rows = [_summary_row(ref) for ref in firings]

    if json_out:
        print(json.dumps({"codename": codename, "firings": rows}, default=str, indent=2))
        return 0

    print(f"alfred-logs {codename} — last {len(rows)} firings")
    print(f"transcripts: {transcripts_root(state_dir)}/{codename}/")
    print()
    header = (
        f"{'firing_id':<22} {'when':<22} {'subtype':<14} {'turns':<6} {'cost':<7} "
        f"{'tools':<6} {'edits':<6} {'top tools'}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        result = r["result"] or {}
        cost = float(result.get("total_cost_usd") or 0)
        skills_note = (
            f" [skills: {','.join(r['skills_invoked'])}]" if r["skills_invoked"] else ""
        )
        print(
            f"{r['firing_id']:<22} {r['ts']:<22} "
            f"{(result.get('subtype') or '?'):<14} "
            f"{(result.get('num_turns') or 0):<6} "
            f"${cost:<6.2f} "
            f"{r['tool_calls_total']:<6} "
            f"{r['files_edited']:<6} "
            f"{r['tool_top']}{skills_note}"
        )
    return 0


def cmd_firing(
    state_dir: Path,
    codename: str,
    firing_id: str,
    tools_only: bool,
    json_out: bool,
) -> int:
    ref = find_firing(state_dir, codename, firing_id)
    if ref is None:
        print(
            f"alfred-logs: no transcript for {codename}/{firing_id} under "
            f"{transcripts_root(state_dir)}",
            file=sys.stderr,
        )
        return 1

    if tools_only:
        s = transcript_summary(ref.path)
        if json_out:
            print(json.dumps(s.to_dict(), indent=2, default=str))
            return 0
        _print_firing_tool_calls(ref, s)
        return 0

    if json_out:
        # Re-emit as-is; this is valid JSONL.
        sys.stdout.write(ref.path.read_text(encoding="utf-8", errors="replace"))
        if not sys.stdout.isatty():
            sys.stdout.flush()
        return 0

    for line in render_firing_jsonl(ref.path):
        print(line)
    return 0


def _print_firing_tool_calls(ref: FiringRef, s: TranscriptSummary) -> None:
    print(f"firing {ref.firing_id} ({_fmt_ts(ref)}) — {_pretty_path(ref.path)}")
    print(f"total tool calls: {s.tool_calls_total}")
    for name, count in sorted(s.tool_calls_by_name.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} x{count}")
    if s.skills_invoked:
        print()
        print("Skill invocations:")
        for sk in s.skills_invoked:
            print(f"  - {sk}")
    if s.files_edited:
        print()
        print(f"Files edited ({len(s.files_edited)}):")
        for f in s.files_edited[:20]:
            print(f"  - {f}")
        if len(s.files_edited) > 20:
            print(f"  ... and {len(s.files_edited) - 20} more")
    if s.bash_commands:
        print()
        print(f"Bash commands ({len(s.bash_commands)}):")
        for c in s.bash_commands[:10]:
            print(f"  $ {c}")
        if len(s.bash_commands) > 10:
            print(f"  ... and {len(s.bash_commands) - 10} more")


def cmd_tools_recent(
    state_dir: Path,
    codename: str,
    last: int,
    json_out: bool,
) -> int:
    firings = list_firings(state_dir, codename)[:last]
    if not firings:
        if json_out:
            print(json.dumps({"codename": codename, "firings": []}))
        else:
            print(f"no transcripts under {transcripts_root(state_dir)}/{codename}/")
        return 0
    aggregate: dict[str, int] = {}
    skills_seen: dict[str, int] = {}
    for ref in firings:
        s = transcript_summary(ref.path)
        for name, count in s.tool_calls_by_name.items():
            aggregate[name] = aggregate.get(name, 0) + count
        for sk in s.skills_invoked:
            skills_seen[sk] = skills_seen.get(sk, 0) + 1

    if json_out:
        print(json.dumps({
            "codename": codename,
            "firings": len(firings),
            "tools": aggregate,
            "skills": skills_seen,
        }, indent=2))
        return 0

    print(f"alfred-logs {codename} --show-tool-calls — last {len(firings)} firings")
    print()
    print("tool          calls")
    print("-" * 24)
    for name, count in sorted(aggregate.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<14} {count:>5}")
    if skills_seen:
        print()
        print("skill invocations:")
        for sk, count in sorted(skills_seen.items(), key=lambda kv: -kv[1]):
            print(f"  /{sk:<22} {count:>3}")
    else:
        print()
        print("(no skill invocations across these firings)")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="alfred-logs",
        description="Inspect stream-JSON transcripts for a single agent codename.",
    )
    p.add_argument(
        "codename",
        help="agent codename (the directory name under transcripts/)",
    )
    p.add_argument(
        "--firing-id",
        dest="firing_id",
        help="dump this firing id only (full pretty-printed transcript)",
    )
    p.add_argument(
        "--last",
        type=int,
        default=10,
        help="how many recent firings to consider (default 10)",
    )
    p.add_argument(
        "--show-tool-calls",
        action="store_true",
        help="aggregate tool / skill calls across the last N firings, "
             "or one firing if --firing-id is set",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_out",
        help="machine-readable JSON output",
    )
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
            f"alfred-logs: state directory {state_dir} does not exist. "
            "Set ALFRED_STATE_DIR or run the fleet at least once.",
            file=sys.stderr,
        )
        return 2

    codenames = list_codenames(state_dir)
    if codenames and args.codename not in codenames:
        print(
            f"alfred-logs: unknown codename '{args.codename}'. "
            f"Known: {', '.join(codenames)}",
            file=sys.stderr,
        )
        return 1

    if args.firing_id:
        return cmd_firing(
            state_dir, args.codename, args.firing_id, args.show_tool_calls, args.json_out
        )
    if args.show_tool_calls:
        return cmd_tools_recent(state_dir, args.codename, args.last, args.json_out)
    return cmd_summary(state_dir, args.codename, args.last, args.json_out)


if __name__ == "__main__":
    sys.exit(main())
