"""Sequential N-PR chain coordinator for multi-repo features.

When a single feature ships across two or more repos (e.g. an API
contract in ``your-backend`` matched by a UI change in ``your-frontend``),
all PRs need to land together and reference each other. This module
provides the primitives for that workflow:

- :class:`RepoTarget` describes one PR to open (repo, branch, title, body,
  acceptance criteria).
- :class:`CrossRepoPRChain` is a plan-and-execute coordinator:

    1. ``plan(targets) -> Plan`` builds a declarative description of the
       N PRs to open, including the cross-link body template and the
       state file path. Pure, no I/O. (12-factor: the plan is data.)
    2. ``execute(plan) -> Result`` carries the plan out: for each repo,
       open the PR, save the URL to the persistent state file, rebuild
       the body of any previously-opened PRs in the chain so they link
       forward to the new sibling.

- All GitHub interaction goes through the :class:`GitHubClient` Protocol.
  The default implementation shells out to ``gh`` via subprocess; tests
  inject a fake.
- CI-readiness polling is exposed as a separate helper
  (:func:`wait_for_ci_green`) so callers that want it can call it
  between PR opens; the chain itself does not block on CI by default.

State is persisted to ``$ALFRED_HOME/state/pr-chains/<feature_id>.json``
using atomic write-and-rename so a crash mid-write never leaves a
half-written JSON blob.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from labels import AUTHORED, PR_OPEN

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Public dataclasses.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoTarget:
    """One repo's worth of input to the chain.

    Args:
        repo_name: logical name (used as the key in the state file).
        gh_repo: GitHub repo slug (``org/repo``) to open the PR against.
        branch: head branch the PR opens from.
        title: PR title.
        acceptance_criteria: per-repo acceptance criteria appended to
            the chain-body template. Plain text (markdown OK).
        extra_labels: optional extra labels to apply on PR open. The
            framework always applies :data:`labels.PR_OPEN` and
            :data:`labels.AUTHORED`.
    """

    repo_name: str
    gh_repo: str
    branch: str
    title: str
    acceptance_criteria: str = ""
    extra_labels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Plan:
    """Declarative description of a cross-repo PR chain.

    Built by :meth:`CrossRepoPRChain.plan`; consumed by
    :meth:`CrossRepoPRChain.execute`. Plain data; safe to serialise or
    log for review before execution (12-factor: declarative plan,
    executor consumes).
    """

    feature_id: str
    feature_title: str
    parent_repo: str
    parent_issue: int
    parent_gh_org: str
    targets: tuple[RepoTarget, ...]
    state_file: Path
    approved_by: str = "operator"
    base_labels: tuple[str, ...] = (PR_OPEN, AUTHORED)

    def total(self) -> int:
        """Total number of PRs in the chain."""
        return len(self.targets)


@dataclass
class Result:
    """Outcome of executing a plan."""

    feature_id: str
    opened: dict[str, str] = field(default_factory=dict)  # repo_name -> PR URL
    failed: list[str] = field(default_factory=list)  # repo_names that failed

    @property
    def ok(self) -> bool:
        return not self.failed


# Persisted-state file shape — kept separate from Plan because the file
# survives between executions (resume after partial chain failure).
@dataclass
class ChainState:
    feature_id: str
    parent_repo: str
    parent_issue: int
    repos: list[str]
    prs: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0


# --------------------------------------------------------------------------
# Injection seams.
# --------------------------------------------------------------------------


class GitHubClient(Protocol):
    """Protocol the chain uses to talk to GitHub."""

    def pr_create(
        self,
        gh_repo: str,
        *,
        title: str,
        body: str,
        head: str,
        labels: Sequence[str],
    ) -> str | None:
        """Open a PR and return its URL, or ``None`` on failure.

        Implementations should ensure required labels exist on the repo
        before adding them.
        """
        ...  # pragma: no cover

    def pr_edit_body(
        self, gh_repo: str, pr_url: str, *, body: str
    ) -> bool:
        """Replace the PR body of an existing PR. Returns True on success."""
        ...  # pragma: no cover

    def pr_status_checks(self, gh_repo: str, pr_url: str) -> list[dict]:
        """Return the ``statusCheckRollup`` payload for a PR."""
        ...  # pragma: no cover


class SubprocessGitHubClient:
    """Default :class:`GitHubClient` implementation; shells out to ``gh``."""

    def __init__(self, gh_bin: str = "gh") -> None:
        self._gh = gh_bin

    def _run(self, args: Sequence[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._gh, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def pr_create(
        self,
        gh_repo: str,
        *,
        title: str,
        body: str,
        head: str,
        labels: Sequence[str],
    ) -> str | None:
        # Use a tempfile for the body to avoid argv length limits and
        # quoting issues; gh requires --body-file for anything non-trivial.
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            body_path = Path(tmp.name)
        try:
            cmd = [
                "pr",
                "create",
                "-R",
                gh_repo,
                "--title",
                title,
                "--body-file",
                str(body_path),
                "--head",
                head,
            ]
            for label in labels:
                cmd.extend(["--label", label])
            res = self._run(cmd, timeout=60)
            if res.returncode != 0:
                logger.warning(
                    "gh pr create failed for %s: %s", gh_repo, res.stderr.strip()
                )
                return None
            for line in reversed((res.stdout or "").splitlines()):
                line = line.strip()
                if line.startswith("https://"):
                    return line
            return None
        finally:
            with contextlib.suppress(OSError):
                body_path.unlink()

    def pr_edit_body(self, gh_repo: str, pr_url: str, *, body: str) -> bool:
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(body)
            body_path = Path(tmp.name)
        try:
            res = self._run(
                [
                    "pr",
                    "edit",
                    pr_url,
                    "-R",
                    gh_repo,
                    "--body-file",
                    str(body_path),
                ],
                timeout=30,
            )
            if res.returncode != 0:
                logger.warning(
                    "gh pr edit body failed for %s: %s",
                    pr_url,
                    res.stderr.strip(),
                )
                return False
            return True
        finally:
            with contextlib.suppress(OSError):
                body_path.unlink()

    def pr_status_checks(self, gh_repo: str, pr_url: str) -> list[dict]:
        res = self._run(
            [
                "pr",
                "view",
                pr_url,
                "-R",
                gh_repo,
                "--json",
                "statusCheckRollup",
            ],
            timeout=15,
        )
        if res.returncode != 0:
            return []
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        checks = data.get("statusCheckRollup") or []
        return checks if isinstance(checks, list) else []


# --------------------------------------------------------------------------
# State persistence (atomic write-and-rename).
# --------------------------------------------------------------------------


def _default_state_dir() -> Path:
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base) / "state" / "pr-chains"


def state_file_path(feature_id: str, *, state_dir: Path | None = None) -> Path:
    """Return the state-file path for a feature_id."""
    root = state_dir or _default_state_dir()
    return root / f"{feature_id}.json"


def save_chain_state(state: ChainState, path: Path) -> None:
    """Persist chain state atomically (write to ``.tmp`` then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "feature_id": state.feature_id,
        "parent_repo": state.parent_repo,
        "parent_issue": state.parent_issue,
        "repos": list(state.repos),
        "prs": dict(state.prs),
        "created_at": state.created_at,
    }
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_chain_state(path: Path) -> ChainState | None:
    """Load chain state from disk, or ``None`` if missing/invalid."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return ChainState(
            feature_id=data["feature_id"],
            parent_repo=data["parent_repo"],
            parent_issue=data["parent_issue"],
            repos=list(data["repos"]),
            prs=dict(data.get("prs", {})),
            created_at=float(data.get("created_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Body template.
# --------------------------------------------------------------------------


def build_pr_body(
    *,
    plan: Plan,
    index_1based: int,
    target: RepoTarget,
    siblings: dict[str, str],
) -> str:
    """Render the PR body for one repo in the chain.

    Args:
        plan: the parent plan (carries feature_id, parent issue, etc.).
        index_1based: this PR's position in the chain (1, 2, ...).
        target: the repo target this body is for.
        siblings: previously-opened PRs by repo name (used to link
            forward; the chain re-renders earlier PR bodies when a later
            one opens, so eventually every PR lists every sibling).

    Returns:
        Markdown body string.
    """
    parent_link = (
        f"https://github.com/{plan.parent_gh_org}/{plan.parent_repo}"
        f"/issues/{plan.parent_issue}"
    )
    sibling_lines: list[str] = []
    for i, t in enumerate(plan.targets, start=1):
        url = siblings.get(t.repo_name)
        if url:
            sibling_lines.append(f"- Part {i} ({t.repo_name}): {url}")
        elif i == index_1based:
            sibling_lines.append(f"- Part {i} ({t.repo_name}): (this PR)")
        else:
            sibling_lines.append(f"- Part {i} ({t.repo_name}): pending")
    siblings_section = "\n".join(sibling_lines)

    acceptance = target.acceptance_criteria.strip() or "(see linked issue)"

    return (
        f"Part {index_1based} of {plan.total()}: {plan.feature_title}\n\n"
        f"## Context\n\n"
        f"**Parent Issue:** [{plan.parent_repo}#{plan.parent_issue}]"
        f"({parent_link})\n\n"
        f"**Feature:** {plan.feature_title}\n\n"
        f"## Sibling PRs\n\n"
        f"{siblings_section}\n\n"
        f"## Acceptance Criteria (this repo)\n\n"
        f"{acceptance}\n\n"
        f"---\n\n"
        f"**Approved by {plan.approved_by}**  \n"
        f"**Feature ID:** {plan.feature_id}\n"
    )


# --------------------------------------------------------------------------
# Coordinator.
# --------------------------------------------------------------------------


@dataclass
class CrossRepoPRChain:
    """Sequential N-PR chain coordinator.

    ``client`` is the injected :class:`GitHubClient`. ``state_dir`` is
    where chain state lives on disk; defaults to
    ``$ALFRED_HOME/state/pr-chains`` and overridable for tests.
    """

    client: GitHubClient = field(default_factory=SubprocessGitHubClient)
    state_dir: Path = field(default_factory=_default_state_dir)
    now: float | None = None

    # ----- plan -----------------------------------------------------------

    def plan(
        self,
        *,
        feature_id: str,
        feature_title: str,
        parent_repo: str,
        parent_issue: int,
        parent_gh_org: str,
        targets: Sequence[RepoTarget],
        approved_by: str = "operator",
    ) -> Plan:
        """Build a declarative plan. Pure; no I/O."""
        if not targets:
            raise ValueError("at least one RepoTarget required")
        seen: set[str] = set()
        for t in targets:
            if t.repo_name in seen:
                raise ValueError(f"duplicate repo_name in targets: {t.repo_name}")
            seen.add(t.repo_name)
        return Plan(
            feature_id=feature_id,
            feature_title=feature_title,
            parent_repo=parent_repo,
            parent_issue=parent_issue,
            parent_gh_org=parent_gh_org,
            targets=tuple(targets),
            state_file=state_file_path(feature_id, state_dir=self.state_dir),
            approved_by=approved_by,
        )

    # ----- execute --------------------------------------------------------

    def execute(self, plan: Plan) -> Result:
        """Carry out a plan. Idempotent on re-run if state file is present."""
        existing = load_chain_state(plan.state_file)
        state = existing or ChainState(
            feature_id=plan.feature_id,
            parent_repo=plan.parent_repo,
            parent_issue=plan.parent_issue,
            repos=[t.repo_name for t in plan.targets],
            prs={},
            created_at=self.now if self.now is not None else time.time(),
        )
        save_chain_state(state, plan.state_file)

        result = Result(feature_id=plan.feature_id, opened=dict(state.prs))

        for idx, target in enumerate(plan.targets, start=1):
            if target.repo_name in state.prs:
                logger.info(
                    "skip %s: PR already opened at %s",
                    target.repo_name,
                    state.prs[target.repo_name],
                )
                continue

            body = build_pr_body(
                plan=plan,
                index_1based=idx,
                target=target,
                siblings=state.prs,
            )
            labels = list(plan.base_labels) + list(target.extra_labels)
            url = self.client.pr_create(
                target.gh_repo,
                title=target.title,
                body=body,
                head=target.branch,
                labels=labels,
            )
            if not url:
                result.failed.append(target.repo_name)
                continue

            state.prs[target.repo_name] = url
            result.opened[target.repo_name] = url
            save_chain_state(state, plan.state_file)

            # Re-render earlier PRs so they link forward to this new sibling.
            self._refresh_earlier_bodies(plan, state, up_to_idx=idx)

        return result

    def _refresh_earlier_bodies(
        self, plan: Plan, state: ChainState, *, up_to_idx: int
    ) -> None:
        for earlier_idx, earlier_target in enumerate(plan.targets[: up_to_idx - 1], start=1):
            url = state.prs.get(earlier_target.repo_name)
            if not url:
                continue
            new_body = build_pr_body(
                plan=plan,
                index_1based=earlier_idx,
                target=earlier_target,
                siblings=state.prs,
            )
            ok = self.client.pr_edit_body(earlier_target.gh_repo, url, body=new_body)
            if not ok:
                logger.warning(
                    "failed to refresh sibling-links body on %s", url
                )


# --------------------------------------------------------------------------
# CI-readiness polling.
# --------------------------------------------------------------------------


_CI_RED_CONCLUSIONS = ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
_CI_PENDING_STATUSES = ("IN_PROGRESS", "QUEUED", "PENDING")


def classify_status_checks(checks: list[dict] | None) -> str:
    """Classify a ``statusCheckRollup`` payload as ``green | red | pending``.

    Empty list means no required checks; treat as green. Red dominates
    pending: any failed conclusion classifies the whole rollup as red.
    """
    checks = checks or []
    if any((c.get("conclusion") or "").upper() in _CI_RED_CONCLUSIONS for c in checks):
        return "red"
    if any((c.get("status") or "").upper() in _CI_PENDING_STATUSES for c in checks):
        return "pending"
    return "green"


def wait_for_ci_green(
    client: GitHubClient,
    gh_repo: str,
    pr_url: str,
    *,
    timeout_seconds: int = 900,
    poll_interval: int = 30,
    sleeper: object | None = None,
) -> bool:
    """Poll a PR's CI rollup until it goes green/red or the timeout hits.

    Args:
        client: injected GitHub client.
        gh_repo: ``org/repo``.
        pr_url: full PR URL (the form ``gh`` accepts as an identifier).
        timeout_seconds: total wall time before giving up.
        poll_interval: seconds between polls.
        sleeper: callable for sleep; defaults to :func:`time.sleep`.
            Inject in tests to avoid real sleeps.

    Returns:
        True if CI went green; False on red or timeout.
    """
    sleep_fn = sleeper if callable(sleeper) else time.sleep
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        checks = client.pr_status_checks(gh_repo, pr_url)
        verdict = classify_status_checks(checks)
        if verdict == "green":
            return True
        if verdict == "red":
            return False
        sleep_fn(poll_interval)
    return False
