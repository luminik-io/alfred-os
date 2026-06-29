"""Batman lifecycle primitives for the multi-repo architect agent.

Batman reads ``agent:large-feature`` parent issues, drafts plans,
captures approval, files scoped child issues, and reports status. This
module keeps the pure-data pieces testable: bundle labels, parent-plan
parsing, approval envelopes, child issue creation, and report shapes.

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
from dependencies import issue_dependencies

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
    repo_slugs: dict[str, str] = field(default_factory=dict)  # local_repo -> explicit owner/repo
    guessed_default_rollout: bool = False
    parse_notes: tuple[str, ...] = ()

    @property
    def needs_scope_resolution(self) -> bool:
        return self.guessed_default_rollout


DEFAULT_ROLLOUT_WARNING = (
    "No affected repositories or per-repo acceptance criteria were parsed. "
    "Batman would have to guess the first repos in the configured rollout "
    "order, so the plan needs manual scope before execution."
)


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


def _normalize_repo_mention(token: str) -> tuple[str | None, str | None]:
    """Return ``(local_name, explicit_owner_repo)`` for a repo mention."""
    raw = token.strip().strip(",")
    local = _normalize_repo_token(raw)
    if not local:
        return None, None
    if "/" not in raw:
        return local, None
    owner, repo = raw.split("/", 1)
    if not owner or not repo:
        return local, None
    return local, f"{owner.lower()}/{repo.lower()}"


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

    Returns ``PlanShape`` with a diagnostic default rollout when nothing
    parseable is found. Callers must treat ``needs_scope_resolution`` as a
    blocker before posting or executing a plan.
    """
    body = body or ""
    affected: list[str] = []
    explicit_repo_slugs: dict[str, str] = {}
    rollout_override: list[str] = []
    criteria_by_repo: dict[str, str] = {}

    # 1. Inline "Repos:" / "Affected Repos:" / "Rollout order:" lines.
    for line in body.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("repos:") or low.startswith("affected repos:"):
            payload = stripped.split(":", 1)[1]
            for tok in re.split(r"[,\s]+", payload):
                local, explicit_slug = _normalize_repo_mention(tok)
                if local and local not in affected:
                    affected.append(local)
                if local and explicit_slug:
                    explicit_repo_slugs.setdefault(local, explicit_slug)
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
                local, explicit_slug = _normalize_repo_mention(tok)
                if local and local not in rollout_override:
                    rollout_override.append(local)
                if local and explicit_slug:
                    explicit_repo_slugs.setdefault(local, explicit_slug)

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
                local, explicit_slug = _normalize_repo_mention(tok)
                if local and local not in affected:
                    affected.append(local)
                if local and explicit_slug:
                    explicit_repo_slugs.setdefault(local, explicit_slug)

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
                    local, explicit_slug = _normalize_repo_mention(tok)
                    if local and local not in rollout_override:
                        rollout_override.append(local)
                    if local and explicit_slug:
                        explicit_repo_slugs.setdefault(local, explicit_slug)

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
        return PlanShape(
            affected_repos=ordered,
            repo_criteria=criteria_by_repo,
            repo_slugs=explicit_repo_slugs,
            guessed_default_rollout=True,
            parse_notes=(DEFAULT_ROLLOUT_WARNING,),
        )

    return PlanShape(
        affected_repos=ordered,
        repo_criteria=criteria_by_repo,
        repo_slugs=explicit_repo_slugs,
    )


def parse_plan_from_bundle(bundle: Bundle) -> PlanShape:
    """Build the unified ``PlanShape`` from a bundle.

    Two shapes Batman accepts:

    - **Solo bundle** (single issue, body encodes a multi-repo plan):
      delegate to ``parse_plan_from_issue(body)`` so the loose Markdown
      shape still parses correctly.
    - **Multi-issue bundle** (the ``agent:bundle:<slug>`` label pattern):
      each issue lives in its own product repo; that repo IS the issue's
      affected repo. Per-repo criteria come from each issue's body. Preserve
      dependency-sorted issue order only when the bundle declares
      dependencies; otherwise use the configured product rollout order so
      GitHub search result ordering does not make a routine bundle arbitrary.
    """
    if len(bundle.issues) <= 1:
        return parse_plan_from_issue(bundle.primary_issue.get("body") or "")

    has_declared_dependencies = any(issue_dependencies(issue) for issue in bundle.issues)
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

    if has_declared_dependencies:
        ordered = affected
    else:
        rollout_order = _rollout_order()
        ordered = [repo for repo in rollout_order if repo in affected]
        ordered.extend(repo for repo in affected if repo not in ordered)

    return PlanShape(affected_repos=ordered, repo_criteria=criteria_by_repo)


# ---------------------------------------------------------------------------
# plan-approve-execute-report lifecycle
# ---------------------------------------------------------------------------
#
# The block below implements Batman's full plan -> approve -> execute -> report
# cycle. Wire it up via ``BatmanLifecycle`` in ``bin/batman.py``.
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
from typing import Protocol, cast  # noqa: E402

import labels as label_constants  # noqa: E402
from planning_assistant import (  # noqa: E402
    apply_repository_scope_feedback,
    plan_feedback_requires_resolution,
    post_pr_feedback_requires_resolution,
    render_operator_amendments,
    render_operator_feedback_ack,
    render_plan_revision_ack,
    render_post_pr_feedback_ack,
    render_post_pr_followup_block,
)
from server.plan_approvals import (  # noqa: E402
    DECISION_APPROVE,
    DECISION_DECLINE,
)
from server.plan_approvals import (  # noqa: E402
    record_decision as record_plan_decision,
)

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
EXEC_NEEDS_SCOPE = "needs_scope"

