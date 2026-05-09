"""Bundle primitives for Batman, the multi-repo feature coordinator.

Batman picks ``agent:bundle:<slug>`` bundles across product repos,
drafts plans, and executes coordinated multi-repo PR chains. This
module is the pure-data part: ``Bundle`` dataclass, claim / release
across the bundle, plan parsing from issue bodies. The execution chain
(worktrees, claude_invoke, PR chaining, founder approval gate) lives in
``bin/batman.py`` so it can be skinned per-fleet without touching
testable primitives.

Key contract — bundle = atomic unit:

- ``claim_bundle`` is all-or-nothing: claim every issue in the bundle or
  release every previously-claimed issue and return False. A bundle is
  never half-claimed across repos.
- ``release_bundle`` is best-effort: a per-issue release failure does
  NOT abort the rest. Used on every termination path so even a hard
  crash leaves at most one stuck issue per bundle.

Plan-shape parsing accepts the loose markdown shape Drake produces:

  - ``Affected Repos:`` inline line OR ``## Affected Repos`` H2 (bullets
    OR comma-separated payload)
  - ``Rollout order:`` inline line OR ``## Rollout (order)`` H2
  - ``### <Repo>`` H3 sections under ``## Acceptance Criteria`` for
    per-repo criteria

Anything we cannot parse falls back to a sensible default
(``DEFAULT_ROLLOUT_ORDER``) — Batman flags malformed bodies via the
plan post but never fails the firing on a parse error.

Backport of luminik-io/alfred PRs #115 + #127 + #121 (parser scope-
widening guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_runner import GH_ORG, GH_REPO_TO_LOCAL, claim_issue, gh_json, release_issue

# Label conventions — must match what Drake files and what `gh` searches.
BUNDLE_LABEL_PREFIX = "agent:bundle:"
LARGE_FEATURE_LABEL = "agent:large-feature"

# Default rollout order. Operators with a different stack override via
# ``BATMAN_ROLLOUT_ORDER`` (comma-separated local-repo names). The list
# below matches a typical "backend → frontend → mobile" multi-repo
# product layout; alfred-os ships it as a sane default rather than a
# strict requirement.
DEFAULT_ROLLOUT_ORDER = [
    "backend",
    "frontend",
    "mobile",
    "agents",
    "data-acquisition",
]


# ``https://github.com/<owner>/<repo>/issues/<n>`` — the URL shape
# ``gh search issues --json url`` returns. Capture owner + repo so we
# can route claim / release calls back to the right repo.
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/\d+",
)


def _gh_repo_from_url(url: str) -> str | None:
    """Extract ``<repo>`` from an issue URL, scoped to the configured
    ``GH_ORG``. Returns None for cross-org URLs or malformed input —
    callers route those to the failure branch rather than crashing.
    """
    if not url:
        return None
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        return None
    if GH_ORG and m.group("owner") != GH_ORG:
        return None
    return m.group("repo")


@dataclass
class Bundle:
    """A set of issues sharing the same ``agent:bundle:<slug>`` label,
    or a single issue when no bundle label is present (a "bundle of
    one"). Every operation Batman performs (claim, release, plan
    parsing, execution) treats the bundle as the atomic unit."""

    issues: list[dict]
    bundle_label: str | None

    @property
    def primary_issue(self) -> dict:
        """Oldest issue by ``createdAt`` — the focal point for plan
        post threading and CLI display."""
        return min(self.issues, key=lambda i: i["createdAt"])

    @property
    def slug(self) -> str:
        """Stable bundle id. Bundle-label slug for multi-issue bundles,
        otherwise ``<repo>-<number>`` for solo issues so per-firing
        artifacts (worktree paths, plan files) never collide."""
        if self.bundle_label:
            return self.bundle_label[len(BUNDLE_LABEL_PREFIX) :]
        repo = _gh_repo_from_url(self.primary_issue.get("url", "")) or "unknown"
        return f"{repo}-{self.primary_issue['number']}"


def list_issues_by_bundle_label(bundle_label: str) -> list[dict]:
    """Cross-repo search for every open issue carrying the given
    ``agent:bundle:<slug>`` label.

    Returns ``[]`` on missing GH_ORG, no matches, or any gh search
    failure — never raises.
    """
    if not GH_ORG:
        return []
    rows = gh_json(
        [
            "gh",
            "search",
            "issues",
            "--owner",
            GH_ORG,
            "--label",
            bundle_label,
            "--state",
            "open",
            "--json",
            "number,title,url,labels,createdAt,body",
            "--limit",
            "20",
        ],
        default=[],
    )
    return rows if isinstance(rows, list) else []


def claim_bundle(bundle: Bundle, *, codename: str, firing_id: str) -> bool:
    """Claim every issue in the bundle. All-or-nothing.

    On the first failure (race lost, paused repo, contested claim),
    every previously-claimed issue is released so the bundle never
    sits half-claimed across repos. Returns True only when the entire
    bundle is locked to this firing.
    """
    claimed: list[tuple[str, int]] = []
    for issue in bundle.issues:
        repo = _gh_repo_from_url(issue.get("url", ""))
        if not repo:
            for prev_repo, prev_num in claimed:
                release_issue(
                    prev_repo,
                    prev_num,
                    codename=codename,
                    firing_id=firing_id,
                    outcome="bundle-claim-rolled-back",
                )
            return False
        ok = claim_issue(repo, issue["number"], codename=codename, firing_id=firing_id)
        if not ok:
            for prev_repo, prev_num in claimed:
                release_issue(
                    prev_repo,
                    prev_num,
                    codename=codename,
                    firing_id=firing_id,
                    outcome="bundle-claim-rolled-back",
                )
            return False
        claimed.append((repo, issue["number"]))
    return True


def release_bundle(
    bundle: Bundle,
    *,
    codename: str,
    firing_id: str,
    outcome: str,
    transition_to: str | None = None,
) -> None:
    """Release every issue in the bundle. Best-effort — a per-issue
    failure does not abort the rest. Used on every termination path.

    ``transition_to`` is the lifecycle label every issue moves to (e.g.
    ``agent:pr-open`` on success). Pass ``None`` to leave the label
    untouched.
    """
    import sys

    for issue in bundle.issues:
        repo = _gh_repo_from_url(issue.get("url", ""))
        if repo is None:
            continue
        try:
            release_issue(
                repo,
                issue["number"],
                codename=codename,
                firing_id=firing_id,
                outcome=outcome,
                transition_to=transition_to,
            )
        except Exception as e:
            print(
                f"[BATMAN-RELEASE-WARN] {repo}#{issue['number']}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Plan-shape parsing
# ---------------------------------------------------------------------------


@dataclass
class PlanShape:
    """Parsed plan extracted from an issue body."""

    affected_repos: list[str]  # local repo names, in dependency order
    repo_criteria: dict[str, str]  # local_repo -> acceptance-criteria text


def _normalize_repo_token(token: str) -> str | None:
    """Map a repo mention in an issue body to a local repo name.

    Accepts ``backend``, ``my-org-backend``, ``my-org/my-org-backend``,
    case-insensitive. Returns ``None`` for unknown tokens; callers
    filter those out rather than failing the firing.

    When ``GH_REPO_TO_LOCAL`` is empty (the default for fresh
    alfred-os installs), the token is accepted as-is iff it looks
    like a plausible repo name (``[\\w.-]+``) — operators who haven't
    populated the map shouldn't have plan parsing silently drop every
    repo. They get whatever they typed.
    """
    if not token:
        return None
    token = token.strip().strip(",").lower()
    if "/" in token:
        token = token.split("/", 1)[1]
    if token in GH_REPO_TO_LOCAL:
        return GH_REPO_TO_LOCAL[token]
    if token in GH_REPO_TO_LOCAL.values():
        return token
    if not GH_REPO_TO_LOCAL and re.fullmatch(r"[\w.-]+", token):
        # No mapping configured: trust the author's spelling.
        return token
    return None


def _rollout_order() -> list[str]:
    """Return the configured rollout order.

    Reads ``BATMAN_ROLLOUT_ORDER`` (comma-separated local-repo names)
    and falls back to ``DEFAULT_ROLLOUT_ORDER`` when unset.
    """
    import os

    raw = (os.environ.get("BATMAN_ROLLOUT_ORDER") or "").strip()
    if not raw:
        return list(DEFAULT_ROLLOUT_ORDER)
    return [t.strip() for t in raw.split(",") if t.strip()]


def parse_plan_from_issue(body: str) -> PlanShape:
    """Extract affected repos + per-repo acceptance criteria from an issue.

    Accepts the loose markdown shape Drake produces:

      - ``Affected Repos:`` inline line OR ``## Affected Repos`` H2
        section. H2 accepts EITHER markdown bullets ("- backend\\n-
        frontend") OR a bare comma/whitespace-separated payload
        ("backend, frontend"). The looser form is what humans
        naturally type.
      - ``Rollout order:`` inline line OR ``## Rollout (order)`` H2.
        Splitter excludes ``-`` so hyphenated names like
        ``data-acquisition`` stay intact.
      - ``### <Repo>`` H3 sections under ``## Acceptance Criteria``
        for per-repo criteria.

    Scope-widening guard from luminik-io/alfred PR #121: when an
    explicit ``Affected Repos`` list is present and the criteria block
    contains a stray ``### frontend`` H3 not in the explicit list, the
    explicit list wins — a typo in the criteria section must NOT
    silently expand the PR set.

    Returns ``PlanShape`` with a sensible default rollout when nothing
    parseable is found. Never fails the firing on a malformed body.
    """
    body = body or ""
    affected: list[str] = []
    rollout_override: list[str] = []
    criteria_by_repo: dict[str, str] = {}

    # 1. Inline "Repos:" / "Affected Repos:" / "Rollout order:" lines.
    for line in body.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("repos:") or low.startswith("affected repos:"):
            payload = stripped.split(":", 1)[1]
            for tok in re.split(r"[,\s]+", payload):
                local = _normalize_repo_token(tok)
                if local and local not in affected:
                    affected.append(local)
        elif low.startswith("rollout order:") or low.startswith("rollout:"):
            payload = stripped.split(":", 1)[1]
            # Do NOT include "-" in the splitter character class — repo
            # names like "data-acquisition" contain hyphens, so splitting
            # on "-" silently drops them. The user-typed arrow ("->") is
            # handled by ">" alone; a stray leading "-" from bullet-style
            # input ("- foo") is stripped per-token below.
            for tok in re.split(r"[,\s>→]+", payload):
                tok = tok.strip().lstrip("-").strip()
                if not tok:
                    continue
                local = _normalize_repo_token(tok)
                if local and local not in rollout_override:
                    rollout_override.append(local)

    # 2. ## Affected Repos H2 block.
    affected_h2 = re.search(
        r"^##\s*Affected Repos\s*$(.*?)(?=^##\s|\Z)",
        body,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if affected_h2:
        for line in affected_h2.group(1).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            bullet = re.match(r"^\s*[-*]\s*(.+)$", line)
            payload = bullet.group(1) if bullet else stripped
            for tok in re.split(r"[,\s]+", payload):
                local = _normalize_repo_token(tok)
                if local and local not in affected:
                    affected.append(local)

    # 2b. ## Rollout (Order) H2 block — same shape relaxation as the
    # inline parser. Also accepts the explicit "## Rollout order"
    # header that Drake emits.
    if not rollout_override:
        rollout_h2 = re.search(
            r"^##\s*Rollout(?:\s+order)?\s*$(.*?)(?=^##\s|\Z)",
            body,
            flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if rollout_h2:
            for line in rollout_h2.group(1).splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                bullet = re.match(r"^\s*[-*]\s*(.+)$", line)
                payload = bullet.group(1) if bullet else stripped
                for tok in re.split(r"[,\s>→]+", payload):
                    tok = tok.strip().lstrip("-").strip()
                    if not tok:
                        continue
                    local = _normalize_repo_token(tok)
                    if local and local not in rollout_override:
                        rollout_override.append(local)

    # 3. Per-repo acceptance-criteria H3 sections.
    ac_block = re.search(
        r"^##\s*Acceptance Criteria\s*$(.*?)(?=^##\s|\Z)",
        body,
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if ac_block:
        ac_text = ac_block.group(1)
        for m in re.finditer(
            r"^###\s*([\w\-]+)\s*$(.*?)(?=^###\s|\Z)",
            ac_text,
            flags=re.MULTILINE | re.DOTALL,
        ):
            local = _normalize_repo_token(m.group(1))
            if local:
                criteria_by_repo[local] = m.group(2).strip()

    # 3b. Backfill affected from acceptance-criteria H3s ONLY when the
    # inline "Repos:" line and the H2 list both missed (or are absent).
    # This stops a well-formed criteria-only issue from falling into
    # the default fallback at step 4. IMPORTANT: do not widen scope
    # when an explicit list IS present (PR #121 scope-widening guard).
    if not affected:
        for repo in criteria_by_repo:
            if repo not in affected:
                affected.append(repo)

    # 4. Resolve final order.
    rollout_order = _rollout_order()
    if rollout_override:
        for r in rollout_override:
            if r not in affected:
                affected.append(r)
        ordered = [r for r in rollout_override if r in affected]
    elif affected:
        ordered = [r for r in rollout_order if r in affected]
        ordered += [r for r in affected if r not in ordered]
    else:
        ordered = list(rollout_order[:3])

    return PlanShape(affected_repos=ordered, repo_criteria=criteria_by_repo)


def parse_plan_from_bundle(bundle: Bundle) -> PlanShape:
    """Build the unified ``PlanShape`` from a bundle.

    Two shapes Batman accepts:

    - **Solo bundle** (single issue, body encodes a multi-repo plan):
      delegate to ``parse_plan_from_issue(body)`` so legacy
      single-issue plans still parse correctly.
    - **Multi-issue bundle** (the ``agent:bundle:<slug>`` pattern from
      luminik-io/alfred PR #127): each issue lives in its own product
      repo; that repo IS the issue's affected repo. Per-repo criteria
      come from each issue's body. Rollout order falls back to the
      configured ``BATMAN_ROLLOUT_ORDER`` (default
      ``DEFAULT_ROLLOUT_ORDER``) filtered down to the affected set.
    """
    if len(bundle.issues) <= 1:
        return parse_plan_from_issue(bundle.primary_issue.get("body") or "")

    affected: list[str] = []
    criteria_by_repo: dict[str, str] = {}
    for issue in bundle.issues:
        gh_repo = _gh_repo_from_url(issue.get("url", ""))
        # Map gh repo slug → local repo name. When GH_REPO_TO_LOCAL is
        # empty, treat the gh slug as the local name (fresh installs).
        local = GH_REPO_TO_LOCAL.get(gh_repo or "") or gh_repo
        if not local:
            continue
        if local not in affected:
            affected.append(local)
        body = (issue.get("body") or "").strip()
        # If the issue body itself has a ``### <repo>`` H3 (common when
        # an author templates the full spec into every per-repo issue),
        # prefer that. Otherwise the whole body becomes the criteria —
        # per-repo bundle issues usually contain only their own scope.
        per_repo = parse_plan_from_issue(body)
        criteria_by_repo[local] = per_repo.repo_criteria.get(local) or body

    rollout_order = _rollout_order()
    ordered = [r for r in rollout_order if r in affected]
    ordered += [r for r in affected if r not in ordered]
    return PlanShape(affected_repos=ordered, repo_criteria=criteria_by_repo)


__all__ = [
    "BUNDLE_LABEL_PREFIX",
    "DEFAULT_ROLLOUT_ORDER",
    "LARGE_FEATURE_LABEL",
    "Bundle",
    "PlanShape",
    "claim_bundle",
    "list_issues_by_bundle_label",
    "parse_plan_from_bundle",
    "parse_plan_from_issue",
    "release_bundle",
]
