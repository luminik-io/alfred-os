"""The seeded prompt-template gate: an unmodified auto-seed must not be injected.

alfred-init seeds starter prompt templates; bin/lucius.py defers to its in-code
guidance until the operator edits the file. Detection is by exact content match
against the shipped template (with or without the auto-seed marker), so both new
seeds and legacy pre-marker seeds are recognized, while any edit is honored.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
_LIB = Path(__file__).resolve().parent.parent / "lib"
_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_TEMPLATE = (_PROMPTS / "feature-dev.md").read_text(encoding="utf-8")


def _strip_marker(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(lines[1:]) if lines and "alfred:auto-seed" in lines[0] else text


@pytest.fixture()
def lucius(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    spec = importlib.util.spec_from_file_location("lucius_under_test", _BIN / "lucius.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fresh_seed_with_marker_is_skipped(lucius, tmp_path):
    seed = tmp_path / "lucius.md"
    seed.write_text(_TEMPLATE, encoding="utf-8")  # verbatim copy alfred-init makes
    assert lucius._is_unmodified_auto_seed(seed) is True


def test_legacy_seed_without_marker_is_skipped(lucius, tmp_path):
    # A seed copied by a release before the marker existed: marker line absent
    # but body identical. Must still be recognized as an untouched auto-seed.
    seed = tmp_path / "lucius.md"
    seed.write_text(_strip_marker(_TEMPLATE), encoding="utf-8")
    assert lucius._is_unmodified_auto_seed(seed) is True


def test_operator_edited_prompt_is_used(lucius, tmp_path):
    edited = tmp_path / "lucius.md"
    edited.write_text(_TEMPLATE + "\n\nMy override: always write tests first.\n", encoding="utf-8")
    assert lucius._is_unmodified_auto_seed(edited) is False


def test_missing_file_is_not_auto_seed(lucius, tmp_path):
    assert lucius._is_unmodified_auto_seed(tmp_path / "nope.md") is False


def test_lucius_issue_link_lines_distinguish_complete_and_wip_work(lucius):
    assert lucius.issue_closing_line(42) == "Closes #42"
    assert lucius.issue_reference_line(42) == "Issue: #42"


def test_operator_prompt_guidance_skips_untouched_seed_end_to_end(lucius):
    # Drive the real injection path: PROMPT_PATH points at ALFRED_HOME/prompts.
    lucius.PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lucius.PROMPT_PATH.write_text(_TEMPLATE, encoding="utf-8")
    out = lucius._operator_prompt_guidance("myorg/api", {"number": 1}, Path("/tmp/wt"), "feat/x")
    assert out == ""


def test_operator_prompt_guidance_injects_edited_prompt_end_to_end(lucius):
    lucius.PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lucius.PROMPT_PATH.write_text("# Operator guidance\nShip small, reversible changes.\n", "utf-8")
    out = lucius._operator_prompt_guidance("myorg/api", {"number": 1}, Path("/tmp/wt"), "feat/x")
    assert "Ship small, reversible changes." in out


def test_every_shipped_template_carries_the_marker():
    templates = sorted(_PROMPTS.glob("*.md"))
    assert templates, "expected seeded prompt templates"
    for tpl in templates:
        first = tpl.read_text(encoding="utf-8").splitlines()[0]
        assert "alfred:auto-seed" in first, f"{tpl.name} missing auto-seed marker"


if __name__ == "__main__":  # pragma: no cover
    import subprocess

    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-v"]))
