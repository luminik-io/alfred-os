#!/usr/bin/env python3
"""Scheduled memory harvest for reviewable failure-pattern lessons.

The regular ``alfred brain harvest`` command is operator-facing. This wrapper
is scheduler-facing: it queues reviewable memory candidates from repeated
failure patterns, then optionally nudges Slack when there is something to
review. It never promotes a candidate into recall and never syncs Redis.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
for candidate in (HERE.parent / "lib", Path(os.environ.get("ALFRED_HOME", "")) / "lib"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def _brain_script() -> Path:
    return HERE / "alfred-brain.py"


def _doctor_mode() -> bool:
    return str(os.environ.get("ALFRED_DOCTOR", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _run_harvest(args: argparse.Namespace) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(_brain_script()),
        "harvest",
        "--window-days",
        str(args.window_days),
        "--min-count",
        str(args.min_count),
        "--limit",
        str(args.limit),
        "--json",
    ]
    if not args.preview:
        cmd.append("--apply")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        stdout, stderr = proc.communicate()
        detail = _short((stderr or stdout or "").strip(), 500)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"memory harvest timed out after {args.timeout}s{suffix}") from exc

    if proc.returncode != 0:
        stderr = (stderr or stdout or "memory harvest failed").strip()
        raise RuntimeError(stderr)
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"memory harvest returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {"raw": payload}


def _proposals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in payload.get("proposals", []) if isinstance(p, dict)]


def _queued_proposals(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in _proposals(payload) if p.get("status") == "queued"]


def _render_slack(payload: dict[str, Any], queued: list[dict[str, Any]] | None = None) -> str:
    applied = bool(payload.get("applied"))
    queued = queued if queued is not None else _queued_proposals(payload)
    duplicates = int(payload.get("duplicates") or 0)
    title = "Alfred memory harvest"
    lines = [f"*{title}*", ""]

    if queued:
        lines.append(
            f"Queued {len(queued)} reviewable memory candidate"
            f"{'' if len(queued) == 1 else 's'} from repeated failures."
        )
    elif applied:
        lines.append("Checked repeated failures. No new memory candidates were queued.")
    else:
        lines.append("Previewed repeated failures. Nothing was written.")

    if duplicates:
        lines.append(f"Skipped {duplicates} duplicate pattern{'' if duplicates == 1 else 's'}.")

    for item in queued[:5]:
        candidate_id = item.get("candidate_id") or "unknown"
        agent = item.get("codename") or item.get("agent") or "operator"
        repo = item.get("repo") or "global"
        body = _short(str(item.get("body") or ""), 180)
        lines.append(f"- `{candidate_id}` `{agent}/{repo}` {body}")

    extra = len(queued) - 5
    if extra > 0:
        lines.append(f"- plus {extra} more.")

    if queued:
        lines.extend(
            [
                "",
                "Review from Slack with `memory`, then `memory promote <id>` or `memory reject <id>`.",
            ]
        )
    return "\n".join(lines).strip()


def _post_slack(message: str, *, severity: str = "info") -> bool:
    try:
        from agent_runner import slack_post
    except Exception as exc:
        print(f"[memory-harvest] Slack unavailable: {exc}", file=sys.stderr)
        return False
    return bool(slack_post(message, severity=severity))


def _short(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--min-count", type=int, default=2)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview harvest candidates without queueing them.",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON payload.")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--slack", dest="slack", action="store_true", default=True)
    parser.add_argument("--no-slack", dest="slack", action="store_false")
    parser.add_argument(
        "--slack-all",
        action="store_true",
        help="Post to Slack even when no candidates were queued.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if _doctor_mode():
        print("[MEMORY-HARVEST-DOCTOR-OK]")
        return 0

    args = build_parser().parse_args(argv)
    try:
        payload = _run_harvest(args)
    except Exception as exc:
        message = f"*Alfred memory harvest failed*\n\n```{_short(str(exc), 900)}```"
        if args.slack:
            _post_slack(message, severity="warn")
        print(f"memory-harvest: {exc}", file=sys.stderr)
        return 1

    queued = _queued_proposals(payload)
    queued_count = len(queued)
    if args.slack and (queued_count > 0 or args.slack_all):
        _post_slack(_render_slack(payload, queued), severity="info")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "memory-harvest: "
            f"applied={bool(payload.get('applied'))} "
            f"proposals={len(payload.get('proposals') or [])} "
            f"queued={queued_count} "
            f"duplicates={int(payload.get('duplicates') or 0)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
