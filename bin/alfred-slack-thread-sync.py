#!/usr/bin/env python3
"""Sweep tracked Slack threads and post fleet progress deltas.

For every thread the Slack issue bridge converted into a GitHub issue, this
queries the issue and its linked PR (read-only ``gh``) and posts only the
*new* lifecycle states since the last sweep: claimed, PR opened, CI
pass/fail, merged. Idempotent -- a sweep with no GitHub change posts nothing.

Run it on a cron/launchd schedule, or rely on the listener's built-in idle
loop (``ALFRED_SLACK_THREAD_SYNC_INTERVAL_S``). Both call the same tracker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "lib",):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from slack_approval import default_slack_client, resolve_bot_token  # noqa: E402
from slack_thread_status import (  # noqa: E402
    SlackThreadStatusTracker,
    default_issue_state_fetcher,
    default_status_root,
)


class _NullPoster:
    """Stand-in poster for ``--dry-run`` that records intended messages."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat_postMessage(self, **kwargs: object) -> dict:
        self.messages.append(dict(kwargs))
        return {"ok": True}


def _build_tracker(args: argparse.Namespace) -> SlackThreadStatusTracker:
    root = Path(args.state_root).expanduser() if args.state_root else default_status_root()
    if args.dry_run:
        poster: object = _NullPoster()
    else:
        poster = default_slack_client(resolve_bot_token())
    return SlackThreadStatusTracker(root=root, poster=poster)


def cmd_sync(args: argparse.Namespace) -> int:
    tracker = _build_tracker(args)
    results = tracker.sweep(fetcher=default_issue_state_fetcher)
    posted = [r for r in results if r.get("posted")]
    if args.json:
        print(json.dumps({"swept": len(results), "updated": posted}, indent=2, sort_keys=True))
        return 0
    if not results:
        print("No tracked Slack threads to sync.")
        return 0
    print(f"Swept {len(results)} tracked thread(s); {len(posted)} posted an update.")
    for record in posted:
        states = ", ".join(record.get("posted", []))
        print(f"  {record['repo']}#{record['issue_number']} -> {states}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred-slack-thread-sync",
        description="Post fleet progress deltas to Slack threads that filed issues.",
    )
    parser.add_argument("--state-root", help="override the thread-status state directory")
    parser.add_argument("--json", action="store_true", help="emit a JSON summary")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute deltas without posting to Slack (prints intended posts in JSON)",
    )
    parser.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
