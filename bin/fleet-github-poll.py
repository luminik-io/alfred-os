#!/usr/bin/env python3
"""Poll GitHub issues/PRs into fleet-brain.

This is a local, pull-based bridge over the GitHub CLI. It does not need a
daemon or webhook endpoint: run it on a timer, or run it by hand before
``alfred brain doctor`` when you want the brain to know current issue/PR and
bundle state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fleet_brain import FleetBrain  # noqa: E402

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)


def _parse_repos(args: argparse.Namespace) -> list[str]:
    repos = list(args.repo or [])
    env_repos = [
        item.strip()
        for item in os.environ.get("ALFRED_GITHUB_POLL_REPOS", "").split(",")
        if item.strip()
    ]
    repos.extend(env_repos)
    return sorted(dict.fromkeys(repos))


def _gh_json(cmd: list[str], runner: Runner) -> list[dict[str, Any]]:
    res = runner(cmd)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "gh command failed").strip())
    data = json.loads(res.stdout or "[]")
    if not isinstance(data, list):
        raise RuntimeError("gh returned non-list JSON")
    return [item for item in data if isinstance(item, dict)]


def _poll_kind(
    brain: FleetBrain,
    *,
    repo: str,
    kind: str,
    state: str,
    limit: int,
    runner: Runner,
    now: datetime,
) -> int:
    if kind == "issue":
        cmd = [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,state,labels,createdAt,updatedAt,closedAt,url",
        ]
    else:
        cmd = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            "number,title,state,labels,createdAt,updatedAt,closedAt,mergedAt,url,headRefName,baseRefName,changedFiles,additions,deletions",
        ]
    rows = _gh_json(cmd, runner)
    for row in rows:
        labels = _labels(row.get("labels"))
        brain.upsert_github_item(
            repo=repo,
            number=int(row["number"]),
            kind="pr" if kind == "pr" else "issue",
            state=_state(row.get("state"), merged_at=row.get("mergedAt")),
            title=str(row.get("title") or ""),
            url=str(row.get("url") or ""),
            labels=labels,
            created_at=_parse_ts(row.get("createdAt")),
            updated_at=_parse_ts(row.get("updatedAt")) or now,
            last_seen_at=now,
            closed_at=_parse_ts(row.get("closedAt")),
            merged_at=_parse_ts(row.get("mergedAt")),
            head_ref=row.get("headRefName"),
            base_ref=row.get("baseRefName"),
            bundle_slug=_bundle_slug(labels),
            changed_files=_optional_non_negative_int(row, "changedFiles"),
            additions=_optional_non_negative_int(row, "additions"),
            deletions=_optional_non_negative_int(row, "deletions"),
        )
    return len(rows)


def poll_repos(
    repos: list[str],
    *,
    brain: FleetBrain,
    state: str = "all",
    limit: int = 100,
    runner: Runner = _default_runner,
    now: datetime | None = None,
) -> dict[str, int]:
    seen_at = now or datetime.now(UTC)
    counts = {"repos": 0, "issues": 0, "prs": 0, "errors": 0}
    for repo in repos:
        counts["repos"] += 1
        try:
            counts["issues"] += _poll_kind(
                brain,
                repo=repo,
                kind="issue",
                state=state,
                limit=limit,
                runner=runner,
                now=seen_at,
            )
            counts["prs"] += _poll_kind(
                brain,
                repo=repo,
                kind="pr",
                state=state,
                limit=limit,
                runner=runner,
                now=seen_at,
            )
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            counts["errors"] += 1
            print(f"fleet-github-poll: {repo}: {exc}", file=sys.stderr)
    return counts


def _labels(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("name")
        else:
            value = item
        text = str(value or "").strip()
        if text:
            out.append(text)
    return sorted(dict.fromkeys(out))


def _bundle_slug(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("agent:bundle:"):
            return label.removeprefix("agent:bundle:").strip() or None
        if label.startswith("bundle:"):
            return label.removeprefix("bundle:").strip() or None
    return None


def _state(raw: object, *, merged_at: object | None = None) -> str:
    if merged_at:
        return "merged"
    value = str(raw or "").strip().lower()
    if value in {"open", "closed", "merged"}:
        return value
    return "unknown"


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _non_negative_int(raw: object) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _optional_non_negative_int(row: dict, key: str) -> int | None:
    if key not in row or row[key] is None:
        return None
    return _non_negative_int(row[key])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet-github-poll",
        description="Poll GitHub issue/PR state into fleet-brain.",
    )
    parser.add_argument("--repo", action="append", help="owner/repo to poll, repeatable")
    parser.add_argument("--state", choices=["open", "closed", "all"], default="all")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--db", help="Path to fleet-brain SQLite db")
    parser.add_argument("--dry-run", action="store_true", help="print what would be polled")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repos = _parse_repos(args)
    if not repos:
        print(
            "fleet-github-poll: no repos configured; pass --repo or ALFRED_GITHUB_POLL_REPOS",
            file=sys.stderr,
        )
        return 2
    if args.dry_run:
        payload = {"repos": repos, "state": args.state, "limit": args.limit, "dry_run": True}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(
                "fleet-github-poll: would poll "
                f"{len(repos)} repo(s): {', '.join(repos)} "
                f"(state={args.state}, limit={args.limit})"
            )
        return 0
    brain = FleetBrain(db_path=args.db) if args.db else FleetBrain()
    counts = poll_repos(repos, brain=brain, state=args.state, limit=max(1, args.limit))
    if args.json:
        print(json.dumps(counts, indent=2))
    else:
        print(
            "fleet-github-poll: "
            f"{counts['repos']} repo(s), {counts['issues']} issue(s), "
            f"{counts['prs']} PR(s), {counts['errors']} error(s)"
        )
    return 1 if counts["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
