#!/usr/bin/env python3
"""alfred assign: route a label-free GitHub issue to Batman or Lucius."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    / "lib",
):
    if candidate.exists():
        candidate_path = str(candidate)
        if candidate_path in sys.path:
            sys.path.remove(candidate_path)
        sys.path.insert(0, candidate_path)

from issue_assignment import assign_issue  # noqa: E402
from issue_queue import parse_issue_ref  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="alfred assign",
        description="Decide whether Batman or Lucius should pick up an issue.",
    )
    parser.add_argument("ref", help="GitHub issue URL or owner/repo#123")
    parser.add_argument("--dry-run", action="store_true", help="Explain without mutating labels")
    parser.add_argument("--json", action="store_true", help="Print structured JSON")
    args = parser.parse_args()

    ref = parse_issue_ref(args.ref)
    if ref is None:
        print(
            "alfred assign: expected a GitHub issue URL or owner/repo#123",
            file=sys.stderr,
        )
        return 2
    repo, number = ref
    result = assign_issue(repo, number, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result.to_jsonable(), indent=2, sort_keys=True))
    elif result.ok:
        print(result.detail)
    else:
        print(f"alfred assign: {result.error or result.detail}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
