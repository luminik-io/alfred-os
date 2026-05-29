#!/usr/bin/env python3
"""Run or test Alfred's Slack planning listener."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "lib",):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from slack_listener import SlackPlanningListener, run_socket_mode  # noqa: E402


class _NullPoster:
    def chat_postMessage(self, **kwargs):
        self.last_payload = kwargs
        return {"ok": True}


def cmd_once(args: argparse.Namespace) -> int:
    if args.path == "-":
        payload = json.load(sys.stdin)
    else:
        payload = json.loads(Path(args.path).expanduser().read_text(encoding="utf-8"))
    state_root = Path(args.state_root).expanduser() if args.state_root else None
    listener = SlackPlanningListener(
        state_root=state_root,
        poster=None if args.no_post else _NullPoster(),
        trusted_user_ids=tuple(args.trusted_user) if args.trusted_user is not None else None,
    )
    result = listener.handle_payload(payload)
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 0 if result.handled or args.allow_ignored else 1


def cmd_run(_args: argparse.Namespace) -> int:
    run_socket_mode()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred-slack-listener",
        description="Listen for Slack planning messages and thread replies.",
    )
    sub = parser.add_subparsers(dest="command")

    p_once = sub.add_parser("once", help="process one Slack event JSON payload")
    p_once.add_argument("path", help="payload JSON path, or '-' for stdin")
    p_once.add_argument("--state-root", help="override Alfred state root")
    p_once.add_argument(
        "--trusted-user",
        action="append",
        help="trusted Slack user id; repeat to override env-based trust",
    )
    p_once.add_argument("--no-post", action="store_true", help="do not post acknowledgement")
    p_once.add_argument("--allow-ignored", action="store_true", help="exit 0 for ignored events")
    p_once.set_defaults(func=cmd_once)

    p_run = sub.add_parser("run", help="run the Socket Mode listener")
    p_run.set_defaults(func=cmd_run)
    parser.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