# Env contract -- documented in docs/BATMAN.md.
ENV_AUTO_EXECUTE = "BATMAN_AUTO_EXECUTE"
ENV_PARENT_REPO = "BATMAN_PARENT_REPO"
ENV_PICKER = "BATMAN_PICKER"
ENV_BUNDLE_SLUG_PREFIX = "BATMAN_BUNDLE_SLUG_PREFIX"
ENV_APPROVAL_TIMEOUT_S = "BATMAN_APPROVAL_TIMEOUT_S"
ENV_APPROVAL_MODE = "BATMAN_APPROVAL_MODE"
ENV_SLACK_CHANNEL = "BATMAN_SLACK_CHANNEL"
ENV_REPORT_FEEDBACK_TIMEOUT_S = "BATMAN_REPORT_FEEDBACK_TIMEOUT_S"

AUTO_EXECUTE_OFF = "0"
AUTO_EXECUTE_GATE = "approval-gate"
AUTO_EXECUTE_FORCE = "1"
VALID_AUTO_EXECUTE = (AUTO_EXECUTE_OFF, AUTO_EXECUTE_GATE, AUTO_EXECUTE_FORCE)

APPROVAL_MODE_SLACK_OR_FILE = "slack-or-file"
APPROVAL_MODE_SLACK = "slack"
APPROVAL_MODE_FILE = "file"
VALID_APPROVAL_MODES = (
    APPROVAL_MODE_SLACK_OR_FILE,
    APPROVAL_MODE_SLACK,
    APPROVAL_MODE_FILE,
)


def _non_negative_int(
    raw: str | None,
    *,
    default: int,
    override: int | None = None,
) -> int:
    if override is not None:
        return max(0, int(override))
    try:
        return max(0, int((raw or str(default)).strip() or str(default)))
    except (TypeError, ValueError):
        return default


def _feedback_texts(items: Iterable[object]) -> tuple[str, ...]:
    """Extract clean text from Slack feedback objects, dicts, or strings."""

    texts: list[str] = []
    for item in items:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text is None:
            text = str(item)
        cleaned = "\n".join(
            re.sub(r"\s+", " ", line).strip()
            for line in str(text or "").splitlines()
            if line.strip()
        )
        if cleaned:
            texts.append(cleaned)
    return tuple(texts)


def _alfred_runtime_home() -> Path:
    return Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred"))


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return cleaned or "feedback"


def _report_feedback_prompt(timeout_s: int) -> str:
    base = (
        "Reply with `change:`, `fix:`, `test:`, `question:`, or plain language. "
        "Trusted replies become context for the next pass; they never approve, "
        "merge, or change code by themselves."
    )
    if timeout_s <= 0:
        return base
    return f"Reply in the next {_compact_duration(timeout_s)}. {base}"


def _compact_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes}m"
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s"


