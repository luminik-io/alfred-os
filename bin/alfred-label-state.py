#!/usr/bin/env python3
"""``alfred label-state`` — operator-facing CLI for the issue claim state machine.

A thin argparse layer on top of the state-machine primitives in
``lib/agent_runner.py`` and the label constants in ``lib/labels.py``.
All real work happens in those modules; this file is the operator's
command surface only.

Subcommands:

  claim <repo>#<N> [--force]
      Set ``do-not-pickup`` on an issue. Agents will skip it.

  release <repo>#<N>
      Remove ``do-not-pickup``.

  dedup-check <repo>#<N> [--json]
      Probe whether an issue is currently claimable. Exits non-zero if
      it's in-flight, has a PR open, or is do-not-pickup. Designed for
      use inside a pre-push git hook.

  status-issue <repo>#<N> [--json]
      Pretty-print the state-machine view of an issue.

  repo {pause,resume,list} [<repo>]
      Pause / resume / list repos. While paused, every consumer's pick
      path skips that repo.

  sweep-claims [--max-age-hours N] [--repo <name>] [--dry-run]
      On-demand stale-claim sweep across configured repos.

Configuration (12-factor):

  GH_ORG                         GitHub org for repo-targeting helpers
                                 inside ``agent_runner``. Required.
  ALFRED_HOME                    Runtime root (default ``~/.alfred``).
  LABEL_STATE_SWEEP_REPOS        Comma-separated repo slugs for the
                                 default ``sweep-claims`` target set.

Doctor-mode contract:
  ``ALFRED_DOCTOR=1`` -> print sentinel + exit 0.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

# Resolve lib/ on sys.path. When deployed via ``deploy.sh`` the lib lives
# at ``$ALFRED_HOME/lib``; when running from a checkout it's a sibling of
# bin/. Try both.
_HERE = Path(__file__).resolve().parent
_REPO_LIB = _HERE.parent / "lib"
_ALFRED_LIB = Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) / "lib"
for candidate in (_REPO_LIB, _ALFRED_LIB):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))

from agent_runner import (  # noqa: E402
    find_stale_claims,
    force_release_stale_claim,
    gh_issue_edit,
    issue_dedup_check,
    list_paused_repos,
    set_repo_paused,
)
from labels import DO_NOT_PICKUP, LabelStateConfig  # noqa: E402

logger = logging.getLogger("alfred.label-state")

ISSUE_REF_RE = re.compile(r"^([\w.-]+)#(\d+)$")


def parse_issue_ref(s: str) -> tuple[str, int]:
    """Parse ``<repo>#<N>`` into ``(repo, num)``. Raises SystemExit on garbage."""
    m = ISSUE_REF_RE.match(s.strip())
    if not m:
        raise SystemExit(
            f"label-state: invalid issue ref '{s}'. Expected form: "
            f"<repo>#<N> (e.g. your-backend#42)"
        )
    return m.group(1), int(m.group(2))


def cmd_claim(args: argparse.Namespace) -> int:
    repo, num = parse_issue_ref(args.ref)
    state = issue_dedup_check(repo, num)
    if state["pr_open"]:
        print(
            f"label-state: {repo}#{num} already has a PR open. Manual claim refused.",
            file=sys.stderr,
        )
        return 2
    if state["in_flight"] and not args.force:
        latest = state.get("latest_claim") or {}
        print(
            f"label-state: {repo}#{num} is in-flight by "
            f"{latest.get('codename', '?')}:{latest.get('firing_id', '?')}. "
            f"Pass --force to override.",
            file=sys.stderr,
        )
        return 2
    gh_issue_edit(repo, num, add_labels=[DO_NOT_PICKUP])
    print(f"label-state: claimed {repo}#{num} for the operator ({DO_NOT_PICKUP} set).")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    repo, num = parse_issue_ref(args.ref)
    gh_issue_edit(repo, num, remove_labels=[DO_NOT_PICKUP])
    print(f"label-state: released {repo}#{num} ({DO_NOT_PICKUP} cleared).")
    return 0


def cmd_dedup_check(args: argparse.Namespace) -> int:
    repo, num = parse_issue_ref(args.ref)
    state = issue_dedup_check(repo, num)
    if state["claimable"]:
        if args.json:
            print(json.dumps(state, indent=2))
        else:
            print(f"label-state: {repo}#{num} is claimable.")
        return 0
    reasons = _build_reason_list(state)
    if args.json:
        state["reasons"] = reasons
        print(json.dumps(state, indent=2))
    else:
        print(
            f"label-state: {repo}#{num} NOT claimable - {', '.join(reasons)}",
            file=sys.stderr,
        )
    return 1


def cmd_status_issue(args: argparse.Namespace) -> int:
    repo, num = parse_issue_ref(args.ref)
    state = issue_dedup_check(repo, num)
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(f"  repo:           {state['repo']} ({'paused' if state['repo_paused'] else 'active'})")
    print(f"  number:         #{state['number']}")
    print(f"  state:          {state['state']}")
    print(f"  claimable:      {'yes' if state['claimable'] else 'no'}")
    print(f"  in-flight:      {'yes' if state['in_flight'] else 'no'}")
    print(f"  pr-open:        {'yes' if state['pr_open'] else 'no'}")
    print(f"  do-not-pickup:  {'yes' if state['do_not_pickup'] else 'no'}")
    print(f"  human-scope:    {'yes' if state['needs_human_scope'] else 'no'}")
    if state.get("latest_claim"):
        c = state["latest_claim"]
        print(
            f"  latest claim:   codename={c.get('codename')} "
            f"firing_id={c.get('firing_id')} ts={c.get('createdAt') or c.get('ts')}"
        )
    print(f"  labels:         {', '.join(state['labels']) if state['labels'] else '(none)'}")
    return 0


