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
