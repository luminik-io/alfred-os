"""Bundle primitives for Batman, the multi-repo planning coordinator.

Batman picks ``agent:bundle:<slug>`` bundles across product repos,
drafts plans, and exposes claim / release helpers for fleets that add
their own execution layer. This
module is the pure-data part: ``Bundle`` dataclass, claim / release
across the bundle, plan parsing from issue bodies. Public ``bin/batman.py``
is plan-only: it finds a bundle, posts a rollout plan, and stops before
worktrees, PR chaining, merge, or deploy steps. Site-specific fleets can
build those execution steps on top of these primitives.

Key contract, bundle = atomic unit:

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
(``DEFAULT_ROLLOUT_ORDER``), Batman flags malformed bodies via the
plan post but never fails the firing on a parse error.

Includes the parser scope-widening guard used by the bundle planner.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from agent_runner import GH_ORG, GH_REPO_TO_LOCAL, claim_issue, gh_json, release_issue

# Label conventions, must match what Drake files and what `gh` searches.
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


# ``https://github.com/<owner>/<repo>/issues/<n>``, the URL shape
# ``gh search issues --json url`` returns. Capture owner + repo so we
# can route claim / release calls back to the right repo.
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/\d+",
)


def _gh_repo_from_url(url: str) -> str | None:
    """Extract ``<repo>`` from an issue URL, scoped to the configured
    ``GH_ORG``. Returns None for cross-org URLs or malformed input;
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


def _allowed_repo_slugs(tokens: Iterable[str] | None) -> set[str]:
    """Normalize repo allowlist tokens to ``owner/repo`` strings."""
    if not tokens:
        return set()
    allowed: set[str] = set()
    for raw in tokens:
        token = (raw or "").strip()
        if not token:
            continue
        if "/" in token:
            allowed.add(token.lower())
            continue
        repo_slug = next(
            (
                github_repo
                for github_repo, local_repo in GH_REPO_TO_LOCAL.items()
                if token in {github_repo, local_repo}
            ),
            token,
        )
        if GH_ORG:
            allowed.add(f"{GH_ORG}/{repo_slug}".lower())
    return allowed