def cmd_repo(args: argparse.Namespace) -> int:
    action = args.action
    if action == "list":
        paused = list_paused_repos()
        if not paused:
            print("label-state: no repos paused.")
            return 0
        print("Paused repos (agents will skip):")
        for r in paused:
            print(f"  {r}")
        return 0
    if action in ("pause", "resume"):
        if not args.repo:
            print(f"label-state repo {action}: need <repo>", file=sys.stderr)
            return 2
        new_list = set_repo_paused(args.repo, paused=(action == "pause"))
        verb = "paused" if action == "pause" else "resumed"
        print(f"label-state: {verb} {args.repo}.")
        if new_list:
            print(f"  currently paused: {', '.join(new_list)}")
        else:
            print("  no repos paused.")
        return 0
    print(f"label-state repo: unknown action '{action}'", file=sys.stderr)
    return 2


def cmd_sweep_claims(args: argparse.Namespace) -> int:
    repos = _resolve_sweep_repos(args)
    if not repos:
        print(
            "label-state sweep-claims: no repos to sweep. Pass --repo or set "
            "LABEL_STATE_SWEEP_REPOS in your environment "
            "(comma-separated repo slugs).",
            file=sys.stderr,
        )
        return 2
    sweep_id = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S-manual")
    total_stale = 0
    total_swept = 0
    for repo in repos:
        try:
            stale = find_stale_claims(repo, max_age_hours=args.max_age_hours)
        except Exception as e:
            print(f"  {repo}: probe failed: {e}", file=sys.stderr)
            continue
        if not stale:
            continue
        total_stale += len(stale)
        print(f"  {repo}: {len(stale)} stale claim(s)")
        for entry in stale:
            print(
                f"    #{entry['number']} codename={entry['codename']} "
                f"firing_id={entry['firing_id']} "
                f"age={entry.get('age_hours', 0):.1f}h - "
                f"{entry.get('title', '')[:60]}"
            )
            if not args.dry_run:
                try:
                    released = force_release_stale_claim(
                        repo,
                        entry["number"],
                        sweep_id=sweep_id,
                        released_codename=entry.get("codename"),
                        released_firing_id=entry.get("firing_id"),
                        label_drift=bool(entry.get("label_drift")),
                        max_age_hours=int(entry.get("max_age_hours") or args.max_age_hours),
                    )
                    if not released:
                        raise RuntimeError("GitHub label/comment update returned false")
                    total_swept += 1
                except Exception as e:
                    print(f"    sweep failed: {e}", file=sys.stderr)
    if args.dry_run:
        print(
            f"label-state: dry-run, {total_stale} stale claim(s) across "
            f"{len(repos)} repo(s). No changes made."
        )
    else:
        print(
            f"label-state: swept {total_swept}/{total_stale} stale claim(s) "
            f"across {len(repos)} repo(s)."
        )
    return 0


def _build_reason_list(state: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    if state.get("state") != "OPEN":
        reasons.append(f"state={state.get('state')}")
    if state.get("in_flight"):
        latest = state.get("latest_claim") or {}
        if isinstance(latest, dict):
            reasons.append(
                f"in-flight ({latest.get('codename', '?')}:{latest.get('firing_id', '?')})"
            )
        else:
            reasons.append("in-flight")
    if state.get("pr_open"):
        reasons.append("PR open")
    if state.get("do_not_pickup"):
        reasons.append(DO_NOT_PICKUP)
    if state.get("needs_human_scope"):
        reasons.append("needs:human-scope")
    if state.get("repo_paused"):
        reasons.append("repo paused")
    return reasons


def _resolve_sweep_repos(args: argparse.Namespace) -> list[str]:
    if args.repo:
        return [args.repo]
    cfg = LabelStateConfig.from_env()
    return list(cfg.sweep_repos)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alfred-label-state")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("claim", help="Set do-not-pickup on an issue")
    p.add_argument("ref", help="<repo>#<N>")
    p.add_argument("--force", action="store_true", help="Override an existing in-flight claim")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="Remove do-not-pickup from an issue")
    p.add_argument("ref")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("dedup-check", help="Exit non-zero if an issue is not claimable")
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dedup_check)

    p = sub.add_parser("status-issue", help="Pretty-print the state-machine view of an issue")
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status_issue)

    p = sub.add_parser("repo", help="Pause / resume / list repos")
    p.add_argument("action", choices=["pause", "resume", "list"])
    p.add_argument("repo", nargs="?", help="<repo-name>")
    p.set_defaults(func=cmd_repo)

    p = sub.add_parser("sweep-claims", help="Force-release stale agent:in-flight claims")
    p.add_argument("--max-age-hours", type=int, default=4)
    p.add_argument("--repo", help="Sweep one repo only")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_sweep_claims)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if os.environ.get("ALFRED_DOCTOR") == "1":
        print("[ALFRED-LABEL-STATE-DOCTOR-OK]")
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
