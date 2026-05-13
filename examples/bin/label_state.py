#!/usr/bin/env python3
"""Operator-facing CLI for the issue claim state machine.

Wraps the framework primitives in ``lib/agent_runner.py`` (``claim_issue``,
``release_issue``, ``find_stale_claims``, ``is_repo_paused``,
``issue_dedup_check``) into a one-binary command surface a consumer can
drop into their fleet's ``alfred``-style dispatcher.

Subcommands:

  claim <repo>#<N> [--force]
      Set ``do-not-pickup`` on an issue. Agents will skip it. Use when
      you want to take an issue manually without racing an in-flight
      agent.

  release <repo>#<N>
      Remove ``do-not-pickup``. Issue returns to the autonomous queue if
      it still carries ``agent:implement``.

  dedup-check <repo>#<N> [--json]
      Probe whether an issue is currently claimable by an agent. Exits
      non-zero if it's in-flight, has a PR open, or is do-not-pickup.
      Designed for use inside a pre-push git hook.

  status-issue <repo>#<N> [--json]
      Pretty-print the state-machine view of an issue.

  repo {pause,resume,list} [<repo>]
      Pause / resume / list repos. While paused, every consumer's
      pick path skips that repo.

  sweep-claims [--max-age-hours N] [--repo <name>] [--dry-run]
      On-demand stale-claim sweep. Force-releases any in-flight claim
      whose latest unreleased claim comment is older than N hours.

Doctor-mode contract:
    When ``ALFRED_DOCTOR=1`` is set, prints the OK sentinel and exits 0
    so a fleet-wide ``doctor.sh`` sweep stays at 100%/100% without
    needing to special-case this binary.

Layout note:
    Drop this file in your fleet's ``bin/`` directory, copy via
    ``deploy.sh`` to ``${ALFRED_HOME}/bin/``, and dispatch from your
    operator-facing wrapper. See the project's README for an
    end-to-end example.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib")
from agent_runner import (
    PAUSED_REPOS_FILE,
    find_stale_claims,
    force_release_stale_claim,
    gh_issue_edit,
    issue_dedup_check,
    list_paused_repos,
    set_repo_paused,
)

ISSUE_REF_RE = re.compile(r"^([\w.-]+)#(\d+)$")


def parse_issue_ref(s: str) -> tuple[str, int]:
    m = ISSUE_REF_RE.match(s.strip())
    if not m:
        raise SystemExit(
            f"label-state: invalid issue ref '{s}'. Expected form: <repo>#<N> (e.g. backend#42)"
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
    gh_issue_edit(repo, num, add_labels=["do-not-pickup"])
    print(f"label-state: claimed {repo}#{num} for the operator (do-not-pickup set).")
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    repo, num = parse_issue_ref(args.ref)
    gh_issue_edit(repo, num, remove_labels=["do-not-pickup"])
    print(f"label-state: released {repo}#{num} (do-not-pickup cleared).")
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
    reasons = []
    if state["state"] != "OPEN":
        reasons.append(f"state={state['state']}")
    if state["in_flight"]:
        latest = state.get("latest_claim") or {}
        reasons.append(f"in-flight ({latest.get('codename', '?')}:{latest.get('firing_id', '?')})")
    if state["pr_open"]:
        reasons.append("PR open")
    if state["do_not_pickup"]:
        reasons.append("do-not-pickup")
    if state["needs_human_scope"]:
        reasons.append("needs:human-scope")
    if state["repo_paused"]:
        reasons.append("repo paused")
    if args.json:
        state["reasons"] = reasons
        print(json.dumps(state, indent=2))
    else:
        print(f"label-state: {repo}#{num} NOT claimable - {', '.join(reasons)}", file=sys.stderr)
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
    repos = [args.repo] if args.repo else _default_sweep_repos()
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


def _default_sweep_repos() -> list[str]:
    """Resolve the default repo set for sweep-claims.

    Reads ``LABEL_STATE_SWEEP_REPOS`` (comma-separated) or falls back to
    an empty list (in which case the caller must pass ``--repo``).
    Alfred clean: no hardcoded repo names in the framework.
    """
    raw = os.environ.get("LABEL_STATE_SWEEP_REPOS", "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def main() -> int:
    if os.environ.get("ALFRED_DOCTOR") == "1":
        print("[LABEL-STATE-DOCTOR-OK]")
        return 0
    parser = argparse.ArgumentParser(prog="label-state")
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

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
