#!/usr/bin/env python3
"""Summarize shipped work across configured GitHub repositories.

Usage:
    alfred-shipped-summary.py --repo myorg/backend --repo myorg/frontend
    alfred-shipped-summary.py --period weekly --slack
    alfred-shipped-summary.py --since 2026-05-01 --until 2026-05-08 --json

Scheduled fleets normally set ALFRED_SHIPPED_SUMMARY_REPOS to a comma-separated
repo list. Bare repo names are resolved through GH_ORG; full owner/repo slugs
work without GH_ORG.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agent_runner import (  # noqa: E402
    GH_ORG,
    STATE_ROOT,
    PreflightFailed,
    PreflightSpec,
    doctor_mode,
    gh_json,
    preflight,
    slack_post,
)

AGENT = "shipped-summary"
PR_FIELDS = (
    "number,title,url,mergedAt,additions,deletions,changedFiles,author,labels,"
    "closingIssuesReferences"
)
ISSUE_FIELDS = "number,title,url,createdAt,closedAt,state"
MODEL_PATH_HINTS = (
    "agents.conf",
    "agent_models",
    "engine",
    "llm",
    "model",
    "models",
    "provider",
    "codex",
    "claude",
)
DEFAULT_QUERY_LIMIT = 1000
MAX_QUERY_LIMIT = 5000
PREFLIGHT = PreflightSpec(agent=AGENT, bins=["gh"], require_gh_auth=True)


@dataclass
class Period:
    label: str
    start: datetime
    end: datetime


def local_tz() -> timezone:
    tz = datetime.now().astimezone().tzinfo
    return tz or UTC


def parse_day(raw: str, *, end_of_day: bool = False) -> datetime:
    day = date.fromisoformat(raw)
    wall = time.max if end_of_day else time.min
    return datetime.combine(day, wall, tzinfo=local_tz())


def resolve_period(args: argparse.Namespace) -> Period:
    now = datetime.now(tz=local_tz())
    if args.since:
        start = parse_day(args.since)
        end = parse_day(args.until, end_of_day=True) if args.until else now
        return Period(label=f"{start.date()} to {end.date()}", start=start, end=end)
    if args.days:
        start = now - timedelta(days=args.days)
        return Period(label=f"last {args.days} days", start=start, end=now)
    if args.period == "weekly":
        start = now - timedelta(days=7)
        return Period(label="last 7 days", start=start, end=now)
    start = datetime.combine(now.date(), time.min, tzinfo=local_tz())
    return Period(label=str(now.date()), start=start, end=now)


def configured_repos() -> list[str]:
    raw = os.environ.get("ALFRED_SHIPPED_SUMMARY_REPOS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def repo_slug(repo: str) -> str:
    if "/" in repo:
        return repo
    if not GH_ORG:
        raise ValueError(
            f"repo '{repo}' is bare and GH_ORG is unset; pass owner/repo or set GH_ORG"
        )
    return f"{GH_ORG}/{repo}"


def parse_github_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def in_period(raw_ts: str | None, period: Period) -> bool:
    ts = parse_github_ts(raw_ts)
    if ts is None:
        return False
    start = period.start.astimezone(UTC)
    end = period.end.astimezone(UTC)
    return start <= ts < end


def search_window(period: Period, qualifier: str) -> str:
    start = period.start.astimezone(UTC).date().isoformat()
    end_utc = period.end.astimezone(UTC)
    end_date = end_utc.date()
    if end_utc.time() != time.min:
        end_date += timedelta(days=1)
    return f"{qualifier}:>={start} {qualifier}:<{end_date.isoformat()}"


def github_query_limit() -> int:
    raw = os.environ.get("ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT", "").strip()
    if not raw:
        return DEFAULT_QUERY_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_QUERY_LIMIT
    return max(1, min(value, MAX_QUERY_LIMIT))


def query_windows(period: Period) -> list[Period]:
    """Split a report period into UTC-day query windows."""
    start = period.start.astimezone(UTC)
    end = period.end.astimezone(UTC)
    if end <= start:
        return [period]

    out: list[Period] = []
    cursor = start
    while cursor < end:
        next_midnight = datetime.combine(
            cursor.date() + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )
        window_end = min(next_midnight, end)
        out.append(Period(label=period.label, start=cursor, end=window_end))
        cursor = window_end
    return out


def fetch_merged_prs(
    repo: str,
    period: Period,
    query_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    limit = github_query_limit()
    slug = repo_slug(repo)
    out: dict[int, dict[str, Any]] = {}
    for window in query_windows(period):
        prs = gh_json(
            [
                "gh",
                "pr",
                "list",
                "-R",
                slug,
                "--state",
                "merged",
                "--search",
                search_window(window, "merged"),
                "--json",
                PR_FIELDS,
                "--limit",
                str(limit),
            ],
            default=[],
        )
        if query_warnings is not None and len(prs or []) >= limit:
            query_warnings.append(
                f"{repo}: merged PR query hit limit {limit}; "
                "increase ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT if totals look capped"
            )
        for pr in prs or []:
            number = pr.get("number")
            if not isinstance(number, int) or not in_period(pr.get("mergedAt"), period):
                continue
            pr["repo"] = repo
            pr["repo_slug"] = slug
            out[number] = pr
    return list(out.values())


def fetch_issues(
    repo: str,
    period: Period,
    qualifier: str,
    query_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    limit = github_query_limit()
    slug = repo_slug(repo)
    ts_key = "createdAt" if qualifier == "created" else "closedAt"
    out: dict[int, dict[str, Any]] = {}
    for window in query_windows(period):
        issues = gh_json(
            [
                "gh",
                "issue",
                "list",
                "-R",
                slug,
                "--state",
                "all",
                "--search",
                search_window(window, qualifier),
                "--json",
                ISSUE_FIELDS,
                "--limit",
                str(limit),
            ],
            default=[],
        )
        if query_warnings is not None and len(issues or []) >= limit:
            query_warnings.append(
                f"{repo}: {qualifier} issue query hit limit {limit}; "
                "increase ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT if totals look capped"
            )
        for issue in issues or []:
            number = issue.get("number")
            if not isinstance(number, int) or not in_period(issue.get(ts_key), period):
                continue
            issue["repo"] = repo
            issue["repo_slug"] = slug
            out[number] = issue
    return list(out.values())


def fetch_pr_files(repo: str, number: int) -> list[dict[str, Any]]:
    data = gh_json(
        ["gh", "pr", "view", str(number), "-R", repo_slug(repo), "--json", "files"], default={}
    )
    files = data.get("files") if isinstance(data, dict) else []
    return files or []


def path_is_model_related(path: str) -> bool:
    lowered = path.lower()
    return any(hint.lower() in lowered for hint in MODEL_PATH_HINTS)


def model_related_prs(
    prs: list[dict[str, Any]],
    *,
    fetch_files: bool = True,
) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    for pr in prs:
        title = pr.get("title", "")
        title_hit = any(hint.lower() in title.lower() for hint in MODEL_PATH_HINTS)
        files = fetch_pr_files(pr["repo"], pr["number"]) if fetch_files else pr.get("files", [])
        paths = [f.get("path", "") for f in files if f.get("path")]
        path_hits = [p for p in paths if path_is_model_related(p)]
        if title_hit or path_hits:
            item = dict(pr)
            item["model_paths"] = path_hits[:8]
            related.append(item)
    return related


def current_model_defaults() -> list[str]:
    candidates = [
        Path(os.environ["ALFRED_AGENTS_CONF"]) if os.environ.get("ALFRED_AGENTS_CONF") else None,
        Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred"))
        / "launchd"
        / "agents.conf",
        _HERE.parent / "launchd" / "agents.conf",
        _HERE.parent / "launchd" / "agents.conf.example",
    ]
    conf = next((path for path in candidates if path and path.exists()), None)
    if not conf:
        return []
    rows: list[str] = []
    for raw in conf.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        label = parts[0].rsplit(".", 1)[-1]
        model = parts[5].strip()
        if model:
            rows.append(f"{label}={model}")
    return rows


def current_engine_overrides() -> list[str]:
    root = STATE_ROOT / "engines"
    if not root.is_dir():
        return []
    rows: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            rows.append(f"{path.name}={value}")
    return rows


def collect(period: Period, repos: list[str], *, fetch_files: bool = True) -> dict[str, Any]:
    prs: list[dict[str, Any]] = []
    issues_opened: list[dict[str, Any]] = []
    issues_closed: list[dict[str, Any]] = []
    query_warnings: list[str] = []
    for repo in repos:
        try:
            prs.extend(fetch_merged_prs(repo, period, query_warnings))
            issues_opened.extend(fetch_issues(repo, period, "created", query_warnings))
            issues_closed.extend(fetch_issues(repo, period, "closed", query_warnings))
        except ValueError as exc:
            query_warnings.append(str(exc))

    prs.sort(key=lambda item: item.get("mergedAt") or "", reverse=True)
    issues_opened.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    issues_closed.sort(key=lambda item: item.get("closedAt") or "", reverse=True)

    return {
        "period": {
            "label": period.label,
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
        },
        "repos": repos,
        "prs": prs,
        "issues_opened": issues_opened,
        "issues_closed": issues_closed,
        "query_warnings": query_warnings,
        "model_related_prs": model_related_prs(prs, fetch_files=fetch_files),
        "model_defaults": current_model_defaults(),
        "engine_overrides": current_engine_overrides(),
    }


def repo_totals(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for pr in prs:
        repo = pr["repo"]
        row = totals.setdefault(
            repo,
            {"repo": repo, "prs": 0, "additions": 0, "deletions": 0, "changed_files": 0},
        )
        row["prs"] += 1
        row["additions"] += int(pr.get("additions") or 0)
        row["deletions"] += int(pr.get("deletions") or 0)
        row["changed_files"] += int(pr.get("changedFiles") or 0)
    return sorted(totals.values(), key=lambda item: (item["prs"], item["additions"]), reverse=True)


def linked_issue_refs(prs: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for pr in prs:
        for ref in pr.get("closingIssuesReferences") or []:
            repo = ref.get("repository", {}).get("name") or pr["repo"]
            number = ref.get("number")
            url = ref.get("url")
            if not number or not url:
                continue
            key = f"{repo}#{number}"
            if key in seen:
                continue
            seen.add(key)
            out.append(f"{key} {url}")
    return out


def render_slack(data: dict[str, Any]) -> str:
    prs = data["prs"]
    opened = data["issues_opened"]
    closed = data["issues_closed"]
    additions = sum(int(pr.get("additions") or 0) for pr in prs)
    deletions = sum(int(pr.get("deletions") or 0) for pr in prs)
    changed_files = sum(int(pr.get("changedFiles") or 0) for pr in prs)
    lines = [
        f"*Alfred shipped - {data['period']['label']}*",
        (
            f"`{len(prs)} PRs merged | {len(opened)} issues opened | "
            f"{len(closed)} issues closed | +{additions}/-{deletions} LOC | "
            f"{changed_files} files`"
        ),
    ]

    if data.get("query_warnings"):
        lines.append("\n*Query warnings*")
        for warning in data["query_warnings"][:6]:
            lines.append(f"- {warning}")

    totals = repo_totals(prs)
    if totals:
        lines.append("\n*By repo*")
        for row in totals[:8]:
            lines.append(
                f"- `{row['repo']}`: {row['prs']} PRs, "
                f"+{row['additions']}/-{row['deletions']}, {row['changed_files']} files"
            )

    if prs:
        lines.append("\n*Merged PRs*")
        for pr in prs[:12]:
            lines.append(
                f"- `{pr['repo']}#{pr['number']}` {pr['title'][:90]} "
                f"(+{pr.get('additions', 0)}/-{pr.get('deletions', 0)}, "
                f"{pr.get('changedFiles', 0)} files) {pr['url']}"
            )
        if len(prs) > 12:
            lines.append(f"- ...and {len(prs) - 12} more")

    linked = linked_issue_refs(prs)
    if linked:
        lines.append("\n*Issues closed by PRs*")
        for item in linked[:10]:
            lines.append(f"- {item}")
        if len(linked) > 10:
            lines.append(f"- ...and {len(linked) - 10} more")

    if data["model_related_prs"]:
        lines.append("\n*Model/config changes*")
        for pr in data["model_related_prs"][:8]:
            paths = ", ".join(pr.get("model_paths") or ["title match"])
            lines.append(f"- `{pr['repo']}#{pr['number']}` {pr['title'][:80]} ({paths})")
    else:
        lines.append("\n*Model/config changes*: none detected")

    defaults = data.get("model_defaults") or []
    overrides = data.get("engine_overrides") or []
    if defaults:
        suffix = "`" if len(defaults) <= 12 else ", ...`"
        lines.append("\n*Current model defaults*")
        lines.append("`" + ", ".join(defaults[:12]) + suffix)
    if overrides:
        suffix = "`" if len(overrides) <= 12 else ", ...`"
        lines.append("\n*Current engine overrides*")
        lines.append("`" + ", ".join(overrides[:12]) + suffix)

    if not prs and not opened and not closed:
        lines.append("\nNo GitHub shipping activity found for this period.")

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Summarize shipped GitHub activity")
    parser.add_argument("--period", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--days", type=int, help="relative period ending now")
    parser.add_argument("--since", help="start date, YYYY-MM-DD")
    parser.add_argument("--until", help="end date, YYYY-MM-DD")
    parser.add_argument("--repo", action="append", help="repo name or owner/repo, repeatable")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--slack", action="store_true")
    parser.add_argument(
        "--no-file-scan",
        action="store_true",
        help="skip per-PR file lookups for model/config change detection",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    period = resolve_period(args)
    repos = args.repo or configured_repos()
    data = collect(period, repos, fetch_files=not args.no_file_scan)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        msg = render_slack(data)
        print(msg)
        if args.slack:
            slack_post(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
