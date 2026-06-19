"""Spec template and lint helpers for Alfred-operated issue queues."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("problem", ("problem", "context", "objective")),
    ("goals", ("goals", "goal", "desired behavior")),
    ("non_goals", ("non-goals", "non goals", "out of scope", "non-goal")),
    (
        "repo_scope",
        ("repositories", "repos", "repo scope", "repository scope", "affected repos"),
    ),
    ("acceptance_criteria", ("acceptance criteria", "acceptance")),
    ("test_plan", ("test plan", "testing", "verification plan")),
    ("rollout", ("rollout", "release plan")),
    ("open_questions", ("open questions", "questions")),
)


@dataclass(frozen=True)
class SpecFinding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class SpecLintResult:
    path: str
    ok: bool
    findings: list[SpecFinding]


@dataclass(frozen=True)
class IssueDraft:
    """Structured issue/spec intake used by the local planning UI."""

    title: str
    problem: str = ""
    user: str = ""
    current_behavior: str = ""
    desired_behavior: str = ""
    repos: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    test_plan: str = ""
    out_of_scope: str = ""
    rollout: str = ""
    open_questions: str = ""


@dataclass(frozen=True)
class IssueReadinessResult:
    """Readiness verdict for Alfred-operated work."""

    ok: bool
    score: int
    findings: tuple[SpecFinding, ...]
    questions: tuple[str, ...]
    issue_body: str


def render_spec_template(title: str, repos: list[str] | None = None) -> str:
    repo_lines = "\n".join(f"- `{repo}`" for repo in (repos or [])) or "- `owner/repo`"
    return f"""# {title.strip()}

## Problem

What user or operator problem are we solving?

## Goals

- TODO

## Non-goals

- TODO

## Repositories

{repo_lines}

## User Stories

- As a user, I can ...

## Acceptance Criteria

- [ ] TODO

## Test Plan

- [ ] Unit tests cover ...
- [ ] Integration or manual verification covers ...

## Rollout

- TODO

## Open Questions

