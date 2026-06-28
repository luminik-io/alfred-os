"""A kanban-shaped view of what the fleet is doing, for humans.

The fleet's work lives as GitHub PRs and issues. A non-developer (or the
operator at a glance) should be able to see *what shipped, what's in flight, and
what's queued* without reading a wall of links. ``build_board`` aggregates the
watched repos into three columns with human context on each card:

    queued       -> open issues (work not yet started)
    in_progress  -> open pull requests (work being built / reviewed)
    shipped       -> pull requests merged within the lookback window

Every card carries title + repo + age + author, not a bare URL, so the Slack
board (PR6) and the native-client Kanban (PR7) can render the same payload.

All GitHub access goes through ``_gh_json`` so the whole module is unit-testable
with a stubbed shell. Repos are resolved from operator config (no hardcoded repo
names), so this module is identical in the public OSS twin.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

from agent_runner.paths import config_value

# Directories where the ``gh`` binary commonly lives. The fleet's cron plists
# already render these into PATH, but the local server is hand-submitted via
# ``launchctl submit`` with a bare PATH (/usr/bin:/bin), so ``gh`` is not found
# and every repo query fails. Resolve ``gh`` against this augmented search path
# so the board works regardless of how its host process was launched. (gh's own
# keyring OAuth token authenticates fine once the binary is reachable; no PAT.)
_GH_EXTRA_PATH = (
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
)

DEFAULT_LOOKBACK_DAYS = 14
_PER_REPO_LIMIT = 50
_DEMO_FILENAME = "setup-demo-cards.json"

# Open issues carrying any of these label substrings are NOT genuine queue
# work: they are already represented by an open PR, or parked for human /
# no-pickup handling, so counting them as "queued" double-counts with the
# in_progress column. Substring + case-insensitive, so it covers agent:pr-open,
# lucius-pr-open, do-not-pickup, needs:human-scope, blocked, etc. Override with
# ALFRED_SHIPPED_QUEUE_EXCLUDE_LABELS (comma-separated).
#
# ``plan-pending-approval`` is the operator-approval gate: a gated single-repo
# plan carries BOTH agent:implement AND agent:plan-pending-approval, so without
# this hint it would pass the include filter and read as "Ready" while the gate
# blocks pickup. Excluding it keeps a blocked issue out of the pickable lane.
_DEFAULT_QUEUE_EXCLUDE = (
    "pr-open",
    "do-not-pickup",
    "needs:human",
    "needs-human",
    "plan-pending-approval",
    "blocked",
    "on-hold",
    "wontfix",
    "wip",
)

_DEFAULT_QUEUE_INCLUDE = ("agent:implement", "agent:large-feature")

_DEFAULT_AGENT_SHIPPED_LABELS = (
    "agent:authored",
    "agent:done",
    "agent:shipped",
    "alfred:shipped",
    "shipped-by-alfred",
)

_DEFAULT_AGENT_BRANCH_PREFIXES = (
    "alfred/",
    "alfred-nightly/",
    "automerge/",
    "bane/",
    "batman/",
    "damian/",
    "lucius/",
    "nightwing/",
    "rasalghul/",
    "robin/",
)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if raw:
        return tuple(v.strip().lower() for v in raw.split(",") if v.strip())
    return default


def _queue_exclude_hints() -> tuple[str, ...]:
    raw = os.environ.get("ALFRED_SHIPPED_QUEUE_EXCLUDE_LABELS", "").strip()
    if raw:
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return _DEFAULT_QUEUE_EXCLUDE


def _queue_include_hints() -> tuple[str, ...]:
    # An open issue must carry one of these labels to count as queue work.
    # Default to the fleet pickup gates (agent:implement for Lucius,
    # agent:large-feature for Batman), because the client is answering "what
    # Alfred can work on", not "what open GitHub issues exist".
    # Set ALFRED_SHIPPED_QUEUE_INCLUDE_LABELS=* for generic deployments that
    # intentionally want every non-parked issue in the queue lane.
    raw = os.environ.get("ALFRED_SHIPPED_QUEUE_INCLUDE_LABELS", "").strip()
    if raw == "*":
        return ()
    if raw:
        return tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return _DEFAULT_QUEUE_INCLUDE


def _in_progress_requires_agent_evidence() -> bool:
    """True when open PRs need Alfred evidence before counting as in progress."""
    raw = os.environ.get("ALFRED_IN_PROGRESS_REQUIRE_AGENT_EVIDENCE")
    return not (raw is not None and not _truthy(raw))


def _shipped_label_hints() -> tuple[str, ...]:
    return _csv_env("ALFRED_SHIPPED_AGENT_LABELS", _DEFAULT_AGENT_SHIPPED_LABELS)


def _shipped_branch_prefixes() -> tuple[str, ...]:
    return _csv_env("ALFRED_SHIPPED_AGENT_BRANCH_PREFIXES", _DEFAULT_AGENT_BRANCH_PREFIXES)


def _shipped_author_hints() -> tuple[str, ...]:
    return _csv_env("ALFRED_SHIPPED_AGENT_AUTHORS")


def _issue_labels(issue: dict) -> list[str]:
    out: list[str] = []
    for lab in issue.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else None
        if name:
            out.append(name.lower())
    return out


def _labels(item: dict) -> list[str]:
    out: list[str] = []
    for lab in item.get("labels") or []:
        name = lab.get("name") if isinstance(lab, dict) else None
        if name:
            out.append(name.lower())
    return out


def _issue_is_parked(issue: dict) -> bool:
    """True if an open issue is already PR-backed or parked (not queue work)."""
    hints = _queue_exclude_hints()
    return any(any(h in label for h in hints) for label in _issue_labels(issue))


def _issue_is_queue_work(issue: dict) -> bool:
    """An open issue is queue work only if it is pickup-ready and not parked.

    "Queued" should read as work Alfred can actually pick up, not the whole
    backlog. By default the issue must carry a Lucius or Batman pickup label;
    operators can override ``ALFRED_SHIPPED_QUEUE_INCLUDE_LABELS`` for different
    pickup gates.
    """
    if _issue_is_parked(issue):
        return False
    include = _queue_include_hints()
    if not include:
        return True
    labels = _issue_labels(issue)
    return any(any(h in label for h in include) for label in labels)


def _agent_shipped_evidence(pr: dict) -> list[str]:
    """Return the Alfred evidence found on a merged PR, newest UI can surface it."""
    evidence: list[str] = []
    labels = _labels(pr)
    label_hints = _shipped_label_hints()
    for label in labels:
        if any(hint in label for hint in label_hints):
            evidence.append(f"label:{label}")

    branch = (pr.get("headRefName") or "").strip().lower()
    if branch and any(branch.startswith(prefix) for prefix in _shipped_branch_prefixes()):
        evidence.append(f"branch:{branch}")

    author = (_author_login(pr) or "").strip().lower()
    if author and author in _shipped_author_hints():
        evidence.append(f"author:{author}")

    return evidence


def _pr_is_agent_shipped(pr: dict) -> bool:
    """True only when the PR carries an agent label.

    The agent label (``agent:authored``, applied by ``gh_pr_create`` when the
    fleet opens the PR, plus ``agent:done`` / ``agent:shipped`` set on merge) is
    the authoritative signal, the same one issue pickup and
    ``find_open_authored_pr_for_issue`` already scope agent work by. A matching
    branch prefix or author is still recorded by ``_agent_shipped_evidence`` for
    display, but no longer qualifies a PR on its own, so a human PR pushed to a
    codename-style branch (or a stale ``automerge/`` branch) is not miscounted as
    agent-shipped.
    """
    labels = _labels(pr)
    label_hints = _shipped_label_hints()
    return any(any(hint in label for hint in label_hints) for label in labels)


def _now() -> datetime:
    """Current UTC time. Indirected so tests can pin it."""
    return datetime.now(UTC)


def _config_value(key: str) -> str:
    """Resolve a config value from the connected ``alfred serve`` runtime.

    Kept as a thin alias so existing ``shipped_board._config_value`` imports
    keep working; ``GET /api/shipped`` uses it to resolve watched repos with
    the same config setup status reports for the connected runtime.
    """
    return config_value(key)


def _gh_bin() -> str:
    """Absolute path to the ``gh`` binary, resolved against an augmented search
    path so a bare-PATH host still finds it. Falls back to the bare name."""
    configured = _config_value("ALFRED_GH_BIN") or _config_value("GH_BIN")
    if configured:
        return configured
    search = os.pathsep.join((*_GH_EXTRA_PATH, os.environ.get("PATH", "")))
    return shutil.which("gh", path=search) or "gh"


def _gh_subprocess_env() -> dict[str, str]:
    """Process env with the gh/git bin dirs on PATH, so any subprocess ``gh``
    itself spawns (e.g. ``git``) is also found on a bare-PATH host."""
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for extra in reversed(_GH_EXTRA_PATH):
        if extra not in parts:
            parts.insert(0, extra)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _gh_json(args: list[str], *, timeout: int = 30) -> Any:
    """Run a ``gh`` command with ``--json`` output and return parsed JSON.

    Returns ``None`` on any failure (missing gh, auth error, rate limit, bad
    repo) so a single flaky repo never breaks the whole board.
    """
    try:
        proc = subprocess.run(
            [_gh_bin(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_gh_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def resolve_repos(explicit: list[str] | None = None) -> list[str]:
    """Resolve the watched repo list (``owner/name`` slugs), config-driven.

    Precedence: explicit arg -> ``ALFRED_SHIPPED_REPOS`` -> ``ALFRED_BRIDGE_REPOS``
    (both comma-separated) -> ``gh repo list <GH_ORG>``. Each env knob also falls
    back to ``$ALFRED_HOME/.env`` (see ``_config_value``) so the launchd-managed
    local server resolves the same repos as the rest of the fleet. No repo names
    are hardcoded, so the public twin behaves identically.
    """
    if explicit:
        return [r.strip() for r in explicit if r.strip()]
    for env_name in ("ALFRED_SHIPPED_REPOS", "ALFRED_BRIDGE_REPOS"):
        raw = _config_value(env_name)
        if raw:
            return [r.strip() for r in raw.split(",") if r.strip()]
    org = _config_value("GH_ORG")
    if org:
        rows = (
            _gh_json(
                [
                    "repo",
                    "list",
                    org,
                    "--no-archived",
                    "--limit",
                    "100",
                    "--json",
                    "nameWithOwner",
                ]
            )
            or []
        )
        return [r["nameWithOwner"] for r in rows if r.get("nameWithOwner")]
    return []


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_days(ts: datetime | None, *, now: datetime) -> int | None:
    if ts is None:
        return None
    return max(0, (now - ts).days)


def _author_login(obj: dict) -> str | None:
    author = obj.get("author") or {}
    return author.get("login") if isinstance(author, dict) else None


def _card(repo: str, item: dict, *, kind: str, ts_field: str, now: datetime) -> dict:
    ts = _parse_ts(item.get(ts_field))
    return {
        "repo": repo,
        "number": item.get("number"),
        "title": (item.get("title") or "").strip(),
        "url": item.get("url"),
        "author": _author_login(item),
        "kind": kind,
        "timestamp": item.get(ts_field),
        "age_days": _age_days(ts, now=now),
        "is_draft": bool(item.get("isDraft", False)),
        "labels": [
            lab.get("name")
            for lab in (item.get("labels") or [])
            if isinstance(lab, dict) and lab.get("name")
        ],
        "agent_evidence": _agent_shipped_evidence(item) if kind == "pr" else [],
    }


def _fetch_repo(
    repo: str, *, cutoff: float, now: datetime, limit: int
) -> tuple[str, list[dict], list[dict], list[dict], bool]:
    """Query one repo's PRs + issues. Returns
    ``(repo, queued, in_progress, shipped, errored)``. ``errored`` is True if
    either gh call failed, so the caller records it without losing the cards it
    did get. Pure per-repo work, safe to run concurrently across repos.
    """
    queued: list[dict] = []
    in_progress: list[dict] = []
    shipped: list[dict] = []
    errored = False

    prs = _gh_json(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,author,state,createdAt,mergedAt,isDraft,labels,headRefName",
        ]
    )
    if prs is None:
        errored = True
    else:
        for pr in prs:
            if pr.get("state") == "OPEN" and (
                not _in_progress_requires_agent_evidence() or _pr_is_agent_shipped(pr)
            ):
                in_progress.append(_card(repo, pr, kind="pr", ts_field="createdAt", now=now))
            elif pr.get("mergedAt"):
                merged = _parse_ts(pr.get("mergedAt"))
                if merged and merged.timestamp() >= cutoff and _pr_is_agent_shipped(pr):
                    shipped.append(_card(repo, pr, kind="pr", ts_field="mergedAt", now=now))

    issues = _gh_json(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,author,createdAt,labels",
        ]
    )
    if issues is None:
        errored = True
    else:
        for issue in issues:
            # "Queued" must mean work Alfred can actually pick up, not the whole
            # backlog: skip PR-backed / parked issues, and (when an include-label
            # is configured) require a pickup-ready label so roadmap / needs-info
            # items don't read as queued.
            if _issue_is_queue_work(issue):
                queued.append(_card(repo, issue, kind="issue", ts_field="createdAt", now=now))

    return repo, queued, in_progress, shipped, errored


def _demo_cards() -> dict[str, list[dict]]:
    """Locally seeded demo cards to merge into the board, or empty columns."""
    empty: dict[str, list[dict]] = {"queued": [], "in_progress": [], "shipped": []}
    try:
        from server.setup import load_demo_cards

        return load_demo_cards()
    except Exception:  # pragma: no cover - defensive: demo store is optional
        pass
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    path = os.path.join(base, "state", _DEMO_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (OSError, json.JSONDecodeError):
        return empty
    columns = payload.get("columns") if isinstance(payload, dict) else None
    if not isinstance(columns, dict):
        return empty
    out = {**empty}
    for key in out:
        value = columns.get(key)
        if isinstance(value, list):
            out[key] = [card for card in value if isinstance(card, dict)]
    return out


def build_board(
    repos: list[str],
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = _PER_REPO_LIMIT,
    now: datetime | None = None,
    include_demo: bool = False,
) -> dict[str, Any]:
    """Aggregate ``repos`` into {queued, in_progress, shipped} columns.

    Each column is newest-first. Missing/erroring repos are skipped and recorded
    in ``errors`` rather than failing the whole board. Repos are fetched
    concurrently (the gh calls are independent and I/O-bound), so a 10-repo board
    completes in roughly the time of the slowest single repo instead of the sum,
    keeping it well under the client's fetch timeout.

    Locally seeded demo cards (``$ALFRED_HOME/state``) are merged ONLY when
    ``include_demo`` is set. It defaults off so the live board reflects real
    fleet work and the aggregator stays hermetic regardless of the operator's
    seeded demo state; ``/api/shipped`` opts in via ``?demo=1`` when the client
    explicitly wants the sample cards.
    """
    now = now or _now()
    cutoff = now.timestamp() - max(1, days) * 86400
    queued: list[dict] = []
    in_progress: list[dict] = []
    shipped: list[dict] = []
    errors: list[str] = []

    if include_demo:
        demo = _demo_cards()
        queued.extend(demo.get("queued", []))
        in_progress.extend(demo.get("in_progress", []))
        shipped.extend(demo.get("shipped", []))

    if repos:
        max_workers = min(len(repos), 8)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_fetch_repo, repo, cutoff=cutoff, now=now, limit=limit)
                for repo in repos
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    repo, q, ip, sh, errored = fut.result()
                except Exception:  # never let one repo's failure break the board
                    continue
                queued.extend(q)
                in_progress.extend(ip)
                shipped.extend(sh)
                if errored:
                    errors.append(repo)

    def _sort(cards: list[dict]) -> list[dict]:
        return sorted(cards, key=lambda c: c.get("timestamp") or "", reverse=True)

    counts = {
        "queued": len(queued),
        "in_progress": len(in_progress),
        "shipped": len(shipped),
    }
    unique_errors = sorted(set(errors))
    result = {
        "generated_at": now.isoformat(),
        "lookback_days": days,
        "repos": repos,
        "columns": {
            "queued": _sort(queued),
            "in_progress": _sort(in_progress),
            "shipped": _sort(shipped),
        },
        "counts": counts,
        "errors": unique_errors,
    }
    watched_repos = {repo for repo in repos if repo}
    if watched_repos and set(unique_errors).issuperset(watched_repos) and not any(counts.values()):
        shown = ", ".join(unique_errors[:3])
        more = f", +{len(unique_errors) - 3} more" if len(unique_errors) > 3 else ""
        result["error"] = (
            f"GitHub data unavailable for {len(unique_errors)} watched "
            f"repo{'s' if len(unique_errors) != 1 else ''}: {shown}{more}"
        )
    return result
