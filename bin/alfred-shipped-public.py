#!/usr/bin/env python3
"""Emit a public-safe shipped feed for self-hosted proof pages.

Usage:
    alfred-shipped-public.py --emit-public-json weekly.json
    alfred-shipped-public.py --emit-public-json - --operator your-org
    alfred-shipped-public.py --emit-public-json out.json --state ~/.alfred/state

Reads Alfred state under ``$ALFRED_HOME/state/`` (default ``~/.alfred/state``)
and produces a versioned JSON document that operators can publish on their
own site if they want a public rolling-proof page. The emitter is the
privacy boundary: nothing leaves this process unless it is in the public
allowlist below. The canonical schema lives at ``schema/weekly.schema.json``.

12-factor: env-driven defaults (``ALFRED_HOME``, ``ALFRED_PUBLIC_OPERATOR``,
``ALFRED_PUBLIC_REPO_ALLOWLIST``), file output via CLI (``--emit-public-json
PATH`` or ``-`` for stdout), structured logging to stderr.

Privacy contract:
- Per PR, only the fields in ``PR_ALLOWED_FIELDS`` are emitted.
- PR diffs and issue bodies are never read or emitted.
- Human reviewer GitHub handles collapse to the literal string ``human``.
- A repo passes through unchanged only if it is in the allowlist or if
  no allowlist is configured AND the repo does not match
  ``PRIVATE_REPO_PATTERNS`` (which catches the Luminik internal repo
  names that must not leak to the public feed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Fields the emitter is allowed to copy from a state record to the public
# feed. Anything else is dropped. This is the explicit allowlist; new
# additions need a deliberate code change.
PR_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "repo",
        "number",
        "title",
        "codename",
        "merged_at",
        "lines_added",
        "lines_removed",
        "files_changed",
        "reviewed_by",
        "url",
    }
)

KNOWN_CODENAMES: frozenset[str] = frozenset(
    {
        "lucius",
        "batman",
        "drake",
        "robin",
        "ras-al-ghul",
        "rasalghul",
        "bane",
        "nightwing",
        "huntress",
        "damian",
        "gordon",
        "human",
    }
)

# Repos that must never appear in a public feed. The list mirrors the
# private/public boundary doc; the operator can override by setting
# ``ALFRED_PUBLIC_REPO_ALLOWLIST``, but a match here always denies even
# in that case.
# Private product-repo basenames that must never appear in a public feed.
# Listed by basename so the source file does not carry a literal "owner/name"
# pair; the emitter denies any owner/name where the name matches.
_PRIVATE_NAMES: tuple[str, ...] = (
    "backend",
    "frontend",
    "mobile",
    "nango",
    "agents",
    "data-acquisition",
    "data-infra",
    "specs",
    "site",
    "design-system",
    "orchestrator",
    "internal",
)
_PRIVATE_PREFIX = "luminik-"

PRIVATE_REPO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:^|/)"
        + re.escape(_PRIVATE_PREFIX)
        + r"(?:"
        + "|".join(_PRIVATE_NAMES)
        + r")"
        + r"(?:$|[^A-Za-z0-9_-])"
    ),
    # Bare-name "alfred" basename under any owner (a former internal repo,
    # not alfred-os which is public). The basename match is intentional;
    # alfred-os carries the "-os" suffix and is unaffected.
    re.compile(r"(?:^|/)alfred(?:$|[^-A-Za-z0-9_])"),
)

# A title may carry a private repo name in plain text; this regex
# replaces those with the neutral placeholder.
PRIVATE_TOKEN_RE = re.compile(
    r"luminik-(backend|frontend|mobile|nango|agents|data-acquisition|data-infra|specs|site|design-system|orchestrator|internal)",
    re.IGNORECASE,
)

# Partner-name redaction. Real PR titles from operators frequently mention the
# external platform an integration targets (event-data platforms, CRMs, mail
# providers). These names are operator-private business context even when the
# platform itself is public, so the emitter neutralises them to category words
# before publishing. Extend this list as new integrations land.
PARTNER_TOKENS: dict[str, str] = {
    # Event-data platforms (Spec 28 extractors)
    "Brella": "vendor",
    "Cvent": "vendor",
    "Grip": "vendor",
    "Swapcard": "vendor",
    "Whova": "vendor",
    "Eventbrite": "vendor",
    "Hopin": "vendor",
    "Bizzabo": "vendor",
    "Pheedloop": "vendor",
    # CRMs and outreach platforms
    "Salesforce": "CRM",
    "HubSpot": "CRM",
    "Apollo": "outreach platform",
    "Outreach": "outreach platform",
    "Salesloft": "outreach platform",
    # Mail / observability
    "Resend": "email provider",
    "Sendgrid": "email provider",
    "Mailgun": "email provider",
    "Postmark": "email provider",
    "Sentry": "error tracker",
    "Datadog": "telemetry",
    "Honeycomb": "telemetry",
    # Auth / SSO
    "WorkOS": "SSO",
    "Auth0": "SSO",
    "Clerk": "SSO",
}

PARTNER_TOKEN_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in PARTNER_TOKENS) + r")\b",
    re.IGNORECASE,
)

PLACEHOLDER_REPO_MAP: dict[str, str] = {
    "backend": "your-backend",
    "frontend": "your-frontend",
    "mobile": "your-mobile",
    "nango": "your-nango",
    "agents": "your-agents",
    "data-acquisition": "your-data-acquisition",
    "data-infra": "your-data-infra",
    "specs": "your-specs",
    "site": "your-site",
    "design-system": "your-design-system",
    "orchestrator": "your-orchestrator",
    "internal": "your-internal",
}

REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

log = logging.getLogger("alfred-shipped-public")


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def to_dict(self) -> dict[str, str]:
        return {"from": _iso(self.start), "to": _iso(self.end)}


@dataclass
class PublicPr:
    repo: str
    number: int
    title: str
    codename: str
    merged_at: str
    lines_added: int
    lines_removed: int
    files_changed: int
    reviewed_by: list[str] = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "codename": self.codename,
            "merged_at": self.merged_at,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "files_changed": self.files_changed,
            "reviewed_by": list(self.reviewed_by),
            "url": self.url,
        }


@dataclass
class WeeklyFeed:
    version: int
    generated_at: str
    operator: str
    window: Window
    summary: dict[str, int]
    trend: list[dict[str, Any]]
    prs: list[PublicPr]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "operator": self.operator,
            "window": self.window.to_dict(),
            "summary": dict(self.summary),
            "trend": list(self.trend),
            "prs": [p.to_dict() for p in self.prs],
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def default_window(now: datetime) -> Window:
    """Last 7 days ending at the current UTC midnight."""
    end = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), tzinfo=UTC)
    start = end - timedelta(days=7)
    return Window(start=start, end=end)


def alfred_state_root() -> Path:
    raw = os.environ.get("ALFRED_HOME") or "~/.alfred"
    return Path(os.path.expanduser(raw)) / "state"


def configured_repo_allowlist() -> list[str]:
    raw = os.environ.get("ALFRED_PUBLIC_REPO_ALLOWLIST", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def configured_operator(fallback: str = "your-org") -> str:
    return (os.environ.get("ALFRED_PUBLIC_OPERATOR") or fallback).strip() or fallback


def is_private_repo(repo: str) -> bool:
    return any(pat.search(repo) for pat in PRIVATE_REPO_PATTERNS)


def scrub_title(title: str) -> str:
    """Scrub a free-text PR title for public emission.

    Applies two passes:

    1. ``PRIVATE_TOKEN_RE``: any literal private repo token (an operator-private
       basename such as the ones listed in the regex above) is replaced with
       the matching ``your-*`` placeholder.
    2. ``PARTNER_TOKEN_RE``: third-party platform names that the operator
       integrates with (event-data platforms, CRMs, mail / observability
       providers, SSO) collapse to a neutral category word so the public title
       reads as feature texture rather than business context.

    The order matters: repo-name redaction runs first so a title that mentions
    both a partner (e.g. a CRM vendor) and a private repo token becomes a
    fully neutralised title like ``CRM action in your-nango``.
    """

    def repl_private(match: re.Match[str]) -> str:
        token = match.group(1).lower()
        return PLACEHOLDER_REPO_MAP.get(token, "your-repo")

    def repl_partner(match: re.Match[str]) -> str:
        matched = match.group(1)
        for canonical, replacement in PARTNER_TOKENS.items():
            if canonical.lower() == matched.lower():
                return replacement
        return matched

    out = PRIVATE_TOKEN_RE.sub(repl_private, title or "")
    out = PARTNER_TOKEN_RE.sub(repl_partner, out)
    return out


def scrub_reviewer(name: str) -> str:
    """Reviewer codenames pass through; everything else collapses to `human`."""
    norm = (name or "").strip().lower()
    if not norm:
        return "human"
    if norm in KNOWN_CODENAMES:
        return norm
    return "human"


def normalize_codename(name: str) -> str:
    norm = (name or "").strip().lower()
    if not norm:
        return "human"
    if norm in KNOWN_CODENAMES:
        return norm
    # Unknown codenames render as `agent` so the table still shows them
    # as machine work, not as a human merge. This protects against future
    # codename additions leaking before they're allowlisted here.
    return "agent"


def in_window(merged_at: str, window: Window) -> bool:
    ts = _parse_iso(merged_at)
    if ts is None:
        return False
    return window.start <= ts < window.end


def iso_week(dt: datetime) -> str:
    iso = dt.astimezone(UTC).isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


# --------------------------------------------------------------------------
# State reading
# --------------------------------------------------------------------------


def read_state_prs(state_root: Path) -> list[dict[str, Any]]:
    """Load PR records from ~/.alfred/state/.

    The expected on-disk shape is ``state_root/shipped/prs.json`` which
    is a list of dicts. If the file is missing or unreadable we return
    an empty list; cold-fork mode handles that downstream.
    """
    candidates = [
        state_root / "shipped" / "prs.json",
        state_root / "shipped.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s: %s", path, exc)
            continue
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict) and isinstance(data.get("prs"), list):
            return [row for row in data["prs"] if isinstance(row, dict)]
    log.info("no state PR file found under %s", state_root)
    return []


def read_state_trend(state_root: Path) -> list[dict[str, Any]]:
    path = state_root / "shipped" / "trend.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read trend file %s: %s", path, exc)
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


# --------------------------------------------------------------------------
# Core transform
# --------------------------------------------------------------------------


def filter_repo(repo: str, allowlist: Iterable[str]) -> bool:
    """Decide whether a repo's PRs are allowed into the public feed."""
    if not REPO_SLUG_RE.match(repo):
        return False
    if is_private_repo(repo):
        return False
    allow = list(allowlist)
    if not allow:
        return True
    return repo in allow


