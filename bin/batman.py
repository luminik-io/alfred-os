#!/usr/bin/env python3
"""``batman`` — multi-repo feature coordinator.

Picks the oldest open ``agent:large-feature`` issue across the
configured product repos. When the issue carries an
``agent:bundle:<slug>`` label, every sibling issue sharing that label
is pulled in and the resulting bundle is treated as the atomic unit.

Bundle primitives live in ``lib/batman.py`` so the parsing /
claim-rollback / plan-shape logic stays unit-testable. This file is the
runner skeleton: preflight, find a bundle, post a plan summary, exit.
The full execution chain (worktrees + Claude invocation + cross-repo
PR chaining + founder approval gate) is intentionally NOT in alfred-os
yet — those bits depend on alfred-private infra (cross_repo_pr,
multi_worktree, slack_approval) and arrive in a follow-up commit.

Wiring:

  - Reads ``GH_ORG`` from the environment.
  - Reads ``BATMAN_SCAN_REPOS`` (comma-separated) for the search scope.
    Defaults to "no scan" when unset, so a fresh install with nothing
    configured is a no-op rather than a crash.
  - Posts a plan summary via the ``slack_format`` thread root when a
    bot token is configured, falling back to the legacy webhook
    ``slack_post`` otherwise.
  - Honours the fleet enable file: if ``batman`` is not enabled there,
    the runner exits early with a one-line stderr note.

Backport of luminik-io/alfred PR #115 + PR #127. Skeleton-only — the
operator can extend the per-repo execution chain to taste.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "lib", Path(os.environ.get("HERMES_HOME", "")) / "lib"):
    if candidate.exists():
        candidate_path = str(candidate)
        if candidate_path in sys.path:
            sys.path.remove(candidate_path)
        sys.path.insert(0, candidate_path)

from agent_runner import (  # noqa: E402
    GH_ORG,
    GH_REPO_TO_LOCAL,
    PreflightSpec,
    doctor_mode,
    gh_json,
    is_agent_enabled,
    preflight,
    slack_post,
    with_lock,
)
from batman import (  # noqa: E402
    BUNDLE_LABEL_PREFIX,
    LARGE_FEATURE_LABEL,
    Bundle,
    list_issues_by_bundle_label,
    parse_plan_from_bundle,
)
from slack_format import firing_thread_root  # noqa: E402

CODENAME = "batman"


def _scan_repos() -> list[str]:
    """Comma-separated list of local repo names Batman searches.

    Empty when unset — a fresh install opts out of cross-repo
    discovery until the operator wires it explicitly. This keeps the
    skeleton runner from blasting ``gh search`` against nothing
    sensible.
    """
    raw = (os.environ.get("BATMAN_SCAN_REPOS") or "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _repo_arg_for_scan_token(token: str) -> str | None:
    """Return the ``owner/repo`` value passed to ``gh search --repo``."""
    token = token.strip()
    if not token:
        return None
    if "/" in token:
        return token
    repo_slug = next(
        (
            github_repo
            for github_repo, local_repo in GH_REPO_TO_LOCAL.items()
            if token in {github_repo, local_repo}
        ),
        token,
    )
    return f"{GH_ORG}/{repo_slug}" if GH_ORG else None


def _list_large_features() -> list[dict]:
    """Cross-repo search for open ``agent:large-feature`` issues.

    Filters out issues that look claimed (``agent:in-flight``) or
    blocked (``do-not-pickup``). Returns ``[]`` on missing GH_ORG or
    empty ``BATMAN_SCAN_REPOS`` so a half-configured fleet exits
    cleanly instead of org-wide blasting issues into the picker.

    The post-search filter (URL prefix match) keeps results scoped to
    the operator-configured repos even though ``gh search`` only takes
    ``--owner``, not ``--repo`` per call. Hits in repos outside the
    scan list are dropped silently.
    """
    if not GH_ORG:
        return []
    repo_args = [_repo_arg_for_scan_token(repo) for repo in _scan_repos()]
    repo_args = [repo for repo in repo_args if repo]
    if not repo_args:
        return []
    cmd = ["gh", "search", "issues"]
    for repo in repo_args:
        cmd.extend(["--repo", repo])
    cmd.extend(
        [
            "--label",
            LARGE_FEATURE_LABEL,
            "--state",
            "open",
            "--json",
            "number,title,url,labels,createdAt,body",
            "--limit",
            "20",
        ]
    )
    rows = gh_json(cmd, default=[])
    if not isinstance(rows, list):
        return []
    allowed_prefixes = tuple(f"https://github.com/{repo}/" for repo in repo_args)
    skip_labels = {"agent:in-flight", "agent:pr-open", "do-not-pickup"}
    eligible: list[dict] = []
    for r in rows:
        url = r.get("url") or ""
        if not url.startswith(allowed_prefixes):
            continue
        labels = {label.get("name") for label in r.get("labels", []) if isinstance(label, dict)}
        if labels & skip_labels:
            continue
        eligible.append(r)
    return eligible


def _bundle_label(issue: dict) -> str | None:
    for label in issue.get("labels", []):
        if isinstance(label, dict):
            name = label.get("name") or ""
            if name.startswith(BUNDLE_LABEL_PREFIX):
                return name
    return None


def _bundle_for_issue(issue: dict) -> Bundle:
    """Resolve the full bundle for an issue.

    If the issue carries an ``agent:bundle:<slug>`` label, every
    sibling sharing that label is pulled in. Otherwise it's a bundle
    of one — the issue itself.
    """
    label = _bundle_label(issue)
    if not label:
        return Bundle(issues=[issue], bundle_label=None)
    siblings = list_issues_by_bundle_label(label)
    by_url = {s.get("url"): s for s in siblings if s.get("url")}
    by_url[issue.get("url")] = issue  # always include the trigger issue
    return Bundle(issues=list(by_url.values()), bundle_label=label)


def _firing_id() -> str:
    import secrets

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def main() -> int:
    if doctor_mode():
        print("[BATMAN-DOCTOR-OK]")
        return 0

    if not is_agent_enabled(CODENAME, default=False):
        print(
            f"[BATMAN-SKIP] {CODENAME} not enabled in fleet file; "
            f"run `alfred enable {CODENAME}` to opt in.",
            file=sys.stderr,
        )
        return 0

    spec = PreflightSpec(
        agent=CODENAME,
        env_vars=["HERMES_HOME", "WORKSPACE_ROOT", "GH_ORG"],
        bins=["gh", "git"],
        require_gh_auth=True,
    )
    try:
        preflight(spec)
    except Exception as e:
        print(f"[BATMAN-PREFLIGHT-FAIL] {e}", file=sys.stderr)
        return 0

    with with_lock(CODENAME):
        issues = _list_large_features()
        if not issues:
            print("[BATMAN-NOOP] no eligible agent:large-feature issues")
            return 0
        # Oldest first.
        issues.sort(key=lambda i: i.get("createdAt", ""))
        bundle = _bundle_for_issue(issues[0])
        plan = parse_plan_from_bundle(bundle)

        firing_id = _firing_id()
        primary = bundle.primary_issue
        summary = (
            f"plan drafted for {bundle.slug} "
            f"({len(bundle.issues)} issue(s), {len(plan.affected_repos)} repo(s))"
        )
        body = (
            f"*Issue:* <{primary.get('url')}|{primary.get('title')}>\n"
            f"*Bundle:* `{bundle.slug}`\n"
            f"*Affected repos:* {', '.join(plan.affected_repos) or '(none)'}\n"
            f"*Rollout order:* {' → '.join(plan.affected_repos) or '(default)'}\n"
        )
        # Try the bot-token thread root first; fall back to the
        # webhook surface so the operator gets *some* visibility on
        # fleets without a bot token configured.
        handle = firing_thread_root(
            codename=CODENAME,
            firing_id=firing_id,
            summary_one_liner=summary,
            severity="info",
            body=body,
        )
        if handle is None:
            slack_post(f"[BATMAN-PLAN-DRAFTED] {summary}\n{body}", severity="info")

        print(f"[BATMAN-PLAN-DRAFTED] firing_id={firing_id} bundle={bundle.slug}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
