"""Tests for ``alfred spec`` helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_spec_template_lints(tmp_path: Path) -> None:
    from spec_helper import lint_spec_file, write_spec_template

    path = write_spec_template(tmp_path / "spec.md", "Add memory", ["org/api"])
    result = lint_spec_file(path)
    assert result.ok
    assert any(f.code == "template_placeholders" for f in result.findings)


def test_spec_lint_reports_missing_sections(tmp_path: Path) -> None:
    from spec_helper import lint_spec_file

    path = tmp_path / "thin.md"
    path.write_text("# Thin\n\n## Problem\n\nSomething.\n", encoding="utf-8")
    result = lint_spec_file(path)
    assert not result.ok
    codes = {f.code for f in result.findings}
    assert "missing_acceptance_criteria" in codes
    assert "missing_test_plan" in codes


def test_spec_lint_warns_when_acceptance_criteria_is_not_checklist() -> None:
    from spec_helper import lint_spec_text

    result = lint_spec_text(
        """# Add memory

## Problem

Something.

## Goals

- Make agents remember useful facts.

## Non-goals

- Replace human review.

## Repositories

- `org/repo`

## Acceptance Criteria

Memory candidates are visible before promotion.

## Test Plan

- [ ] Unit tests cover linting.

## Rollout

- Ship behind normal review.

## Open Questions

- None.
"""
    )

    assert result.ok
    assert any(f.code == "acceptance_not_checklist" for f in result.findings)


def test_issue_readiness_blocks_vague_drafts() -> None:
    from spec_helper import IssueDraft, assess_issue_draft

    result = assess_issue_draft(
        IssueDraft(
            title="Make it better",
            problem="It is confusing.",
            desired_behavior="Polish stuff.",
            repos=["owner/repo"],
            acceptance_criteria=["Improve things"],
            test_plan="",
        )
    )

    assert not result.ok
    codes = {finding.code for finding in result.findings}
    assert "missing_problem" in codes
    assert "missing_desired_behavior" in codes
    assert "missing_test_plan" in codes
    assert result.questions


def test_issue_readiness_renders_github_ready_issue() -> None:
    from spec_helper import IssueDraft, assess_issue_draft

    result = assess_issue_draft(
        IssueDraft(
            title="Add Slack plan revision flow",
            problem=(
                "Designers need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            user="Non-developer repo owner",
            current_behavior="Batman posts a plan and waits for emoji approval.",
            desired_behavior=(
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            repos=["luminik-io/alfred-os"],
            acceptance_criteria=[
                "A plan with unresolved questions is marked needs-scope.",
                "Slack plan messages tell the operator how to reply with changes.",
            ],
            test_plan="Run Batman plan unit tests and manually inspect the Slack payload.",
            out_of_scope="No automatic GitHub issue creation from the planning UI.",
        )
    )

    assert result.ok
    assert result.score >= 80
    assert "## Acceptance Criteria" in result.issue_body
    assert "- [ ] A plan with unresolved questions" in result.issue_body


def test_issue_readiness_does_not_add_todo_for_optional_blanks() -> None:
    from spec_helper import IssueDraft, assess_issue_draft

    result = assess_issue_draft(
        IssueDraft(
            title="Add Slack plan revision flow",
            problem=(
                "Designers need to discuss a Batman plan before implementation "
                "so Alfred does not ship the wrong workflow."
            ),
            desired_behavior=(
                "Batman keeps implementation paused when a plan needs revision "
                "and accepts thread feedback before child issues are filed."
            ),
            repos=["luminik-io/alfred-os"],
            acceptance_criteria=[
                "A plan with unresolved questions is marked needs-scope.",
                "Slack plan messages tell the operator how to reply with changes.",
            ],
            test_plan="Run Batman plan unit tests and manually inspect the Slack payload.",
        )
    )

    assert result.ok
    assert "TODO" not in result.issue_body
    assert not any(f.code == "template_placeholders" for f in result.findings)


def test_alfred_spec_cli_new_and_lint(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    mod = _load("alfred_spec_cli", ROOT / "bin" / "alfred-spec.py")
    spec_path = tmp_path / "feature.md"
    rc = mod.main(["new", "Add memory", "--repo", "org/api", "--out", str(spec_path)])
    assert rc == 0
    assert spec_path.exists()
    capsys.readouterr()

    rc = mod.main(["lint", str(spec_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_alfred_spec_cli_assess_blocks_vague_work(capsys) -> None:  # type: ignore[no-untyped-def]
    mod = _load("alfred_spec_cli_assess", ROOT / "bin" / "alfred-spec.py")

    rc = mod.main(
        [
            "assess",
            "Make it better",
            "--problem",
            "Confusing.",
            "--desired-behavior",
            "Improve stuff.",
            "--repo",
            "org/api",
            "--acceptance",
            "Make it nice",
            "--json",
        ]
    )

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(finding["severity"] == "error" for finding in payload["findings"])