- TODO
"""


def write_spec_template(path: Path, title: str, repos: list[str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_spec_template(title, repos), encoding="utf-8")
    return path


def lint_spec_text(text: str, *, path: str = "<memory>") -> SpecLintResult:
    headings = _headings(text)
    normalized = {_normalize_heading(h) for h in headings}
    findings: list[SpecFinding] = []
    for code, aliases in REQUIRED_SECTIONS:
        if not any(alias in normalized for alias in aliases):
            findings.append(
                SpecFinding(
                    code=f"missing_{code}",
                    severity="error",
                    message=f"Missing section: {aliases[0]}",
                )
            )

    acceptance_body = _section_body(text, ("acceptance criteria", "acceptance"))
    if acceptance_body is not None and not _has_checkbox(acceptance_body):
        findings.append(
            SpecFinding(
                code="acceptance_not_checklist",
                severity="warning",
                message="Acceptance criteria should be written as checkboxes.",
            )
        )
    if _looks_like_empty_template(text):
        findings.append(
            SpecFinding(
                code="template_placeholders",
                severity="warning",
                message="Spec still appears to contain empty template placeholders.",
            )
        )
    return SpecLintResult(
        path=path,
        ok=not any(f.severity == "error" for f in findings),
        findings=findings,
    )


def lint_spec_file(path: Path) -> SpecLintResult:
    return lint_spec_text(path.read_text(encoding="utf-8"), path=str(path))


def assess_issue_draft(draft: IssueDraft) -> IssueReadinessResult:
    """Assess whether a local issue/spec draft is concrete enough to run.

    The bar is intentionally practical: Alfred should not implement when
    the problem, expected behavior, repo scope, acceptance criteria, or
    verification path are missing. Warnings still allow saving the draft,
    but blockers should keep Batman/Lucius in planning mode.
    """
    issue_body = render_issue_body(draft)
    findings: list[SpecFinding] = []
    questions: list[str] = []

    if len(draft.title.strip()) < 8:
        findings.append(_finding("missing_title", "error", "Add a specific issue title."))
        questions.append("What should Alfred call this work?")
    elif _looks_vague(draft.title):
        findings.append(
            _finding(
                "vague_title",
                "warning",
                "Title uses broad wording. Name the user-visible outcome.",
            )
        )

    if len(_plain(draft.problem)) < 30:
        findings.append(
            _finding(
                "missing_problem",
                "error",
                "Explain the user/operator problem in at least one concrete paragraph.",
            )
        )
        questions.append("What problem is the user facing today?")

    if len(_plain(draft.desired_behavior)) < 30:
        findings.append(
            _finding(
                "missing_desired_behavior",
                "error",
                "Describe the exact behavior Alfred should make true.",
            )
        )
        questions.append("What should be different when this ships?")

    clean_repos = [repo for repo in draft.repos if _valid_repo(repo)]
    if not clean_repos:
        findings.append(
            _finding(
                "missing_repo_scope",
                "error",
                "Choose at least one concrete GitHub repo, such as owner/repo.",
            )
        )
        questions.append("Which part of the workspace should Alfred change?")

    actionable_acceptance = [
        item for item in draft.acceptance_criteria if _plain(item) and not _is_placeholder(item)
    ]
    if not actionable_acceptance:
        findings.append(
            _finding(
                "missing_acceptance_criteria",
                "error",
                "Add checklist-style acceptance criteria with observable outcomes.",
            )
        )
        questions.append("How will you verify this worked after Alfred opens a PR?")
    elif any(_looks_vague(item) for item in actionable_acceptance):
        findings.append(
            _finding(
                "vague_acceptance_criteria",
                "warning",
                "Some acceptance criteria use vague wording. Prefer observable checks.",
            )
        )

    if len(_plain(draft.test_plan)) < 15:
        findings.append(
            _finding(
                "missing_test_plan",
                "error",
                "Add a verification plan, even if it is a manual check.",
            )
        )
        questions.append("What should Alfred test or manually check before opening a PR?")

    if _has_unresolved_open_questions(draft.open_questions):
        findings.append(
            _finding(
                "open_questions_unresolved",
                "error",
                "Resolve or explicitly accept open questions before Alfred implements.",
            )
        )
        questions.append("Which open questions are resolved, and which are accepted as risk?")

    if not _plain(draft.out_of_scope):
        findings.append(
            _finding(
                "missing_non_goals",
                "warning",
                "Add out-of-scope notes so Alfred does not overbuild.",
            )
        )

    if _contains_placeholder(issue_body):
        findings.append(
            _finding(
                "template_placeholders",
                "warning",
                "Draft still contains placeholders or TODO text.",
            )
        )

    blocker_count = sum(1 for finding in findings if finding.severity == "error")
    warning_count = sum(1 for finding in findings if finding.severity == "warning")
    score = max(0, 100 - blocker_count * 22 - warning_count * 6)
    return IssueReadinessResult(
        ok=blocker_count == 0,
        score=score,
        findings=tuple(findings),
        questions=tuple(_dedupe(questions)),
        issue_body=issue_body,
    )


def render_issue_body(draft: IssueDraft) -> str:
    """Render a GitHub-ready issue body from structured local intake."""
    repos = draft.repos or ["owner/repo"]
    acceptance = draft.acceptance_criteria or ["TODO"]
    repo_lines = (
        "\n".join(f"- `{repo.strip()}`" for repo in repos if repo.strip()) or "- `owner/repo`"
    )
    acceptance_lines = (
        "\n".join(f"- [ ] {item.strip()}" for item in acceptance if item.strip()) or "- [ ] TODO"
    )
    sections = [
        f"# {draft.title.strip() or 'Untitled Alfred work'}",
        "## Problem",
        draft.problem.strip() or "TODO",
        "## User",
        draft.user.strip() or "Not specified.",
        "## Current Behavior",
        draft.current_behavior.strip() or "Not specified.",
        "## Desired Behavior",
        draft.desired_behavior.strip() or "TODO",
        "## Repositories",
        repo_lines,
        "## Acceptance Criteria",
        acceptance_lines,
        "## Test Plan",
        draft.test_plan.strip() or "TODO",
        "## Non-goals",
        draft.out_of_scope.strip() or "Not specified.",
        "## Rollout",
        draft.rollout.strip() or "Normal Alfred PR review.",
        "## Open Questions",
        draft.open_questions.strip() or "None.",
    ]
    return "\n\n".join(sections).strip() + "\n"


def _headings(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)]


def _normalize_heading(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"[^a-z0-9/ -]+", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _has_section(normalized_headings: set[str], aliases: tuple[str, ...]) -> bool:
    return any(alias in normalized_headings for alias in aliases)


def _section_body(text: str, aliases: tuple[str, ...]) -> str | None:
    heading_matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE))
    alias_set = set(aliases)
    for index, match in enumerate(heading_matches):
        level = len(match.group(1))
        if _normalize_heading(match.group(2)) not in alias_set:
            continue

        end = len(text)
        for next_match in heading_matches[index + 1 :]:
            if len(next_match.group(1)) <= level:
                end = next_match.start()
                break
        return text[match.end() : end]
    return None


def _has_checkbox(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*-\s+\[[ xX]\]\s+", text))


def _looks_like_empty_template(text: str) -> bool:
    lines = [line.rstrip() for line in text.splitlines()]
    empty_bullets = sum(1 for line in lines if line in {"- ", "- [ ] "})
    return empty_bullets >= 2 or "What user or operator problem are we solving?" in text


def _finding(code: str, severity: str, message: str) -> SpecFinding:
    return SpecFinding(code=code, severity=severity, message=message)


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _valid_repo(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value.strip()))


def _contains_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ("todo", "tbd", "???", "placeholder"))


def _is_placeholder(value: str) -> bool:
    lowered = _plain(value).lower()
    return not lowered or lowered in {"todo", "tbd", "none", "n/a", "placeholder"}


def _has_unresolved_open_questions(value: str) -> bool:
    cleaned = _plain(value).lower().strip(".")
    return bool(cleaned) and cleaned not in {
        "accepted as risk",
        "accepted risk",
        "n/a",
        "no",
        "no open questions",
        "none",
        "not applicable",
    }


def _looks_vague(value: str) -> bool:
    lowered = _plain(value).lower()
    vague_terms = {
        "better",
        "clean up",
        "etc",
        "improve",
        "maybe",
        "nice",
        "polish",
        "stuff",
        "things",
    }
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in vague_terms)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