@dataclass(frozen=True)
class PlanReadinessFinding:
    code: str
    severity: str
    message: str


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
        readiness_findings: warnings or blockers discovered while parsing.
    """

    bundle_slug: str
    parent_repo: str
    parent_issue_number: int
    parent_title: str
    affected_repos: tuple[str, ...]
    children: tuple[ChildIssue, ...]
    done_when: str
    plan_markdown: str
    readiness_findings: tuple[PlanReadinessFinding, ...] = ()

    @property
    def readiness_blockers(self) -> tuple[PlanReadinessFinding, ...]:
        return tuple(f for f in self.readiness_findings if f.severity == "error")


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
    feedback: tuple[str, ...] = ()


def _approval_marker_paths(issue_num: int) -> tuple[Path, Path]:
    base = _alfred_runtime_home() / "batman" / "approvals"
    return base / f"{issue_num}.approved", base / f"{issue_num}.rejected"


def _approval_state_root() -> Path:
    return _alfred_runtime_home() / "state"


def _result_with_elapsed(result: ApprovalResult, elapsed_s: float) -> ApprovalResult:
    return ApprovalResult(
        approved=result.approved,
        verdict=result.verdict,
        detail=result.detail,
        elapsed_s=elapsed_s,
        feedback=result.feedback,
    )


def _record_consumed_file_decision(
    plan: BundlePlan,
    decision: str,
    *,
    detail: str = "",
) -> None:
    try:
        record_plan_decision(
            _approval_state_root(),
            plan.parent_issue_number,
            decision,
            reason=detail,
            source="Batman file approval",
        )
    except OSError as exc:
        logger.warning(
            "could not persist consumed Batman approval for issue %s: %s",
            plan.parent_issue_number,
            exc,
        )


def _consume_file_approval(plan: BundlePlan) -> ApprovalResult | None:
    """Consume the in-app approval marker for a Batman plan, if one exists."""

    approved, rejected = _approval_marker_paths(plan.parent_issue_number)
    if approved.exists():
        _record_consumed_file_decision(plan, DECISION_APPROVE)
        approved.unlink(missing_ok=True)
        rejected.unlink(missing_ok=True)
        return ApprovalResult(
            approved=True,
            verdict=EXEC_OK,
            detail="approved via Alfred client",
        )
    if rejected.exists():
        try:
            detail = rejected.read_text(encoding="utf-8").strip()
        except OSError:
            detail = ""
        _record_consumed_file_decision(plan, DECISION_DECLINE, detail=detail[:300])
        rejected.unlink(missing_ok=True)
        approved.unlink(missing_ok=True)
        return ApprovalResult(
            approved=False,
            verdict=EXEC_REJECTED,
            detail=(detail[:300] or "declined via Alfred client"),
        )
    return None


def wait_for_approval_file(
    plan: BundlePlan,
    *,
    timeout_s: int,
    poll_interval_s: int = 2,
    _now: Callable[[], float] | None = None,
    _sleep: Callable[[float], None] | None = None,
) -> ApprovalResult:
    """Poll Alfred's file-based plan decision marker until timeout."""

    import time

    now = _now or time.monotonic
    sleep = _sleep or time.sleep
    start = now()
    deadline = start + max(0, timeout_s)
    interval = max(0.1, float(poll_interval_s))
    while True:
        result = _consume_file_approval(plan)
        if result is not None:
            return _result_with_elapsed(result, max(0.0, now() - start))
        current = now()
        if current >= deadline:
            return ApprovalResult(
                approved=False,
                verdict=EXEC_APPROVAL_TIMEOUT,
                detail="no file approval marker received",
                elapsed_s=max(0.0, current - start),
            )
        sleep(min(interval, max(0.0, deadline - current)))


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
    approval_timeout_s: int = 86400
    approval_mode: str = APPROVAL_MODE_SLACK_OR_FILE
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
            timeout = int((e.get(ENV_APPROVAL_TIMEOUT_S) or "86400").strip() or "86400")
        except ValueError:
            timeout = 86400
        raw_mode = (
            e.get(ENV_APPROVAL_MODE) or APPROVAL_MODE_SLACK_OR_FILE
        ).strip().lower() or APPROVAL_MODE_SLACK_OR_FILE
        if raw_mode not in VALID_APPROVAL_MODES:
            logger.warning(
                "%s=%r not in %s; treating as %s",
                ENV_APPROVAL_MODE,
                raw_mode,
                VALID_APPROVAL_MODES,
                APPROVAL_MODE_SLACK_OR_FILE,
            )
            raw_mode = APPROVAL_MODE_SLACK_OR_FILE
        return cls(
            auto_execute=raw_auto,
            parent_repo=(e.get(ENV_PARENT_REPO) or "").strip(),
            picker=((e.get(ENV_PICKER) or "oldest").strip() or "oldest"),
            bundle_slug_prefix=(e.get(ENV_BUNDLE_SLUG_PREFIX) or "").strip(),
            approval_timeout_s=max(0, timeout),
            approval_mode=raw_mode,
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

        # Pre-create any per-bundle labels (`agent:bundle:<slug>`) and
        # the operator's ad-hoc labels before `gh issue create`,
        # mirroring what `gh_pr_create` already does for PRs (issue #117).
        # Without this, the first cross-repo execute fails with
        # `could not add label: ... not found` and the operator is left
        # with an approved plan and zero filed children.
        for label in labels:
            self._ensure_label(repo, label)
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

    def _ensure_label(self, repo: str, label: str) -> None:
        """Opportunistically create ``label`` on ``repo``.

        Idempotent: `gh label create` returns non-zero when the label
        already exists, and we swallow that. The colour and description
        are conservative defaults; per-bundle labels use the same
        purple family as `batman-pr-open` so they cluster visually in
        the GitHub label picker (issue #117).
        """
        if label.startswith(BUNDLE_LABEL_PREFIX):
            color = "5319e7"  # matches batman-pr-open
            desc = (
                f"Batman bundle: {label[len(BUNDLE_LABEL_PREFIX) :]}. "
                "Linked children share this label across repos."
            )
        else:
            color = "ededed"
            desc = "Auto-created by Batman child-issue filing on first use"
        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    self._gh,
                    "label",
                    "create",
                    label,
                    "--color",
                    color,
                    "--description",
                    desc,
                    "-R",
                    repo,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )


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
        kill_check: Callable[[], bool] | None = None,
        feedback_callback: Callable | None = None,
    ) -> object: ...  # pragma: no cover