def to_public_pr(raw: dict[str, Any]) -> PublicPr | None:
    """Apply the field allowlist and per-field scrubbers."""
    scrubbed: dict[str, Any] = {k: v for k, v in raw.items() if k in PR_ALLOWED_FIELDS}

    # Required fields with sensible coercion.
    try:
        number = int(scrubbed.get("number") or 0)
    except (TypeError, ValueError):
        return None
    repo = str(scrubbed.get("repo") or "")
    merged_at = str(scrubbed.get("merged_at") or "")
    url = str(scrubbed.get("url") or "")
    if not repo or not number or not merged_at:
        return None

    reviewed_raw = scrubbed.get("reviewed_by") or []
    if not isinstance(reviewed_raw, list):
        reviewed_raw = []
    reviewed = [scrub_reviewer(str(r)) for r in reviewed_raw]

    return PublicPr(
        repo=repo,
        number=number,
        title=scrub_title(str(scrubbed.get("title") or "")),
        codename=normalize_codename(str(scrubbed.get("codename") or "")),
        merged_at=merged_at,
        lines_added=int(scrubbed.get("lines_added") or 0),
        lines_removed=int(scrubbed.get("lines_removed") or 0),
        files_changed=int(scrubbed.get("files_changed") or 0),
        reviewed_by=reviewed,
        url=url,
    )


