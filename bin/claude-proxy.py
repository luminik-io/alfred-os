#!/usr/bin/env python3
"""alfred-claude-proxy daemon.

Long-running unix-socket bridge that lets non-Aqua launchd agents invoke
``claude`` through a process that lives in the Aqua login session. See
``docs/MACOS_KEYCHAIN.md`` for the underlying macOS keychain issue this
solves, and ``docs/CLAUDE_PROXY.md`` for install + verify instructions.

This script is intentionally thin: argv parsing, logging setup, then a
single ``asyncio.run`` of :func:`claude_proxy.server.run_server`. All
real logic lives in the library so the tests can exercise it without
shelling out to this script.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Make ``lib/`` importable when run from a launchd plist that points
# directly at this script. The plist sets ALFRED_HOME but does not
# necessarily add lib/ to PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.claude_proxy.server import (  # noqa: E402
    resolve_audit_log_path,
    resolve_socket_path,
    run_server,
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-proxy",
        description=(
            "Localhost daemon that brokers claude -p invocations on behalf "
            "of launchd-spawned agent processes that cannot themselves read "
            "the macOS keychain."
        ),
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Unix socket path. Defaults to $ALFRED_HOME/run/claude-proxy.sock.",
    )
    parser.add_argument(
        "--claude-bin",
        default=None,
        help=(
            "Path to the claude executable. Defaults to $CLAUDE_BIN or the "
            "string 'claude' (resolved against PATH at exec time)."
        ),
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help=(
            "Path to the JSONL audit log. Defaults to "
            "$ALFRED_HOME/state/claude-proxy/log.jsonl. Pass '' to disable."
        ),
    )
    parser.add_argument(
        "--graceful-shutdown-seconds",
        type=int,
        default=0,
        help=(
            "On SIGTERM, wait up to N seconds for in-flight invocations "
            "before killing them. Default 0 (terminate immediately)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("ALFRED_CLAUDE_PROXY_LOG_LEVEL", "INFO"),
        help="Python log level for stderr output. Default INFO.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    socket_path = args.socket or resolve_socket_path()
    audit_log_path: Path | None
    if args.audit_log is None:
        audit_log_path = resolve_audit_log_path()
    elif str(args.audit_log) == "":
        audit_log_path = None
    else:
        audit_log_path = args.audit_log

    return asyncio.run(
        run_server(
            socket_path=socket_path,
            claude_bin=args.claude_bin,
            audit_log_path=audit_log_path,
            graceful_shutdown_seconds=args.graceful_shutdown_seconds,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
