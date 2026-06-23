#!/usr/bin/env python3
"""Scheduled, gated memory auto-promotion.

The operator-facing command is ``alfred brain auto-promote``. This wrapper is
scheduler-facing: it runs one bounded auto-promotion pass and posts a short
Slack note only when something was promoted or held for a human. It promotes
ONLY high-confidence, evidence-backed, judge-approved candidates and leaves
everything else in the review queue.

Safe by default: the whole pass is a true no-op unless ``ALFRED_AUTO_PROMOTE``
is armed, and ``ALFRED_AUTO_PROMOTE_KILL`` halts it without editing config. The
per-run promotion cap and judge-call budget bound the work (and this wrapper's
timeout), so a runaway queue can never promote in bulk or hang the scheduler.
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


def _run_auto_promote(args: argparse.Namespace) -> dict[str, Any]:
    cmd = [sys.executable, str(_brain_script()), "auto-promote", "--json"]
    if args.threshold is not None:
        cmd += ["--threshold", str(args.threshold)]
    if args.max_per_run is not None:
        cmd += ["--max-per-run", str(args.max_per_run)]

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
        raise RuntimeError(f"memory auto-promote timed out after {args.timeout}s{suffix}") from exc

    if proc.returncode != 0:
        stderr = (stderr or stdout or "memory auto-promote failed").strip()
        raise RuntimeError(stderr)
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"memory auto-promote returned invalid JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else {"raw": payload}


def _held_count(payload: dict[str, Any]) -> int:
    return (
        int(payload.get("flagged_behavior_change") or 0)
        + int(payload.get("skipped_duplicate") or 0)
        + int(payload.get("held_low_confidence") or 0)
    )


def _render_slack(payload: dict[str, Any]) -> str:
    promoted = [p for p in payload.get("promoted", []) if isinstance(p, str)]
    held = _held_count(payload)
    lines = ["*Alfred memory auto-promote*", ""]

    if promoted:
        lines.append(
            f"Promoted {len(promoted)} high-confidence lesson"
            f"{'' if len(promoted) == 1 else 's'} into recall."
        )
    else:
        lines.append("Checked the queue. Nothing cleared the bar this run.")

    if held:
        lines.append(f"Held {held} for human review (behavior-changing or duplicate).")

    for candidate_id in promoted[:5]:
        lines.append(f"- `{candidate_id}`")
    extra = len(promoted) - 5
    if extra > 0:
        lines.append(f"- plus {extra} more.")

    if held:
        lines.extend(
            [
                "",
                "Review the held ones from Slack with `memory`, then "
                "`memory promote <id>` or `memory reject <id>`.",
            ]
        )
    return "\n".join(lines).strip()


def _post_slack(message: str, *, severity: str = "info") -> bool:
    try:
        from agent_runner import slack_post
    except Exception as exc:
        print(f"[memory-auto-promote] Slack unavailable: {exc}", file=sys.stderr)
        return False
    return bool(slack_post(message, severity=severity))


def _short(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--max-per-run", dest="max_per_run", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Print raw JSON payload.")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--slack", dest="slack", action="store_true", default=True)
    parser.add_argument("--no-slack", dest="slack", action="store_false")
    parser.add_argument(
        "--slack-all",
        action="store_true",
        help="Post to Slack even when nothing was promoted or held.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _run_auto_promote(args)
    except Exception as exc:
        message = f"*Alfred memory auto-promote failed*\n\n```{_short(str(exc), 900)}```"
        if args.slack:
            _post_slack(message, severity="warn")
        print(f"memory-auto-promote: {exc}", file=sys.stderr)
        return 1

    promoted = [p for p in payload.get("promoted", []) if isinstance(p, str)]
    held = _held_count(payload)
    # Disarmed runs are a silent no-op: never nag Slack when the operator has
    # not armed auto-promotion.
    armed = bool(payload.get("enabled"))
    notable = bool(promoted) or held > 0
    if args.slack and armed and (notable or args.slack_all):
        _post_slack(_render_slack(payload), severity="info")

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "memory-auto-promote: "
            f"enabled={armed} "
            f"considered={int(payload.get('considered') or 0)} "
            f"promoted={len(promoted)} "
            f"held={held}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