def compute_summary(prs: list[PublicPr], extra: dict[str, Any] | None = None) -> dict[str, int]:
    extra = extra or {}
    prs_reverted = int(extra.get("prs_reverted") or 0)
    issues_closed = int(extra.get("issues_closed") or 0)
    agents_active = int(extra.get("agents_active") or 0)
    spend_cents = int(extra.get("spend_cents") or 0)
    repos_touched = len({pr.repo for pr in prs})
    prs_merged = len(prs)
    if prs_merged == 0:
        merge_clean_pct = 100 if prs_reverted == 0 else 0
    else:
        clean = max(0, prs_merged - prs_reverted)
        merge_clean_pct = round(100 * clean / prs_merged)
    return {
        "prs_merged": prs_merged,
        "prs_reverted": prs_reverted,
        "issues_closed": issues_closed,
        "agents_active": agents_active,
        "repos_touched": repos_touched,
        "spend_cents": spend_cents,
        "merge_clean_pct": max(0, min(100, merge_clean_pct)),
    }


def compute_trend(prs: list[PublicPr], window: Window, weeks: int = 12) -> list[dict[str, Any]]:
    """Bucket merged PRs into ISO weeks ending at the window end."""
    buckets: dict[str, int] = {}
    end_day = window.end.date()
    # Initialise the last `weeks` ISO weeks at zero so flat weeks still show.
    for i in range(weeks):
        day = end_day - timedelta(days=7 * (weeks - 1 - i))
        wk = iso_week(datetime.combine(day, datetime.min.time(), tzinfo=UTC))
        buckets.setdefault(wk, 0)

    for pr in prs:
        ts = _parse_iso(pr.merged_at)
        if ts is None:
            continue
        wk = iso_week(ts)
        if wk in buckets:
            buckets[wk] += 1
    return [{"week": wk, "prs_merged": buckets[wk]} for wk in sorted(buckets.keys())]


