"""Spec template and lint helpers for Alfred-operated issue queues."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("problem", ("problem", "context")),
    ("goals", ("goals", "goal")),
    ("non_goals", ("non-goals", "non goals", "out of scope", "non-goal")),
    ("repo_scope", ("repositories", "repos", "repo scope", "affected repos")),
    ("acceptance_criteria", ("acceptance criteria", "acceptance")),
    ("test_plan", ("test plan", "testing")),
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