class Reporter(Protocol):
    """Post-execute notifier. ``SlackReporter`` is the default."""

    def post_plan(
        self,
        plan: BundlePlan,
        *,
        channel: str,
    ) -> tuple[str, str] | None:
        """Post the plan; return ``(channel_id, message_ts)`` or ``None``.

        ``channel_id`` is the Slack channel ID (``"C0..."``) returned by
        ``chat.postMessage``, NOT the channel name passed in. Downstream
        ``reactions.get`` calls reject channel names with
        ``channel_not_found`` for private channels and some bot scopes,
        so the envelope must carry the ID Slack just resolved for us.
        """
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
    """Parse a `Repos:` block into a list of ``owner/repo`` slugs.

    Accepts two shapes (issue #116):

    - ``owner/repo`` (canonical): kept verbatim.
    - bare ``repo``: qualified with ``GH_ORG`` when set, so the
      operator's natural shorthand works under the common
      "one-org fleet" setup. Without ``GH_ORG`` the bare line is
      skipped with a stderr warning rather than silently dropped.

    Lines that match neither shape (and can't be qualified) get a
    single warning each; the previous behaviour was to drop them
    silently, which left operators with a confusing ``children=0``
    plan post and no visible cause (the original bug report).
    """
    import sys

    out: list[str] = []
    for line in block.splitlines():
        token = line.strip().lstrip("-*").strip()
        if not token:
            continue
        if "/" in token:
            out.append(token)
            continue
        # Bare repo name. Qualify with GH_ORG when available so the
        # operator's natural shorthand (`palette`, `palette-web`) works
        # in a single-org fleet without forcing them to spell out
        # `owner/` on every line.
        if GH_ORG:
            qualified = f"{GH_ORG}/{token}"
            print(
                f"[BATMAN-PARSE-INFO] _parse_repo_lines: qualified bare repo "
                f"name {token!r} with GH_ORG ({qualified!r}). For multi-org "
                f"fleets, write `owner/repo` explicitly.",
                file=sys.stderr,
            )
            out.append(qualified)
            continue
        # No GH_ORG and no slash: we cannot construct a usable slug.
        # Warn loudly so the operator notices on first firing instead
        # of after a wasted Slack approval cycle.
        print(
            f"[BATMAN-PARSE-WARN] _parse_repo_lines: skipping bare repo "
            f"name {token!r}: no `/` and `GH_ORG` is unset. Write "
            f"`owner/{token}` or set GH_ORG in `~/.alfredrc`. See "
            f"docs/BATMAN_PARENT_ISSUE_TEMPLATE.md.",
            file=sys.stderr,
        )
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

    # Auto-fallback to the loose `## Affected Repos` / `## Acceptance
    # Criteria` shape when the canonical `Repos:` / `Children:` blocks
    # are absent. Operators (and AI assistants) naturally type the loose
    # shape from prose descriptions; without this, the lifecycle path
    # silently returns children=() and the firing dies as EXEC_NO_CHILDREN.
    # See issue #107. Note: parse_plan_from_issue falls back to the
    # default rollout order when nothing parses; that would make every
    # bare body look like a loose-shape match. Gate the fallback on the
    # presence of an explicit `## Affected Repos` / `## Acceptance
    # Criteria` H2 marker in the body so a truly-empty body still hits
    # the EXEC_NO_CHILDREN warning.
    _has_loose_markers = bool(
        re.search(
            r"^##\s*(?:Affected Repos|Acceptance Criteria)\s*$",
            body,
            flags=re.IGNORECASE | re.MULTILINE,
        )
    )
    loose_scope_notes: list[str] = []
    if not repos and not children_pairs and _has_loose_markers:
        loose = parse_plan_from_issue(body)
        if loose.needs_scope_resolution:
            loose_scope_notes.extend(loose.parse_notes)
            logger.warning(
                "parse_parent_issue: loose-shape fallback would require a default "
                "rollout guess; blocking until the parent issue names affected repos."
            )
        elif loose.affected_repos:
            # Map local repo names from the loose parser back to
            # `owner/repo` slugs using GH_REPO_TO_LOCAL.
            local_to_gh = {v: k for k, v in GH_REPO_TO_LOCAL.items()}
            parent_org = parent_repo.split("/", 1)[0] if "/" in parent_repo else ""
            for local in loose.affected_repos:
                # Prefer an explicit GH_REPO_TO_LOCAL mapping, then fall
                # back to <parent_org>/<local> so a fresh fleet with no
                # mapping still produces a usable owner/repo pair. Local
                # name `gh_slug` avoids shadowing the `full` used in the
                # main children-pairs loop below (which would otherwise
                # widen its type to `str | None` and trip mypy).
                gh_slug = (
                    loose.repo_slugs.get(local)
                    or local_to_gh.get(local)
                    or (f"{parent_org}/{local}" if parent_org else local)
                )
                if gh_slug not in repos:
                    repos.append(gh_slug)
            # Synthesize one child per affected repo so the plan post
            # carries real work the operator can approve. The per-repo
            # acceptance-criteria block (if present) becomes the seed
            # context the implementer reads; otherwise the child body
            # falls back to a generic "implement <slug>" stub.
            for local in loose.affected_repos:
                child_title = f"{local}: implement {slug or 'large-feature'}"
                children_pairs.append((local, child_title))
            if not done_when:
                # Reuse the per-repo criteria as a done-when summary so
                # the Slack plan post is not empty under "Done when".
                joined = "\n".join(
                    f"- {repo}: {loose.repo_criteria.get(repo, 'see acceptance criteria')}"
                    for repo in loose.affected_repos
                )
                done_when = joined
            logger.warning(
                "parse_parent_issue: no `Repos:` / `Children:` blocks; "
                "auto-fell-back to `## Affected Repos` / `## Acceptance Criteria` "
                "shape (synthesized %d child(ren) across %d repo(s)). "
                "See docs/BATMAN.md#parent-issue-body-template for the canonical shape.",
                len(children_pairs),
                len(loose.affected_repos),
            )
    elif not repos and not children_pairs:
        logger.warning(
            "parse_parent_issue: no `Repos:` line, no `Children:` block, no "
            "`## Affected Repos` H2, no `## Acceptance Criteria` H3 sections found. "
            "The plan will have children=0 and the bundle will die as "
            "EXEC_NO_CHILDREN. See docs/BATMAN.md#parent-issue-body-template."
        )

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

    if loose_scope_notes:
        readiness_findings = [
            PlanReadinessFinding(
                code="guessed_default_rollout",
                severity="error",
                message=note,
            )
            for note in loose_scope_notes
        ]
    else:
        readiness_findings = _assess_plan_readiness(
            affected_repos=repos,
            children=children,
            done_when=done_when,
        )

    plan_md = _render_plan_markdown(
        slug=slug,
        parent_repo=parent_repo,
        parent_issue=parent_issue_number,
        parent_title=title,
        affected_repos=repos,
        children=children,
        done_when=done_when,
        readiness_findings=readiness_findings,
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
        readiness_findings=tuple(readiness_findings),
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
    readiness_findings: list[PlanReadinessFinding],
) -> str:
    """Render the markdown Batman posts to Slack for approval."""
    blockers = [finding for finding in readiness_findings if finding.severity == "error"]
    warnings = [finding for finding in readiness_findings if finding.severity == "warning"]
    repo_count = len(affected_repos)
    child_count = len(children)
    repo_label = "repo" if repo_count == 1 else "repos"
    child_label = "child issue" if child_count == 1 else "child issues"

    lines: list[str] = []
    lines.append(f"*Alfred plan ready* · `{slug}`")
    lines.append(f"*Parent:* {_issue_link(parent_repo, parent_issue)}")
    lines.append(f"*Work:* {parent_title}")
    if blockers:
        lines.append("*Readiness:* needs scope before implementation")
    else:
        lines.append("*Readiness:* ready for approval")
    lines.append(
        "*Next step:* reply in this thread to steer the plan, or approve only if it is right."
    )
    lines.append(
        "*Replies Alfred understands:* `change:`, `acceptance:`, `test:`, `add repo:`, `remove repo:`, `question:`, `open questions: none`"
    )
    lines.append("*Approval gate:* :white_check_mark: starts this exact scope; :x: stops it.")
    lines.append("")

    lines.append(f"*Scope if approved now:* {repo_count} {repo_label}, {child_count} {child_label}")
    if children:
        for c in children:
            lines.append(f"  - `{c.repo}`: {c.title}")
    elif affected_repos:
        for repo in affected_repos:
            lines.append(f"  - `{repo}`: child scope not parsed yet")
    else:
        lines.append("  - no repository scope parsed yet")

    if done_when.strip():
        lines.append("")
        lines.append("*Done when:*")
        lines.append(done_when)

    if readiness_findings:
        lines.append("")
        lines.append("*Scope checks:*")
        for finding in readiness_findings:
            icon = ":no_entry:" if finding.severity == "error" else ":warning:"
            lines.append(f"  - {icon} `{finding.severity}` {finding.message}")

    lines.append("")
    lines.append("*After approval Alfred will:*")
    lines.append("  1. File the scoped child issues.")
    lines.append("  2. Run each repo in the rollout order.")
    lines.append("  3. Report PR links, failed repos, and merge order in this thread.")

    lines.append("")
    if blockers:
        lines.append(
            "Alfred will not execute while scope blockers remain. Reply with fixes, then approve."
        )
    elif warnings:
        lines.append(
            "Warnings do not block approval, but this is the right moment to tighten the plan."
        )
    else:
        lines.append("No child issues are filed until this plan is approved.")
    return "\n".join(lines)