def merge_trend_with_state(
    computed: list[dict[str, Any]],
    state_trend: list[dict[str, Any]],
    weeks: int = 12,
) -> list[dict[str, Any]]:
    """Prefer state-supplied trend rows over computed ones when present."""
    by_week: dict[str, int] = {row["week"]: row["prs_merged"] for row in computed}
    for row in state_trend:
        wk = row.get("week")
        if not isinstance(wk, str):
            continue
        try:
            value = int(row.get("prs_merged") or 0)
        except (TypeError, ValueError):
            continue
        if wk in by_week:
            by_week[wk] = value
    return [{"week": wk, "prs_merged": by_week[wk]} for wk in sorted(by_week.keys())][-weeks:]


def build_feed(
    raw_prs: list[dict[str, Any]],
    *,
    operator: str,
    window: Window,
    allowlist: list[str],
    summary_extra: dict[str, Any] | None = None,
    state_trend: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> WeeklyFeed:
    now = now or datetime.now(tz=UTC)
    public_prs: list[PublicPr] = []
    for raw in raw_prs:
        repo = str(raw.get("repo") or "")
        if not filter_repo(repo, allowlist):
            log.info("dropped PR from non-allowlisted repo: %s", repo or "<missing>")
            continue
        merged_at = str(raw.get("merged_at") or "")
        if not in_window(merged_at, window):
            continue
        pub = to_public_pr(raw)
        if pub is not None:
            public_prs.append(pub)

    public_prs.sort(key=lambda pr: pr.merged_at, reverse=True)

    trend = compute_trend(public_prs, window)
    if state_trend:
        trend = merge_trend_with_state(trend, state_trend)

    return WeeklyFeed(
        version=SCHEMA_VERSION,
        generated_at=_iso(now),
        operator=operator,
        window=window,
        summary=compute_summary(public_prs, summary_extra),
        trend=trend,
        prs=public_prs,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred-shipped-public",
        description="Emit a public-safe shipped JSON feed for the /shipped/ page.",
    )
    parser.add_argument(
        "--emit-public-json",
        metavar="PATH",
        required=True,
        help="output file. Use '-' for stdout.",
    )
    parser.add_argument(
        "--state",
        metavar="DIR",
        help="override the Alfred state root (defaults to $ALFRED_HOME/state).",
    )
    parser.add_argument(
        "--operator",
        help="operator display name (defaults to $ALFRED_PUBLIC_OPERATOR or 'your-org').",
    )
    parser.add_argument(
        "--public-allowlist",
        action="append",
        metavar="REPO",
        help="repo slug allowed in the public feed. Repeatable. Overrides $ALFRED_PUBLIC_REPO_ALLOWLIST.",
    )
    parser.add_argument(
        "--since",
        help="window start, YYYY-MM-DD UTC. Defaults to 7 days before --until.",
    )
    parser.add_argument(
        "--until",
        help="window end, YYYY-MM-DD UTC. Defaults to today UTC.",
    )
    parser.add_argument(
        "--summary-extra",
        metavar="PATH",
        help="path to a JSON file supplying prs_reverted/issues_closed/agents_active/spend_cents overrides.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress informational stderr logging (warnings still emitted).",
    )
    return parser


def parse_window(args: argparse.Namespace, now: datetime) -> Window:
    if not args.since and not args.until:
        return default_window(now)
    until_day = date.fromisoformat(args.until) if args.until else now.astimezone(UTC).date()
    since_day = date.fromisoformat(args.since) if args.since else until_day - timedelta(days=7)
    start = datetime.combine(since_day, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(until_day, datetime.min.time(), tzinfo=UTC)
    if end <= start:
        end = start + timedelta(days=1)
    return Window(start=start, end=end)


def write_output(path_str: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    if path_str == "-":
        sys.stdout.write(text)
        return
    out_path = Path(path_str).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    now = datetime.now(tz=UTC)
    window = parse_window(args, now)
    state_root = Path(args.state).expanduser() if args.state else alfred_state_root()
    operator = (args.operator or configured_operator()).strip()
    allowlist = list(args.public_allowlist or configured_repo_allowlist())

    raw_prs = read_state_prs(state_root)
    state_trend = read_state_trend(state_root)

    summary_extra: dict[str, Any] = {}
    if args.summary_extra:
        try:
            summary_extra = json.loads(Path(args.summary_extra).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read summary-extra file: %s", exc)

    feed = build_feed(
        raw_prs,
        operator=operator,
        window=window,
        allowlist=allowlist,
        summary_extra=summary_extra,
        state_trend=state_trend,
        now=now,
    )

    log.info(
        "emitting feed: %s PRs, %s repos, window %s to %s",
        feed.summary["prs_merged"],
        feed.summary["repos_touched"],
        feed.window.start.date(),
        feed.window.end.date(),
    )

    write_output(args.emit_public_json, feed.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
