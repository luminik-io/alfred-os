#!/usr/bin/env python3
"""``alfred spec`` helpers for specs-driven fleet work."""

from __future__ import annotations

import argparse
import json
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

from spec_helper import lint_spec_file, render_spec_template, write_spec_template  # noqa: E402


def cmd_new(args: argparse.Namespace) -> int:
    repos = [repo.strip() for repo in (args.repo or []) if repo.strip()]
    if args.out:
        path = Path(args.out).expanduser()
    else:
        slug = _slug(args.title)
        path = Path.cwd() / "docs" / "specs" / f"{slug}.md"
    write_spec_template(path, args.title, repos)
    print(f"alfred-spec: wrote {path}")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    result = lint_spec_file(Path(args.path).expanduser())
    if args.json:
        print(
            json.dumps(
                {
                    "path": result.path,
                    "ok": result.ok,
                    "findings": [
                        {"code": f.code, "severity": f.severity, "message": f.message}
                        for f in result.findings
                    ],
                },
                indent=2,
            )
        )
    else:
        if result.ok:
            print(f"alfred-spec: ok {result.path}")
        else:
            print(f"alfred-spec: issues in {result.path}")
        for finding in result.findings:
            print(f"  {finding.severity}: {finding.code}: {finding.message}")
    return 0 if result.ok else 1


def cmd_template(args: argparse.Namespace) -> int:
    repos = [repo.strip() for repo in (args.repo or []) if repo.strip()]
    print(render_spec_template(args.title, repos))
    return 0


def _slug(title: str) -> str:
    out = []
    last_dash = False
    for ch in title.strip().lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return slug or "new-spec"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alfred-spec", description="Spec helper for Alfred")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="write a new spec template")
    p_new.add_argument("title")
    p_new.add_argument("--repo", action="append", help="affected repo (repeatable)")
    p_new.add_argument("--out", help="output path")
    p_new.set_defaults(func=cmd_new)

    p_lint = sub.add_parser("lint", aliases=["check"], help="lint a spec file")
    p_lint.add_argument("path")
    p_lint.add_argument("--json", action="store_true")
    p_lint.set_defaults(func=cmd_lint)

    p_template = sub.add_parser("template", help="print the spec template")
    p_template.add_argument("title")
    p_template.add_argument("--repo", action="append", help="affected repo (repeatable)")
    p_template.set_defaults(func=cmd_template)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