def _assess_plan_readiness(
    *,
    affected_repos: list[str],
    children: list[ChildIssue],
    done_when: str,
) -> list[PlanReadinessFinding]:
    findings: list[PlanReadinessFinding] = []
    if not affected_repos:
        findings.append(
            PlanReadinessFinding(
                code="missing_repos",
                severity="error",
                message="No affected repositories were parsed.",
            )
        )
    if not children:
        findings.append(
            PlanReadinessFinding(
                code="missing_children",
                severity="error",
                message="No child issues were parsed from the parent body.",
            )
        )
    if children:
        for child in children:
            plain_title = re.sub(r"\s+", " ", child.title).strip()
            if len(plain_title) < 5 or re.search(
                r"\b(?:todo|tbd|placeholder)\b", plain_title, re.I
            ):
                findings.append(
                    PlanReadinessFinding(
                        code="vague_child_scope",
                        severity="error",
                        message=f"`{child.repo}` child scope is too vague: {plain_title or '(empty)'}",
                    )
                )
            elif re.search(
                r"\b(?:better|etc|improve|maybe|nice|stuff|things)\b", plain_title, re.I
            ):
                findings.append(
                    PlanReadinessFinding(
                        code="soft_child_scope",
                        severity="warning",
                        message=f"`{child.repo}` child scope may need a sharper outcome: {plain_title}",
                    )
                )
    if not done_when.strip():
        findings.append(
            PlanReadinessFinding(
                code="missing_done_when",
                severity="error",
                message="Add a Done when block before Alfred can execute this bundle.",
            )
        )
    elif re.search(r"\b(?:todo|tbd|placeholder)\b", done_when, re.I):
        findings.append(
            PlanReadinessFinding(
                code="vague_done_when",
                severity="error",
                message="Done when still contains placeholder text.",
            )
        )
    return findings


def _issue_link(repo: str, number: int) -> str:
    try:
        from slack_format import github_issue_link

        return github_issue_link(repo, number)
    except Exception:
        return f"<https://github.com/{repo}/issues/{number}|{repo}#{number}>"


def _slack_url_link(url: str, *, label: str | None = None) -> str:
    try:
        from slack_format import github_url_link

        return github_url_link(url, label=label)
    except Exception:
        clean = str(url or "").strip()
        if not clean:
            return ""
        return f"<{clean}|{label}>" if label else clean


def _approval_feedback(raw: object) -> tuple[str, ...]:
    """Extract operator thread replies from a Slack approval result."""
    return _feedback_texts(getattr(raw, "feedback", ()) or ())


def _append_operator_feedback(body: str, feedback: Iterable[str]) -> str:
    """Append approved Slack-thread amendments to a child issue body."""
    amendment_block = render_operator_amendments(feedback)
    if not amendment_block:
        return body
    return f"{body.rstrip()}\n\n{amendment_block}"


def _apply_operator_feedback_to_plan(
    plan: BundlePlan,
    feedback: Iterable[str],
) -> BundlePlan:
    """Apply Slack repo-scope amendments before filing child issues."""

    default_org = plan.parent_repo.split("/", 1)[0] if "/" in plan.parent_repo else None
    affected_repos = apply_repository_scope_feedback(
        plan.affected_repos,
        feedback,
        default_org=default_org,
    )
    if affected_repos == plan.affected_repos:
        return plan

    affected_lookup = {repo.lower() for repo in affected_repos}
    children: list[ChildIssue] = [
        child for child in plan.children if child.repo.lower() in affected_lookup
    ]
    existing_children = {child.repo.lower() for child in children}
    base_labels = (
        plan.children[0].labels
        if plan.children
        else (
            label_constants.IMPLEMENT,
            label_constants.bundle_label(plan.bundle_slug),
        )
    )
    for repo in affected_repos:
        if repo.lower() in existing_children:
            continue
        short_repo = repo.rsplit("/", 1)[-1]
        child_title = f"{short_repo}: implement {plan.bundle_slug}"
        children.append(
            ChildIssue(
                repo=repo,
                title=child_title,
                body=_render_child_body(
                    parent_repo=plan.parent_repo,
                    parent_issue=plan.parent_issue_number,
                    parent_title=plan.parent_title,
                    bundle_slug=plan.bundle_slug,
                    child_title=child_title,
                    done_when=plan.done_when,
                ),
                labels=base_labels,
            )
        )

    readiness_findings = _assess_plan_readiness(
        affected_repos=list(affected_repos),
        children=children,
        done_when=plan.done_when,
    )
    return BundlePlan(
        bundle_slug=plan.bundle_slug,
        parent_repo=plan.parent_repo,
        parent_issue_number=plan.parent_issue_number,
        parent_title=plan.parent_title,
        affected_repos=affected_repos,
        children=tuple(children),
        done_when=plan.done_when,
        plan_markdown=_render_plan_markdown(
            slug=plan.bundle_slug,
            parent_repo=plan.parent_repo,
            parent_issue=plan.parent_issue_number,
            parent_title=plan.parent_title,
            affected_repos=list(affected_repos),
            children=children,
            done_when=plan.done_when,
            readiness_findings=readiness_findings,
        ),
        readiness_findings=tuple(readiness_findings),
    )


