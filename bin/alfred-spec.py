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

from planning_assistant import engine_refiner_from_env, refine_issue_draft  # noqa: E402
from spec_helper import (  # noqa: E402
    IssueDraft,
    assess_issue_draft,
    lint_spec_file,
    render_spec_template,
    write_spec_template,
)


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


def cmd_assess(args: argparse.Namespace) -> int:
    draft = IssueDraft(
        title=args.title,
        problem=args.problem or "",
        user=args.user or "",
        current_behavior=args.current_behavior or "",
        desired_behavior=args.desired_behavior or "",
        repos=[repo.strip() for repo in (args.repo or []) if repo.strip()],
        acceptance_criteria=[item.strip() for item in (args.acceptance or []) if item.strip()],
        test_plan=args.test_plan or "",
        out_of_scope=args.out_of_scope or "",
        rollout=args.rollout or "",
        open_questions=args.open_questions or "",
    )
    result = assess_issue_draft(draft)
    payload = {
        "ok": result.ok,
        "score": result.score,
        "findings": [
            {"code": f.code, "severity": f.severity, "message": f.message} for f in result.findings
        ],
        "questions": result.questions,
        "issue_body": result.issue_body,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        status = "ready" if result.ok else "needs scope"
        print(f"alfred-spec: {status} score={result.score}")
        for finding in result.findings:
            print(f"  {finding.severity}: {finding.code}: {finding.message}")
        if result.questions:
            print("questions:")
            for question in result.questions:
                print(f"  - {question}")
        if args.print_body:
            print("\n" + result.issue_body)
    return 0 if result.ok else 1


def cmd_refine(args: argparse.Namespace) -> int:
    draft = IssueDraft(
        title=args.title,
        problem=args.problem or "",
        user=args.user or "",
        current_behavior=args.current_behavior or "",
        desired_behavior=args.desired_behavior or "",
        repos=[repo.strip() for repo in (args.repo or []) if repo.strip()],
        acceptance_criteria=[item.strip() for item in (args.acceptance or []) if item.strip()],
        test_plan=args.test_plan or "",
        out_of_scope=args.out_of_scope or "",
        rollout=args.rollout or "",
        open_questions=args.open_questions or "",
    )
    result = refine_issue_draft(
        draft,
        args.message or [],
        refiner=engine_refiner_from_env(workdir=Path.cwd()),
    )
    payload = {
        "ok": result.readiness.ok,
        "score": result.readiness.score,
        "summary": result.summary,
        "amendments": result.amendments,
        "questions": result.questions,
        "issue_body": result.issue_body,
        "spec_body": result.spec_body,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"alfred-spec: {result.summary}")
        for amendment in result.amendments:
            print(f"  - {amendment}")
        if args.print_body:
            print("\n" + result.issue_body)
        if args.print_spec:
            print("\n" + result.spec_body)
    return 0 if result.readiness.ok else 1


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

    p_assess = sub.add_parser("assess", help="assess a GitHub issue draft")
    p_assess.add_argument("title")
    p_assess.add_argument("--problem")
    p_assess.add_argument("--user")
    p_assess.add_argument("--current-behavior")
    p_assess.add_argument("--desired-behavior")
    p_assess.add_argument("--repo", action="append", help="affected repo (repeatable)")
    p_assess.add_argument("--acceptance", action="append", help="acceptance criterion")
    p_assess.add_argument("--test-plan")
    p_assess.add_argument("--out-of-scope")
    p_assess.add_argument("--rollout")
    p_assess.add_argument("--open-questions")
    p_assess.add_argument("--print-body", action="store_true")
    p_assess.add_argument("--json", action="store_true")
    p_assess.set_defaults(func=cmd_assess)

    p_refine = sub.add_parser("refine", help="apply chat-style planning feedback")
    p_refine.add_argument("title")
    p_refine.add_argument("--message", action="append", help="chat or Slack feedback")
    p_refine.add_argument("--problem")
    p_refine.add_argument("--user")
    p_refine.add_argument("--current-behavior")
    p_refine.add_argument("--desired-behavior")
    p_refine.add_argument("--repo", action="append", help="affected repo (repeatable)")
    p_refine.add_argument("--acceptance", action="append", help="acceptance criterion")
    p_refine.add_argument("--test-plan")
    p_refine.add_argument("--out-of-scope")
    p_refine.add_argument("--rollout")
    p_refine.add_argument("--open-questions")
    p_refine.add_argument("--print-body", action="store_true")
    p_refine.add_argument("--print-spec", action="store_true")
    p_refine.add_argument("--json", action="store_true")
    p_refine.set_defaults(func=cmd_refine)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