def _issue_in_allowed_repo(issue: dict, allowed_repos: set[str]) -> bool:
    if not allowed_repos:
        return True
    url = issue.get("url") or ""
    m = _ISSUE_URL_RE.match(url.strip())
    if not m:
        return False
    return f"{m.group('owner')}/{m.group('repo')}".lower() in allowed_repos


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
        """Oldest issue by ``createdAt``, the focal point for plan
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


def list_issues_by_bundle_label(
    bundle_label: str, *, allowed_repos: Iterable[str] | None = None
) -> list[dict]:
    """Cross-repo search for every open issue carrying the given
    ``agent:bundle:<slug>`` label.

    Returns ``[]`` on missing GH_ORG, no matches, or any gh search failure,
    never raises. ``allowed_repos`` keeps bundle siblings inside the same
    configured scan scope as the trigger issue.
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
    if not isinstance(rows, list):
        return []
    allowed = _allowed_repo_slugs(allowed_repos)
    return [row for row in rows if _issue_in_allowed_repo(row, allowed)]


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
    """Release every issue in the bundle. Best-effort, a per-issue
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
    like a plausible repo name (``[\\w.-]+``), operators who haven't
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

    Scope-widening guard: when an
    explicit ``Affected Repos`` list is present and the criteria block
    contains a stray ``### frontend`` H3 not in the explicit list, the
    explicit list wins, a typo in the criteria section must NOT
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
            # Do NOT include "-" in the splitter character class, repo
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

    # 2b. ## Rollout (Order) H2 block, same shape relaxation as the
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
    - **Multi-issue bundle** (the ``agent:bundle:<slug>`` label pattern):
      each issue lives in its own product repo;
      that repo IS the issue's affected repo. Per-repo criteria come
      from each issue's body. Rollout order falls back to the
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
        # prefer that. Otherwise the whole body becomes the criteria;
        # per-repo bundle issues usually contain only their own scope.
        per_repo = parse_plan_from_issue(body)
        criteria_by_repo[local] = per_repo.repo_criteria.get(local) or body

    rollout_order = _rollout_order()
    ordered = [r for r in rollout_order if r in affected]
    ordered += [r for r in affected if r not in ordered]
    return PlanShape(affected_repos=ordered, repo_criteria=criteria_by_repo)


# ---------------------------------------------------------------------------
# plan-approve-execute-report lifecycle
# ---------------------------------------------------------------------------
#
# The block below extends Batman from "draft a plan and stop" to a full
# plan -> approve -> execute -> report cycle. Wire it up via
# ``BatmanLifecycle`` in ``bin/batman.py``; the original parsing /
# claim helpers above stay untouched so legacy fleets keep working.
#
# Design rules (SOLID, DRY, 12-factor):
#
# - Dependency injection: ``BatmanLifecycle`` accepts ``SlackApproval``,
#   ``GitHubChildIssueClient``, and ``Reporter`` via the constructor.
#   Tests inject fakes; production wires the real Slack + gh CLI clients.
# - Label strings come from ``labels.py`` ONLY (no string literals here).
# - Env-driven config via ``BatmanLifecycleConfig.from_env``.
# - Dataclasses for ``BundlePlan``, ``ChildIssue``, ``ApprovalEnvelope``,
#   ``ExecuteResult``, ``ReportEnvelope`` so every shape is JSON-friendly
#   and easy to log / diff.

import contextlib  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Protocol  # noqa: E402

import labels as label_constants  # noqa: E402

logger = logging.getLogger("alfred.batman.lifecycle")

# Outcomes for ExecuteResult.reason. Strings, not enums, so the value
# survives JSON serialisation cleanly.
EXEC_OK = "ok"
EXEC_APPROVAL_TIMEOUT = "approval_timeout"
EXEC_REJECTED = "rejected_by_operator"
EXEC_TRANSPORT = "approval_transport_down"
EXEC_NO_CHILDREN = "no_children_parsed"
EXEC_PARTIAL = "partial"
EXEC_GATE_DISABLED = "gate_disabled"

# Env contract -- documented in docs/BATMAN.md.
ENV_AUTO_EXECUTE = "BATMAN_AUTO_EXECUTE"
ENV_PARENT_REPO = "BATMAN_PARENT_REPO"
ENV_PICKER = "BATMAN_PICKER"
ENV_BUNDLE_SLUG_PREFIX = "BATMAN_BUNDLE_SLUG_PREFIX"
ENV_APPROVAL_TIMEOUT_S = "BATMAN_APPROVAL_TIMEOUT_S"
ENV_SLACK_CHANNEL = "BATMAN_SLACK_CHANNEL"

AUTO_EXECUTE_OFF = "0"
AUTO_EXECUTE_GATE = "approval-gate"
AUTO_EXECUTE_FORCE = "1"
VALID_AUTO_EXECUTE = (AUTO_EXECUTE_OFF, AUTO_EXECUTE_GATE, AUTO_EXECUTE_FORCE)


@dataclass(frozen=True)
class ChildIssue:
    """One scoped sub-issue Batman intends to file in a downstream repo.

    Args:
        repo: ``owner/repo`` slug the issue belongs in.
        title: full issue title (the leading ``<repo>:`` prefix is stripped
            from the parent-body bullet because the issue already lives in
            that repo).
        body: markdown body for the child issue.
        labels: extra labels (lifecycle + bundle) to apply on creation.
    """

    repo: str
    title: str
    body: str
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundlePlan:
    """The result of ``BatmanLifecycle.plan``. Pure data.

    Args:
        bundle_slug: short id derived from the parent issue title
            (``billing-v2`` for "Bundle: billing-v2 rollout"). Used as the
            ``agent:bundle:<slug>`` label so every child shares one trail.
        parent_repo: ``owner/repo`` of the issue Batman is reading.
        parent_issue_number: GitHub issue number on ``parent_repo``.
        parent_title: human-readable parent title (for the Slack post).
        affected_repos: ``owner/repo`` list, declaration order preserved.
        children: per-repo child issues to file on execute.
        done_when: free-text "Done when" block lifted from the body.
        plan_markdown: the rendered markdown a human reads in Slack. The
            execute step does NOT use this; tests pin it for clarity.
    """

    bundle_slug: str
    parent_repo: str
    parent_issue_number: int
    parent_title: str
    affected_repos: tuple[str, ...]
    children: tuple[ChildIssue, ...]
    done_when: str
    plan_markdown: str


@dataclass(frozen=True)
class ApprovalEnvelope:
    """Returned by ``request_approval``; consumed by ``await_approval``.

    Args:
        channel: Slack channel id or name the plan was posted to.
        message_ts: ``chat.postMessage`` ts of the plan message, the
            anchor for the reaction poll.
        plan: the plan that was posted (so ``await_approval`` does not
            need a second argument).
    """

    channel: str
    message_ts: str
    plan: BundlePlan


@dataclass(frozen=True)
class ApprovalResult:
    """A simplified verdict for ``BatmanLifecycle.await_approval``.

    Wraps ``slack_approval.ApprovalResult`` into a Batman-shaped tuple so
    callers do not need to import slack_approval directly.
    """

    approved: bool
    verdict: str
    detail: str = ""
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class ExecuteResult:
    """Outcome of ``BatmanLifecycle.execute``.

    Args:
        executed: True iff at least one child issue was filed.
        reason: machine-readable status (``ok`` / ``approval_timeout`` /
            ``rejected_by_operator`` / ``partial`` / etc.).
        created_issue_urls: URLs of the children that did land.
        failed_repos: ``owner/repo`` of every target that failed to file.
        detail: free-text context for the report.
    """

    executed: bool
    reason: str
    created_issue_urls: tuple[str, ...] = ()
    failed_repos: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class ReportEnvelope:
    """What ``BatmanLifecycle.report`` posts to Slack as a follow-up.

    Kept as a separate dataclass so a fleet that wants a different
    surface (email, PagerDuty, ...) can swap the reporter without
    rewriting the orchestrator.
    """

    bundle_slug: str
    parent_title: str
    created: tuple[str, ...]
    failed_repos: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class BatmanLifecycleConfig:
    """All env-driven knobs for the plan-approve-execute-report flow.

    Build via :meth:`from_env`. Every field is overridable for tests.
    """

    auto_execute: str = AUTO_EXECUTE_OFF
    parent_repo: str = ""
    picker: str = "oldest"
    bundle_slug_prefix: str = ""
    approval_timeout_s: int = 900
    slack_channel: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BatmanLifecycleConfig:
        e = env if env is not None else dict(os.environ)
        raw_auto = (e.get(ENV_AUTO_EXECUTE) or "").strip().lower() or AUTO_EXECUTE_OFF
        if raw_auto not in VALID_AUTO_EXECUTE:
            logger.warning(
                "%s=%r not in %s; treating as off",
                ENV_AUTO_EXECUTE,
                raw_auto,
                VALID_AUTO_EXECUTE,
            )
            raw_auto = AUTO_EXECUTE_OFF
        try:
            timeout = int((e.get(ENV_APPROVAL_TIMEOUT_S) or "900").strip() or "900")
        except ValueError:
            timeout = 900
        return cls(
            auto_execute=raw_auto,
            parent_repo=(e.get(ENV_PARENT_REPO) or "").strip(),
            picker=((e.get(ENV_PICKER) or "oldest").strip() or "oldest"),
            bundle_slug_prefix=(e.get(ENV_BUNDLE_SLUG_PREFIX) or "").strip(),
            approval_timeout_s=max(0, timeout),
            slack_channel=(e.get(ENV_SLACK_CHANNEL) or "").strip(),
        )

    @property
    def gate_enabled(self) -> bool:
        return self.auto_execute == AUTO_EXECUTE_GATE

    @property
    def execute_enabled(self) -> bool:
        return self.auto_execute in (AUTO_EXECUTE_GATE, AUTO_EXECUTE_FORCE)


# ---------------------------------------------------------------------------
# Injection seams.
# ---------------------------------------------------------------------------


class GitHubChildIssueClient(Protocol):
    """Subset of the gh CLI Batman needs to file children.

    Implementations must never raise; return ``None`` on failure so the
    orchestrator can record a partial result and continue.
    """

    def create_issue(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str],
    ) -> str | None:
        """File one issue. Returns the issue URL on success, ``None``
        otherwise. Implementations should ensure labels exist on the
        target repo before adding them, or accept gh's auto-creation
        behaviour.
        """
        ...  # pragma: no cover


class SubprocessGitHubChildIssueClient:
    """Default ``GitHubChildIssueClient`` that shells out to ``gh``.

    Tests inject a fake; production uses this. Errors are logged at
    WARNING and surface as ``None`` returns so callers can record a
    partial-execute result without a crash.
    """

    def __init__(self, gh_bin: str = "gh") -> None:
        self._gh = gh_bin

    def create_issue(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str],
    ) -> str | None:
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            body_path = Path(tmp.name)
        try:
            cmd = [
                self._gh,
                "issue",
                "create",
                "-R",
                repo,
                "--title",
                title,
                "--body-file",
                str(body_path),
            ]
            for label in labels:
                cmd.extend(["--label", label])
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
            if res.returncode != 0:
                logger.warning("gh issue create failed for %s: %s", repo, res.stderr.strip())
                return None
            for line in reversed((res.stdout or "").splitlines()):
                line = line.strip()
                if line.startswith("https://"):
                    return line
            return None
        finally:
            with contextlib.suppress(OSError):
                body_path.unlink()


class ApprovalGate(Protocol):
    """Minimal interface Batman needs from the approval surface.

    ``slack_approval.SlackApproval`` already implements ``await_approval``
    with the right signature, so it satisfies this protocol directly.
    """

    def await_approval(
        self,
        channel: str,
        message_ts: str,
        *,
        timeout_s: int = 900,
        poll_interval_s: int = 30,
    ) -> object: ...  # pragma: no cover


class Reporter(Protocol):
    """Post-execute notifier. ``SlackReporter`` is the default."""

    def post_plan(
        self,
        plan: BundlePlan,
        *,
        channel: str,
    ) -> str | None:
        """Post the plan and return a Slack ts (string), or ``None``."""
        ...  # pragma: no cover

    def post_report(self, envelope: ReportEnvelope, *, channel: str) -> bool:
        """Post the follow-up report. Returns success."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Parent-issue body parsing for the plan-approve-execute lifecycle.
