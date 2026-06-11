"""Assign a label-free GitHub issue to the right Alfred agent lane.

This module is deliberately side-effect-light at the top level:

* ``decide_assignment`` is pure and testable.
* ``assign_issue`` validates the repo allowlist, fetches one issue with ``gh``,
  then either dry-runs or applies the decided labels.

The two executable lanes are the existing state machine labels:

* Lucius: ``agent:implement`` for single-repo implementation work.
* Batman: ``agent:large-feature`` for large / multi-repo planning work.

Vague work is not forced into either lane. It gets ``needs:human-scope`` so a
human can clarify it before an autonomous agent spends context guessing.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

import labels as label_constants
from issue_queue import allowed_queue_repos
from shipped_board import _gh_bin, _gh_subprocess_env

ROUTE_LUCIUS = "lucius"
ROUTE_BATMAN = "batman"
ROUTE_HUMAN_SCOPE = "human_scope"
ROUTE_ALREADY_ROUTED = "already_routed"
ROUTE_BLOCKED = "blocked"

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

_ACTIONABLE_CUES = (
    "add",
    "build",
    "change",
    "create",
    "fix",
    "implement",
    "improve",
    "make",
    "remove",
    "repair",
    "support",
    "test",
    "update",
    "wire",
)

_BATMAN_SCOPE_CUES = (
    "across repos",
    "across repositories",
    "all repos",
    "architecture",
    "bundle",
    "cross repo",
    "cross-repo",
    "dependency order",
    "end to end",
    "end-to-end",
    "e2e",
    "migration",
    "multi repo",
    "multi-repo",
    "multiple repos",
    "platform-wide",
    "rollout",
)

_SURFACE_ALIASES: dict[str, tuple[str, ...]] = {
    "backend": (
        "api",
        "backend",
        "server",
        "service",
    ),
    "frontend": (
        "dashboard",
        "frontend",
        "react",
        "ui",
        "web",
        "web app",
    ),
    "mobile": (
        "android",
        "expo",
        "ios",
        "mobile",
        "mobile app",
    ),
    "nango": (
        "integration service",
        "integrations",
        "nango",
    ),
    "agents": (
        "agent service",
        "agents service",
    ),
    "data-acquisition": (
        "data acquisition",
        "data-acquisition",
        "extractor",
        "extractors",
    ),
    "alfred": (
        "alfred",
        "cli",
        "desktop client",
        "native client",
        "orchestrator",
        "slack listener",
    ),
}


@dataclass(frozen=True)
class IssueSnapshot:
    repo: str
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    state: str = "OPEN"
    url: str = ""

    @classmethod
    def from_gh_payload(cls, repo: str, number: int, payload: dict[str, Any]) -> IssueSnapshot:
        return cls(
            repo=repo,
            number=int(payload.get("number") or number),
            title=str(payload.get("title") or ""),
            body=str(payload.get("body") or ""),
            labels=tuple(_label_names(payload.get("labels") or [])),
            state=str(payload.get("state") or "OPEN").upper(),
            url=str(payload.get("url") or ""),
        )


@dataclass(frozen=True)
class AssignmentDecision:
    route: str
    agent: str
    add_labels: tuple[str, ...]
    remove_labels: tuple[str, ...]
    reason: str
    confidence: float

    @property
    def changed(self) -> bool:
        return bool(self.add_labels or self.remove_labels)


@dataclass(frozen=True)
class AssignmentResult:
    ok: bool
    repo: str
    number: int
    url: str
    dry_run: bool
    decision: AssignmentDecision
    detail: str
    error: str = ""

    @property
    def changed(self) -> bool:
        return self.decision.changed and self.ok and not self.dry_run

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        data["changed"] = self.changed
        return data


GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Fetcher = Callable[[str, int], IssueSnapshot]


def fetch_issue_snapshot(repo: str, number: int) -> IssueSnapshot:
    """Fetch one issue via ``gh issue view``."""
    proc = _run_gh(
        [
            _gh_bin(),
            "issue",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "number,title,body,labels,state,url",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh issue view failed").strip())
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh issue view returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("gh issue view returned a non-object payload")
    return IssueSnapshot.from_gh_payload(repo, number, payload)


def decide_assignment(issue: IssueSnapshot) -> AssignmentDecision:
    """Return the lane and labels for ``issue`` without touching GitHub."""
    labels = set(issue.labels)
    if issue.state.upper() != "OPEN":
        return AssignmentDecision(
            route=ROUTE_BLOCKED,
            agent="none",
            add_labels=(),
            remove_labels=(),
            reason=f"issue is {issue.state.lower()}",
            confidence=1.0,
        )

    blockers = _assignment_blocking_labels(labels)
    if blockers:
        return AssignmentDecision(
            route=ROUTE_BLOCKED,
            agent="none",
            add_labels=(),
            remove_labels=(),
            reason="blocked from autonomous pickup by label(s): " + ", ".join(blockers),
            confidence=1.0,
        )

    if label_constants.LARGE_FEATURE in labels or any(
        label_constants.is_bundle_label(label) for label in labels
    ):
        return AssignmentDecision(
            route=ROUTE_ALREADY_ROUTED,
            agent="batman",
            add_labels=(),
            remove_labels=(),
            reason="already routed to Batman",
            confidence=1.0,
        )

    if label_constants.IMPLEMENT in labels:
        return AssignmentDecision(
            route=ROUTE_ALREADY_ROUTED,
            agent="lucius",
            add_labels=(),
            remove_labels=(),
            reason="already routed to Lucius",
            confidence=1.0,
        )

    text = _issue_text(issue)
    surfaces = _mentioned_surfaces(text, repo=issue.repo)
    remove = (label_constants.DO_NOT_PICKUP,) if label_constants.DO_NOT_PICKUP in labels else ()

    if _needs_human_scope(issue, text):
        return AssignmentDecision(
            route=ROUTE_HUMAN_SCOPE,
            agent="human",
            add_labels=_missing(labels, label_constants.NEEDS_HUMAN_SCOPE),
            remove_labels=(),
            reason="not enough actionable scope for Batman or Lucius",
            confidence=0.76,
        )

    if _should_route_to_batman(text, surfaces):
        return AssignmentDecision(
            route=ROUTE_BATMAN,
            agent="batman",
            add_labels=_missing(labels, label_constants.LARGE_FEATURE),
            remove_labels=remove,
            reason=_batman_reason(text, surfaces),
            confidence=0.84 if len(surfaces) >= 2 else 0.72,
        )

    lucius_blockers = _lucius_assignment_blocking_labels(labels)
    if lucius_blockers:
        return AssignmentDecision(
            route=ROUTE_BLOCKED,
            agent="none",
            add_labels=(),
            remove_labels=(),
            reason="Lucius pickup is blocked by label(s): " + ", ".join(lucius_blockers),
            confidence=1.0,
        )

    return AssignmentDecision(
        route=ROUTE_LUCIUS,
        agent="lucius",
        add_labels=_missing(labels, label_constants.IMPLEMENT),
        remove_labels=remove,
        reason="single-repo implementation work",
        confidence=0.82,
    )


def assign_issue(
    repo: str,
    number: int,
    *,
    dry_run: bool = False,
    fetcher: Fetcher = fetch_issue_snapshot,
    runner: GhRunner | None = None,
) -> AssignmentResult:
    """Fetch, decide, and optionally apply an issue assignment."""
    runner = runner or _run_gh
    validation_error = _validate_target(repo, number)
    if validation_error:
        return _error_result(repo, number, validation_error, dry_run=dry_run)

    try:
        issue = fetcher(repo, number)
    except Exception as exc:
        return _error_result(repo, number, str(exc), dry_run=dry_run)

    decision = decide_assignment(issue)
    if decision.route == ROUTE_BLOCKED and not dry_run:
        return AssignmentResult(
            ok=False,
            repo=repo,
            number=number,
            url=issue.url,
            dry_run=False,
            decision=decision,
            detail=render_assignment_detail(repo, number, decision, dry_run=False),
            error=decision.reason,
        )
    if dry_run or not decision.changed:
        return AssignmentResult(
            ok=True,
            repo=repo,
            number=number,
            url=issue.url,
            dry_run=dry_run,
            decision=decision,
            detail=render_assignment_detail(repo, number, decision, dry_run=dry_run),
        )

    _ensure_labels(repo, decision.add_labels, runner=runner)
    cmd = [_gh_bin(), "issue", "edit", str(number), "-R", repo]
    for label in decision.add_labels:
        cmd.extend(["--add-label", label])
    for label in decision.remove_labels:
        cmd.extend(["--remove-label", label])
    proc = runner(cmd)
    if proc.returncode != 0:
        return AssignmentResult(
            ok=False,
            repo=repo,
            number=number,
            url=issue.url,
            dry_run=False,
            decision=decision,
            detail="assignment failed",
            error=(proc.stderr or proc.stdout or "gh issue edit failed").strip(),
        )

    return AssignmentResult(
        ok=True,
        repo=repo,
        number=number,
        url=issue.url,
        dry_run=False,
        decision=decision,
        detail=render_assignment_detail(repo, number, decision, dry_run=False),
    )


def render_assignment_detail(
    repo: str,
    number: int,
    decision: AssignmentDecision,
    *,
    dry_run: bool = False,
) -> str:
    """Human-readable, Slack-safe summary of a decision."""
    target = f"{repo}#{number}"
    prefix = "Dry run: would " if dry_run and decision.changed else ""
    if decision.route == ROUTE_LUCIUS:
        return (
            f"{prefix}assign {target} to Lucius "
            f"by adding `{label_constants.IMPLEMENT}`. Reason: {decision.reason}."
        )
    if decision.route == ROUTE_BATMAN:
        return (
            f"{prefix}assign {target} to Batman "
            f"by adding `{label_constants.LARGE_FEATURE}`. Reason: {decision.reason}."
        )
    if decision.route == ROUTE_HUMAN_SCOPE:
        return (
            f"{prefix}mark {target} `{label_constants.NEEDS_HUMAN_SCOPE}`. "
            f"Reason: {decision.reason}."
        )
    if decision.route == ROUTE_ALREADY_ROUTED:
        return f"{target} is already routed to {decision.agent}. Reason: {decision.reason}."
    return f"{target} was not assigned. Reason: {decision.reason}."


def _validate_target(repo: str, number: int) -> str:
    if not _REPO_SLUG_RE.match(repo or ""):
        return f"invalid repo slug: {repo!r}"
    if number <= 0:
        return f"invalid issue number: {number}"
    allowed = allowed_queue_repos()
    if not allowed:
        return (
            "assignment repo allowlist is not configured; set ALFRED_QUEUE_REPOS, "
            "ALFRED_SHIPPED_REPOS, or ALFRED_BRIDGE_REPOS"
        )
    if repo.lower() not in allowed:
        return f"repo not in Alfred assignment allowlist: {repo}"
    return ""


def _error_result(repo: str, number: int, error: str, *, dry_run: bool) -> AssignmentResult:
    decision = AssignmentDecision(
        route=ROUTE_BLOCKED,
        agent="none",
        add_labels=(),
        remove_labels=(),
        reason="assignment could not be evaluated",
        confidence=0.0,
    )
    return AssignmentResult(
        ok=False,
        repo=repo,
        number=number,
        url="",
        dry_run=dry_run,
        decision=decision,
        detail="assignment failed",
        error=error,
    )


def _run_gh(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=_gh_subprocess_env(),
    )


def _ensure_labels(repo: str, labels: tuple[str, ...], *, runner: GhRunner) -> None:
    """Best-effort label creation for fresh installs.

    Existing labels make ``gh label create`` exit non-zero; that is fine. The
    subsequent ``gh issue edit`` is the authoritative success/failure signal.
    """
    meta = {
        label_constants.IMPLEMENT: (
            "ffa500",
            "Eligible for autonomous pickup. Set by Alfred assignment.",
        ),
        label_constants.LARGE_FEATURE: (
            "ff6b00",
            "Large or multi-repo feature routed to Batman.",
        ),
        label_constants.NEEDS_HUMAN_SCOPE: (
            "e99695",
            "Requires manual scoping before autonomous pickup.",
        ),
    }
    for label in labels:
        color, desc = meta.get(label, ("cccccc", "Alfred-managed label"))
        runner(
            [
                _gh_bin(),
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                desc,
                "-R",
                repo,
            ]
        )


def _label_names(raw_labels: list[Any]) -> list[str]:
    out: list[str] = []
    for label in raw_labels:
        if isinstance(label, dict):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            out.append(name)
    return sorted(set(out))


def _missing(labels: set[str], *wanted: str) -> tuple[str, ...]:
    return tuple(label for label in wanted if label not in labels)


def _assignment_blocking_labels(labels: set[str]) -> list[str]:
    blockers = set(label_constants.pickup_blocking_labels(labels))
    blockers.discard(label_constants.DO_NOT_PICKUP)
    blockers.discard(label_constants.LARGE_FEATURE)
    blockers = {label for label in blockers if not label_constants.is_bundle_label(label)}
    return sorted(blockers)


def _lucius_assignment_blocking_labels(labels: set[str]) -> list[str]:
    candidate_labels = set(labels)
    candidate_labels.add(label_constants.IMPLEMENT)
    blockers = set(label_constants.feature_dev_pickup_blocking_labels(candidate_labels))
    blockers.discard(label_constants.DO_NOT_PICKUP)
    return sorted(blockers)


def _issue_text(issue: IssueSnapshot) -> str:
    return _normalize(f"{issue.title}\n{issue.body}")


def _normalize(text: str) -> str:
    lowered = (text or "").lower()
    lowered = lowered.replace("&", " and ")
    return re.sub(r"\s+", " ", lowered).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = _normalize(phrase)
    if not phrase:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _mentioned_surfaces(text: str, *, repo: str) -> frozenset[str]:
    surfaces: set[str] = set()
    bare_repo = repo.rsplit("/", 1)[-1].lower()
    for surface, aliases in _SURFACE_ALIASES.items():
        if any(_contains_phrase(text, alias) for alias in aliases):
            surfaces.add(surface)
    repo_surface = _surface_for_repo(bare_repo)
    if repo_surface:
        surfaces.add(repo_surface)
    return frozenset(surfaces)


def _surface_for_repo(bare_repo: str) -> str:
    if bare_repo == "alfred":
        return "alfred"
    for surface in _SURFACE_ALIASES:
        if bare_repo.endswith(surface):
            return surface
    return ""


def _needs_human_scope(issue: IssueSnapshot, text: str) -> bool:
    words = re.findall(r"[a-z0-9]+", text)
    if len(words) >= 4 and any(_contains_phrase(text, cue) for cue in _ACTIONABLE_CUES):
        return False
    if len((issue.title or "").strip()) < 8 and not (issue.body or "").strip():
        return True
    return len(words) < 4


def _should_route_to_batman(text: str, surfaces: frozenset[str]) -> bool:
    non_alfred_surfaces = {surface for surface in surfaces if surface != "alfred"}
    if len(non_alfred_surfaces) >= 2:
        return True
    has_scope_cue = any(_contains_phrase(text, cue) for cue in _BATMAN_SCOPE_CUES)
    return has_scope_cue and ("repo" in text or "repository" in text)


def _batman_reason(text: str, surfaces: frozenset[str]) -> str:
    non_alfred_surfaces = sorted(surface for surface in surfaces if surface != "alfred")
    if len(non_alfred_surfaces) >= 2:
        return "mentions multiple product surfaces: " + ", ".join(non_alfred_surfaces)
    cue = next((cue for cue in _BATMAN_SCOPE_CUES if _contains_phrase(text, cue)), "")
    return f"large-feature cue: {cue}" if cue else "large-feature scope"


__all__ = [
    "ROUTE_ALREADY_ROUTED",
    "ROUTE_BATMAN",
    "ROUTE_BLOCKED",
    "ROUTE_HUMAN_SCOPE",
    "ROUTE_LUCIUS",
    "AssignmentDecision",
    "AssignmentResult",
    "IssueSnapshot",
    "assign_issue",
    "decide_assignment",
    "fetch_issue_snapshot",
    "render_assignment_detail",
]