def _children_by_repo(plan: BundlePlan) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in plan.children:
        counts[child.repo] = counts.get(child.repo, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Default reporter (Slack-backed). Tests inject a FakeReporter instead.
# ---------------------------------------------------------------------------


class SlackReporter:
    """Default reporter: posts plan + report through ``slack_format``.

    Falls back to the webhook surface (``slack_post`` from
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
        report_feedback_timeout_s: int | None = None,
        feedback_reader: Callable[[str, str], Iterable[object]] | None = None,
        feedback_reply: Callable | None = None,
        followup_dir: Path | None = None,
        sleeper: Callable[[float], None] | None = None,
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
        self._report_feedback_timeout_s = _non_negative_int(
            os.environ.get(ENV_REPORT_FEEDBACK_TIMEOUT_S),
            default=60,
            override=report_feedback_timeout_s,
        )
        self._feedback_reader = feedback_reader
        self._feedback_reply = feedback_reply
        self._followup_dir = followup_dir
        self._sleeper = sleeper

    def post_plan(self, plan: BundlePlan, *, channel: str) -> tuple[str, str] | None:
        summary = (
            f"plan drafted for {plan.bundle_slug} "
            f"({len(plan.children)} child issue(s), "
            f"{len(plan.affected_repos)} repo(s))"
        )
        plan_path = self._write_plan_copy(plan)
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
        # Slack's chat.postMessage echoes back the resolved channel ID;
        # propagate that to ApprovalEnvelope so reactions.get gets the
        # ID it needs (channel names fail with channel_not_found on
        # private channels and some bot scope sets).
        ch_id = getattr(handle, "channel", None)
        ts = getattr(handle, "ts", None)
        if not ch_id or not ts:
            return None
        self._register_plan_thread(plan, channel=str(ch_id), ts=str(ts), plan_path=plan_path)
        return (ch_id, ts)

    def post_plan_feedback(
        self,
        *,
        channel: str,
        message_ts: str,
        feedback: Iterable[str],
        plan: BundlePlan | None = None,
        all_feedback: Iterable[str] = (),
        revised_repos: Iterable[str] = (),
    ) -> bool:
        """Acknowledge operator planning feedback in the original plan thread."""
        if plan is not None:
            effective_repos = tuple(revised_repos) or plan.affected_repos
            text = render_plan_revision_ack(
                all_feedback or feedback,
                revised_repos=effective_repos,
                child_count=len(plan.children),
            )
        else:
            text = render_operator_feedback_ack(feedback)
        if not text:
            return False
        try:
            from slack_format import ThreadHandle, firing_thread_reply
        except Exception:  # pragma: no cover
            return False
        return bool(
            firing_thread_reply(
                ThreadHandle(channel=channel, ts=message_ts),
                text=text,
                severity="info",
            )
        )

    def post_report(self, envelope: ReportEnvelope, *, channel: str) -> bool:
        lines = [
            f"*Alfred report* · `{envelope.bundle_slug}`",
            f"*Outcome:* `{envelope.reason}`",
            f"*Work:* {envelope.parent_title}",
        ]
        if envelope.created:
            lines.append("*Created for implementation:*")
            for index, url in enumerate(envelope.created, start=1):
                lines.append(f"  - {_slack_url_link(url, label=f'child {index}')}")
        if envelope.failed_repos:
            lines.append("*Failed repos:*")
            for repo in envelope.failed_repos:
                lines.append(f"  - {repo}")
        lines.extend(
            [
                "",
                "*Need a tweak?*",
                _report_feedback_prompt(self._report_feedback_timeout_s),
            ]
        )
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
            self._register_report_thread(envelope, handle)
            self._capture_report_feedback(handle, envelope)
            return True
        if self._fallback_post is not None:
            self._fallback_post(f"[BATMAN-REPORT] {summary}\n{text}", severity="info")
            return True
        return False

    def _register_plan_thread(
        self,
        plan: BundlePlan,
        *,
        channel: str,
        ts: str,
        plan_path: Path | None = None,
    ) -> None:
        try:
            from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry
        except Exception:  # pragma: no cover - optional local state helper
            return
        plan_path = plan_path or self._write_plan_copy(plan)
        try:
            SlackThreadRegistry().register(
                SlackThreadRecord(
                    kind="plan",
                    channel=channel,
                    thread_ts=ts,
                    codename=self._codename,
                    firing_id=self._firing_id,
                    title=plan.parent_title,
                    status="awaiting_approval",
                    parent_repo=plan.parent_repo,
                    parent_issue=plan.parent_issue_number,
                    plan_path=str(plan_path) if plan_path else "",
                    metadata={
                        "bundle_slug": plan.bundle_slug,
                        "affected_repos": list(plan.affected_repos),
                        "child_count": len(plan.children),
                        "children_by_repo": _children_by_repo(plan),
                    },
                )
            )
        except Exception as exc:  # pragma: no cover - local state best effort
            logger.debug("could not register Slack plan thread: %s", exc)

    def _register_report_thread(self, envelope: ReportEnvelope, handle: object) -> None:
        try:
            from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry
        except Exception:  # pragma: no cover - optional local state helper
            return
        channel = str(getattr(handle, "channel", "") or "")
        ts = str(getattr(handle, "ts", "") or "")
        if not channel or not ts:
            return
        try:
            SlackThreadRegistry().register(
                SlackThreadRecord(
                    kind="report",
                    channel=channel,
                    thread_ts=ts,
                    codename=self._codename,
                    firing_id=f"{self._firing_id}-report",
                    title=envelope.parent_title,
                    status=envelope.reason,
                    metadata={
                        "bundle_slug": envelope.bundle_slug,
                        "created": list(envelope.created),
                        "failed_repos": list(envelope.failed_repos),
                    },
                )
            )
        except Exception as exc:  # pragma: no cover - local state best effort
            logger.debug("could not register Slack report thread: %s", exc)

    def _write_plan_copy(self, plan: BundlePlan) -> Path | None:
        try:
            root = _alfred_runtime_home() / "batman-plans"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{plan.parent_issue_number}-plan.md"
            path.write_text(plan.plan_markdown, encoding="utf-8")
            return path
        except OSError as exc:
            logger.debug("could not write Batman plan copy: %s", exc)
            return None

    def _capture_report_feedback(self, handle: object, envelope: ReportEnvelope) -> None:
        """Best-effort capture of trusted Slack replies after a report post."""

        if self._report_feedback_timeout_s > 0:
            if self._sleeper is not None:
                self._sleeper(float(self._report_feedback_timeout_s))
            else:
                import time

                time.sleep(self._report_feedback_timeout_s)
        channel = str(getattr(handle, "channel", "") or "")
        ts = str(getattr(handle, "ts", "") or "")
        if not channel or not ts:
            return
        feedback = self._read_report_feedback(channel, ts)
        if not feedback:
            return
        ack = render_post_pr_feedback_ack(
            feedback,
            pr_urls=envelope.created,
        )
        if ack:
            severity = "warn" if post_pr_feedback_requires_resolution(feedback) else "info"
            self._post_report_feedback_ack(handle, ack, severity=severity)
        self._write_report_followup(envelope, feedback)

    def _read_report_feedback(self, channel: str, ts: str) -> tuple[str, ...]:
        reader = self._feedback_reader
        if reader is not None:
            return _feedback_texts(reader(channel, ts))
        try:
            from slack_approval import (
                collect_trusted_thread_feedback,
                default_slack_client,
                operator_user_id_from_env,
                trusted_feedback_user_ids_from_env,
            )
        except Exception:  # pragma: no cover - optional Slack surface
            return ()
        operator_id = operator_user_id_from_env()
        feedback_user_ids = trusted_feedback_user_ids_from_env(operator_id)
        if not feedback_user_ids:
            return ()
        try:
            client = default_slack_client()
        except Exception as exc:  # pragma: no cover - env-dependent
            logger.debug("cannot build Slack client for report feedback: %s", exc)
            return ()
        feedback = collect_trusted_thread_feedback(
            client,
            channel=channel,
            message_ts=ts,
            feedback_user_ids=feedback_user_ids,
            purpose="report feedback",
        )
        return _feedback_texts(feedback)

    def _post_report_feedback_ack(
        self,
        handle: object,
        text: str,
        *,
        severity: str,
    ) -> bool:
        reply: Callable[..., object] | None = self._feedback_reply
        if reply is None:
            try:
                from slack_format import firing_thread_reply as imported_reply
            except Exception:  # pragma: no cover - optional Slack surface
                return False
            reply = cast(Callable[..., object], imported_reply)
        return bool(reply(handle, text=text, severity=severity))

    def _write_report_followup(
        self,
        envelope: ReportEnvelope,
        feedback: Iterable[str],
    ) -> Path | None:
        block = render_post_pr_followup_block(
            feedback,
            pr_urls=envelope.created,
        )
        if not block:
            return None
        root = self._followup_dir or _alfred_runtime_home() / "state" / "followups"
        try:
            root.mkdir(parents=True, exist_ok=True)
            path = root / (
                f"{_safe_filename(self._firing_id)}-{_safe_filename(envelope.bundle_slug)}.md"
            )
            header = [
                f"# Follow-up for {envelope.parent_title or envelope.bundle_slug}",
                "",
                f"- Bundle: `{envelope.bundle_slug}`",
            ]
            if envelope.created:
                header.append(f"- Created: {', '.join(envelope.created)}")
            if envelope.failed_repos:
                header.append(f"- Failed repos: {', '.join(envelope.failed_repos)}")
            path.write_text("\n".join(header).rstrip() + "\n\n" + block, encoding="utf-8")
            return path
        except OSError as exc:
            logger.warning("could not write Batman follow-up feedback: %s", exc)
            return None


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
    operator_feedback: tuple[str, ...] = field(default=(), init=False, repr=False)

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
        result = self.reporter.post_plan(plan, channel=self.config.slack_channel)
        if not result:
            return None
        channel_id, ts = result
        return ApprovalEnvelope(
            channel=channel_id,
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
        timeout = timeout_s if timeout_s is not None else self.config.approval_timeout_s
        file_enabled = self.config.approval_mode in (
            APPROVAL_MODE_SLACK_OR_FILE,
            APPROVAL_MODE_FILE,
        )
        file_result = _consume_file_approval(envelope.plan) if file_enabled else None
        if file_result is not None:
            self.operator_feedback = file_result.feedback
            return file_result
        if self.config.approval_mode == APPROVAL_MODE_FILE:
            file_result = wait_for_approval_file(envelope.plan, timeout_s=timeout)
            self.operator_feedback = file_result.feedback
            return file_result
        if self.gate is None:
            return ApprovalResult(
                approved=False,
                verdict=EXEC_GATE_DISABLED,
                detail="no SlackApproval injected",
            )

        def file_kill_check() -> bool:
            nonlocal file_result
            if not file_enabled:
                return False
            file_result = _consume_file_approval(envelope.plan)
            return file_result is not None

        feedback_callback: Callable[[tuple[object, ...]], None] | None = None
        accumulated_feedback: list[str] = []
        post_plan_feedback = getattr(self.reporter, "post_plan_feedback", None)
        if callable(post_plan_feedback):

            def feedback_callback(raw_feedback: tuple[object, ...]) -> None:
                feedback = _feedback_texts(raw_feedback)
                if feedback:
                    accumulated_feedback.extend(feedback)
                    default_org = (
                        envelope.plan.parent_repo.split("/", 1)[0]
                        if "/" in envelope.plan.parent_repo
                        else None
                    )
                    revised_repos = apply_repository_scope_feedback(
                        envelope.plan.affected_repos,
                        accumulated_feedback,
                        default_org=default_org,
                    )
                    revised_plan = _apply_operator_feedback_to_plan(
                        envelope.plan,
                        accumulated_feedback,
                    )
                    post_plan_feedback(
                        channel=envelope.channel,
                        message_ts=envelope.message_ts,
                        feedback=feedback,
                        plan=revised_plan,
                        all_feedback=tuple(accumulated_feedback),
                        revised_repos=revised_repos,
                    )

        raw = self.gate.await_approval(
            envelope.channel,
            envelope.message_ts,
            timeout_s=timeout,
            poll_interval_s=5 if file_enabled else 30,
            kill_check=file_kill_check if file_enabled else None,
            feedback_callback=feedback_callback,
        )
        if file_result is not None:
            file_result = _result_with_elapsed(
                file_result,
                float(getattr(raw, "elapsed_s", file_result.elapsed_s)),
            )
            self.operator_feedback = file_result.feedback
            return file_result
        approved = bool(getattr(raw, "approved", False))
        verdict_raw = getattr(raw, "verdict", "unknown")
        feedback = _approval_feedback(raw)
        self.operator_feedback = feedback
        from slack_approval import (
            APPROVAL_GRANTED,
            APPROVAL_REJECTED,
            APPROVAL_TIMEOUT,
            APPROVAL_TRANSPORT_DOWN,
        )

        if verdict_raw == APPROVAL_GRANTED:
            if plan_feedback_requires_resolution(feedback):
                approved = False
                verdict = EXEC_NEEDS_SCOPE
                detail = "Slack feedback contains open questions; resolve them before approval."
            else:
                verdict = EXEC_OK
                detail = str(getattr(raw, "detail", ""))
        elif verdict_raw == APPROVAL_REJECTED:
            verdict = EXEC_REJECTED
            detail = str(getattr(raw, "detail", ""))
        elif verdict_raw == APPROVAL_TIMEOUT:
            verdict = EXEC_APPROVAL_TIMEOUT
            detail = str(getattr(raw, "detail", ""))
        elif verdict_raw == APPROVAL_TRANSPORT_DOWN:
            verdict = EXEC_TRANSPORT
            detail = str(getattr(raw, "detail", ""))
        else:
            verdict = str(verdict_raw)
            detail = str(getattr(raw, "detail", ""))
        return ApprovalResult(
            approved=approved,
            verdict=verdict,
            detail=detail,
            elapsed_s=float(getattr(raw, "elapsed_s", 0.0)),
            feedback=feedback,
        )

    # ---- execute ----

    def execute(self, plan: BundlePlan) -> ExecuteResult:
        """File every child issue declared in ``plan``.

        Partial failures do not abort: every target is attempted, and the
        outcome is recorded per-repo. Callers report the partial via
        :meth:`report` so the operator can pick up the failed repos
        manually.
        """
        if plan_feedback_requires_resolution(self.operator_feedback):
            return ExecuteResult(
                executed=False,
                reason=EXEC_NEEDS_SCOPE,
                detail="Slack feedback contains open questions.",
            )
        plan = _apply_operator_feedback_to_plan(plan, self.operator_feedback)
        if not plan.children:
            return ExecuteResult(executed=False, reason=EXEC_NO_CHILDREN)
        if plan.readiness_blockers:
            detail = "; ".join(f.message for f in plan.readiness_blockers)
            return ExecuteResult(executed=False, reason=EXEC_NEEDS_SCOPE, detail=detail)
        created: list[str] = []
        failed: list[str] = []
        for child in plan.children:
            url = self.gh_client.create_issue(
                child.repo,
                title=child.title,
                body=_append_operator_feedback(child.body, self.operator_feedback),
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
    "APPROVAL_MODE_FILE",
    "APPROVAL_MODE_SLACK",
    "APPROVAL_MODE_SLACK_OR_FILE",
    "AUTO_EXECUTE_FORCE",
    "AUTO_EXECUTE_GATE",
    "AUTO_EXECUTE_OFF",
    "BUNDLE_LABEL_PREFIX",
    "DEFAULT_ROLLOUT_ORDER",
    "ENV_APPROVAL_MODE",
    "ENV_APPROVAL_TIMEOUT_S",
    "ENV_AUTO_EXECUTE",
    "ENV_BUNDLE_SLUG_PREFIX",
    "ENV_PARENT_REPO",
    "ENV_PICKER",
    "ENV_REPORT_FEEDBACK_TIMEOUT_S",
    "ENV_SLACK_CHANNEL",
    "EXEC_APPROVAL_TIMEOUT",
    "EXEC_GATE_DISABLED",
    "EXEC_NEEDS_SCOPE",
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
    "PlanReadinessFinding",
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
    "wait_for_approval_file",
]