# ---------------------------------------------------------------------------

_BUNDLE_TITLE_RE = re.compile(r"bundle:\s*(?P<slug>[a-z0-9][a-z0-9\-]*)", re.IGNORECASE)
_REPOS_BLOCK_RE = re.compile(
    r"^\s*repos?\s*:\s*$(.*?)(?=^\s*(?:children|done\s*when)\s*:|^\#|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_CHILDREN_BLOCK_RE = re.compile(
    r"^\s*children\s*:\s*$(.*?)(?=^\s*done\s*when\s*:|^\#|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_DONE_BLOCK_RE = re.compile(
    r"^\s*done\s*when\s*:\s*$(.*?)(?=^\#|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def _slugify_bundle_title(title: str, prefix: str = "") -> str:
    """Derive a bundle slug from a parent-issue title.

    Prefers an explicit ``Bundle: <slug>`` lead in the title. Falls back
    to a sanitised slug of the whole title. Lower-snake hyphens, no
    leading/trailing dashes. ``prefix`` is prepended when set (used by
    ``BATMAN_BUNDLE_SLUG_PREFIX``).
    """
    title = (title or "").strip()
    m = _BUNDLE_TITLE_RE.search(title)
    if m:
        slug = m.group("slug").lower().strip("-")
    else:
        s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug = s or "bundle"
    if prefix:
        return f"{prefix.strip('-')}-{slug}"
    return slug


def _parse_repo_lines(block: str) -> list[str]:
    out: list[str] = []
    for line in block.splitlines():
        token = line.strip().lstrip("-*").strip()
        if not token:
            continue
        if "/" not in token:
            # The parent-body shape REQUIRES owner/repo so siblings can be
            # filed cross-repo without operator-side path mapping. Skip
            # malformed lines rather than guess.
            continue
        out.append(token)
    return out


def _parse_children_lines(block: str) -> list[tuple[str, str]]:
    """Return ``[(short_repo, title), ...]`` from a ``Children:`` block.

    Each line is ``- <short_repo>: <title>``. ``short_repo`` is the local
    name (``backend``, ``frontend``) that ``BatmanLifecycle.plan`` maps
    back to a full ``owner/repo`` slug using the affected-repos list.
    """
    out: list[tuple[str, str]] = []
    for line in block.splitlines():
        stripped = line.strip().lstrip("-*").strip()
        if not stripped or ":" not in stripped:
            continue
        repo_token, title = stripped.split(":", 1)
        repo_token = repo_token.strip().lower()
        title = title.strip()
        if not repo_token or not title:
            continue
        out.append((repo_token, title))
    return out


def _resolve_child_repo(short: str, affected: list[str]) -> str | None:
    """Map ``backend`` to ``owner/backend`` using the affected list.

    Trailing-segment match: ``backend`` matches ``my-org/my-backend`` only
    when no exact ``my-org/backend`` is present. Exact match wins.
    """
    short = short.lower()
    exact = [r for r in affected if r.split("/", 1)[-1].lower() == short]
    if exact:
        return exact[0]
    sub = [r for r in affected if r.split("/", 1)[-1].lower().endswith(short)]
    if len(sub) == 1:
        return sub[0]
    return None


def parse_parent_issue(
    *,
    body: str,
    title: str,
    parent_repo: str,
    parent_issue_number: int,
    bundle_slug_prefix: str = "",
) -> BundlePlan:
    """Build a ``BundlePlan`` from a well-formed parent-issue body.

    The body shape is documented in ``docs/BATMAN.md``::

        Bundle: <human title>

        Repos:
        - org/repo-a
        - org/repo-b

        Children:
        - repo-a: short scope
        - repo-b: short scope

        Done when:
        - free-text criteria

    Missing sections degrade gracefully: an empty ``Children`` block
    yields an empty ``children`` tuple, which surfaces as
    ``EXEC_NO_CHILDREN`` at execute time. The parser never raises on
    malformed input.
    """
    body = body or ""
    slug = _slugify_bundle_title(title, prefix=bundle_slug_prefix)

    repos: list[str] = []
    rm = _REPOS_BLOCK_RE.search(body)
    if rm:
        repos = _parse_repo_lines(rm.group(1))

    children_pairs: list[tuple[str, str]] = []
    cm = _CHILDREN_BLOCK_RE.search(body)
    if cm:
        children_pairs = _parse_children_lines(cm.group(1))

    done_when = ""
    dm = _DONE_BLOCK_RE.search(body)
    if dm:
        done_when = dm.group(1).strip()

    bundle_label_str = label_constants.bundle_label(slug)
    base_labels = (label_constants.IMPLEMENT, bundle_label_str)
    children: list[ChildIssue] = []
    for short, child_title in children_pairs:
        full = _resolve_child_repo(short, repos)
        if not full:
            logger.warning("child %r references unknown repo %r; skipping", child_title, short)
            continue
        child_body = _render_child_body(
            parent_repo=parent_repo,
            parent_issue=parent_issue_number,
            parent_title=title,
            bundle_slug=slug,
            child_title=child_title,
            done_when=done_when,
        )
        children.append(
            ChildIssue(
                repo=full,
                title=child_title,
                body=child_body,
                labels=base_labels,
            )
        )

    plan_md = _render_plan_markdown(
        slug=slug,
        parent_repo=parent_repo,
        parent_issue=parent_issue_number,
        parent_title=title,
        affected_repos=repos,
        children=children,
        done_when=done_when,
    )

    return BundlePlan(
        bundle_slug=slug,
        parent_repo=parent_repo,
        parent_issue_number=parent_issue_number,
        parent_title=title,
        affected_repos=tuple(repos),
        children=tuple(children),
        done_when=done_when,
        plan_markdown=plan_md,
    )


def _render_child_body(
    *,
    parent_repo: str,
    parent_issue: int,
    parent_title: str,
    bundle_slug: str,
    child_title: str,
    done_when: str,
) -> str:
    parent_link = f"https://github.com/{parent_repo}/issues/{parent_issue}"
    done_block = f"\n\n## Done when\n\n{done_when}\n" if done_when.strip() else ""
    return (
        f"## Scope\n\n{child_title}\n\n"
        f"## Parent\n\n"
        f"- Bundle: `{bundle_slug}`\n"
        f"- Parent issue: [{parent_repo}#{parent_issue}]({parent_link})\n"
        f"- Parent title: {parent_title}"
        f"{done_block}\n\n"
        f"---\nFiled by Batman as part of the `{bundle_slug}` bundle.\n"
    )


def _render_plan_markdown(
    *,
    slug: str,
    parent_repo: str,
    parent_issue: int,
    parent_title: str,
    affected_repos: list[str],
    children: list[ChildIssue],
    done_when: str,
) -> str:
    """Render the markdown Batman posts to Slack for approval."""
    lines: list[str] = []
    lines.append(f"*Batman plan: `{slug}`*")
    lines.append(
        f"*Parent:* <https://github.com/{parent_repo}/issues/{parent_issue}|"
        f"{parent_repo}#{parent_issue}> -- {parent_title}"
    )
    if affected_repos:
        lines.append("*Affected repos:* " + ", ".join(affected_repos))
    else:
        lines.append("*Affected repos:* (none parsed)")
    lines.append("")
    lines.append("*Children to file:*")
    if children:
        for c in children:
            lines.append(f"  - `{c.repo}` -- {c.title}")
    else:
        lines.append("  - (none parsed; check the parent-issue body shape)")
    if done_when.strip():
        lines.append("")
        lines.append("*Done when:*")
        lines.append(done_when)
    lines.append("")
    lines.append("React with :white_check_mark: to approve, :x: to reject.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Default reporter (Slack-backed). Tests inject a FakeReporter instead.
# ---------------------------------------------------------------------------


class SlackReporter:
    """Default reporter: posts plan + report through ``slack_format``.

    Falls back to the legacy webhook (``slack_post`` from
    ``agent_runner``) when the bot-token surface is unavailable; the
    operator still sees the plan even on a half-configured fleet, but
    without a ``message_ts`` the approval gate is bypassed (callers
    treat that as ``EXEC_GATE_DISABLED``).
    """

    def __init__(
        self,
        *,
        firing_id: str,
        codename: str = "batman",
        thread_root: Callable | None = None,
        fallback_post: Callable | None = None,
    ) -> None:
        self._firing_id = firing_id
        self._codename = codename
        if thread_root is None:
            from slack_format import firing_thread_root as thread_root
        if fallback_post is None:
            try:
                from agent_runner import slack_post as fallback_post
            except Exception:  # pragma: no cover
                fallback_post = None
        # thread_root is guaranteed non-None here (either passed in or
        # filled in by the import above). The assert makes the
        # narrowing visible to mypy so the call sites below do not
        # trip "None not callable".
        assert thread_root is not None
        self._thread_root: Callable = thread_root
        self._fallback_post = fallback_post

    def post_plan(self, plan: BundlePlan, *, channel: str) -> str | None:
        summary = (
            f"plan drafted for {plan.bundle_slug} "
            f"({len(plan.children)} child issue(s), "
            f"{len(plan.affected_repos)} repo(s))"
        )
        handle = self._thread_root(
            codename=self._codename,
            firing_id=self._firing_id,
            summary_one_liner=summary,
            severity="info",
            channel=channel or None,
            body=plan.plan_markdown,
        )
        if handle is None:
            if self._fallback_post is not None:
                self._fallback_post(
                    f"[BATMAN-PLAN-DRAFTED] {summary}\n{plan.plan_markdown}",
                    severity="info",
                )
            return None
        return getattr(handle, "ts", None)

    def post_report(self, envelope: ReportEnvelope, *, channel: str) -> bool:
        lines = [
            f"*Batman bundle `{envelope.bundle_slug}` -- {envelope.reason}*",
            f"*Parent:* {envelope.parent_title}",
        ]
        if envelope.created:
            lines.append("*Filed children:*")
            for url in envelope.created:
                lines.append(f"  - {url}")
        if envelope.failed_repos:
            lines.append("*Failed repos:*")
            for repo in envelope.failed_repos:
                lines.append(f"  - {repo}")
        text = "\n".join(lines)
        summary = f"bundle {envelope.bundle_slug} report ({envelope.reason})"
        handle = self._thread_root(
            codename=self._codename,
            firing_id=f"{self._firing_id}-report",
            summary_one_liner=summary,
            severity="info",
            channel=channel or None,
            body=text,
        )
        if handle is not None:
            return True
        if self._fallback_post is not None:
            self._fallback_post(f"[BATMAN-REPORT] {summary}\n{text}", severity="info")
            return True
        return False


# ---------------------------------------------------------------------------
# Orchestrator.
# ---------------------------------------------------------------------------


@dataclass
class BatmanLifecycle:
    """Orchestrates plan -> approve -> execute -> report for one parent
    issue.

    Dependency-injected (SOLID): swap any of ``gate`` / ``gh_client`` /
    ``reporter`` for tests. ``config`` carries every env-driven knob.
    """

    config: BatmanLifecycleConfig
    gate: ApprovalGate | None = None
    gh_client: GitHubChildIssueClient = field(default_factory=SubprocessGitHubChildIssueClient)
    reporter: Reporter | None = None

    # ---- plan ----

    def plan(
        self,
        *,
        body: str,
        title: str,
        parent_repo: str,
        parent_issue_number: int,
    ) -> BundlePlan:
        """Parse the parent issue and return a :class:`BundlePlan`."""
        return parse_parent_issue(
            body=body,
            title=title,
            parent_repo=parent_repo,
            parent_issue_number=parent_issue_number,
            bundle_slug_prefix=self.config.bundle_slug_prefix,
        )

    # ---- approval ----

    def request_approval(self, plan: BundlePlan) -> ApprovalEnvelope | None:
        """Post the plan to Slack and return an :class:`ApprovalEnvelope`.

        Returns ``None`` when the reporter could not capture a
        ``message_ts`` (no bot token, channel unset, transport down).
        Callers that hit this path should treat the gate as effectively
        disabled and fall back to the operator's
        ``BATMAN_AUTO_EXECUTE`` choice.
        """
        if self.reporter is None:
            return None
        ts = self.reporter.post_plan(plan, channel=self.config.slack_channel)
        if not ts:
            return None
        return ApprovalEnvelope(
            channel=self.config.slack_channel,
            message_ts=ts,
            plan=plan,
        )

    def await_approval(
        self,
        envelope: ApprovalEnvelope,
        *,
        timeout_s: int | None = None,
    ) -> ApprovalResult:
        """Block until the operator approves, rejects, or the wall-clock
        timeout expires. Treats a missing gate as "no approval"."""
        if self.gate is None:
            return ApprovalResult(
                approved=False,
                verdict=EXEC_GATE_DISABLED,
                detail="no SlackApproval injected",
            )
        timeout = timeout_s if timeout_s is not None else self.config.approval_timeout_s
        raw = self.gate.await_approval(
            envelope.channel,
            envelope.message_ts,
            timeout_s=timeout,
        )
        approved = bool(getattr(raw, "approved", False))
        verdict_raw = getattr(raw, "verdict", "unknown")
        from slack_approval import (
            APPROVAL_GRANTED,
            APPROVAL_REJECTED,
            APPROVAL_TIMEOUT,
            APPROVAL_TRANSPORT_DOWN,
        )

        if verdict_raw == APPROVAL_GRANTED:
            verdict = EXEC_OK
        elif verdict_raw == APPROVAL_REJECTED:
            verdict = EXEC_REJECTED
        elif verdict_raw == APPROVAL_TIMEOUT:
            verdict = EXEC_APPROVAL_TIMEOUT
        elif verdict_raw == APPROVAL_TRANSPORT_DOWN:
            verdict = EXEC_TRANSPORT
        else:
            verdict = str(verdict_raw)
        return ApprovalResult(
            approved=approved,
            verdict=verdict,
            detail=str(getattr(raw, "detail", "")),
            elapsed_s=float(getattr(raw, "elapsed_s", 0.0)),
        )

    # ---- execute ----

    def execute(self, plan: BundlePlan) -> ExecuteResult:
        """File every child issue declared in ``plan``.

        Partial failures do not abort: every target is attempted, and the
        outcome is recorded per-repo. Callers report the partial via
        :meth:`report` so the operator can pick up the failed repos
        manually.
        """
        if not plan.children:
            return ExecuteResult(executed=False, reason=EXEC_NO_CHILDREN)
        created: list[str] = []
        failed: list[str] = []
        for child in plan.children:
            url = self.gh_client.create_issue(
                child.repo,
                title=child.title,
                body=child.body,
                labels=list(child.labels),
            )
            if url:
                created.append(url)
                logger.info("filed %s: %s", child.repo, url)
            else:
                failed.append(child.repo)
                logger.warning("failed to file child in %s: %s", child.repo, child.title)
        if not failed:
            return ExecuteResult(
                executed=True,
                reason=EXEC_OK,
                created_issue_urls=tuple(created),
            )
        if not created:
            return ExecuteResult(
                executed=False,
                reason=EXEC_PARTIAL,
                failed_repos=tuple(failed),
                detail="no children landed",
            )
        return ExecuteResult(
            executed=True,
            reason=EXEC_PARTIAL,
            created_issue_urls=tuple(created),
            failed_repos=tuple(failed),
        )

    # ---- report ----

    def report(self, plan: BundlePlan, result: ExecuteResult) -> None:
        """Post the follow-up Slack message naming the filed children."""
        if self.reporter is None:
            return
        envelope = ReportEnvelope(
            bundle_slug=plan.bundle_slug,
            parent_title=plan.parent_title,
            created=result.created_issue_urls,
            failed_repos=result.failed_repos,
            reason=result.reason,
        )
        self.reporter.post_report(envelope, channel=self.config.slack_channel)


__all__ = [
    "AUTO_EXECUTE_FORCE",
    "AUTO_EXECUTE_GATE",
    "AUTO_EXECUTE_OFF",
    "BUNDLE_LABEL_PREFIX",
    "DEFAULT_ROLLOUT_ORDER",
    "ENV_APPROVAL_TIMEOUT_S",
    "ENV_AUTO_EXECUTE",
    "ENV_BUNDLE_SLUG_PREFIX",
    "ENV_PARENT_REPO",
    "ENV_PICKER",
    "ENV_SLACK_CHANNEL",
    "EXEC_APPROVAL_TIMEOUT",
    "EXEC_GATE_DISABLED",
    "EXEC_NO_CHILDREN",
    "EXEC_OK",
    "EXEC_PARTIAL",
    "EXEC_REJECTED",
    "EXEC_TRANSPORT",
    "LARGE_FEATURE_LABEL",
    "ApprovalEnvelope",
    "ApprovalResult",
    "BatmanLifecycle",
    "BatmanLifecycleConfig",
    "Bundle",
    "BundlePlan",
    "ChildIssue",
    "ExecuteResult",
    "GitHubChildIssueClient",
    "PlanShape",
    "ReportEnvelope",
    "Reporter",
    "SlackReporter",
    "SubprocessGitHubChildIssueClient",
    "claim_bundle",
    "list_issues_by_bundle_label",
    "parse_parent_issue",
    "parse_plan_from_bundle",
    "parse_plan_from_issue",
    "release_bundle",
]
