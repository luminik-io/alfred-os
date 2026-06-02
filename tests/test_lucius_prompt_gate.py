"""The seeded prompt-template gate: an unmodified auto-seed must not be injected.

alfred-init seeds starter prompt templates carrying an ``alfred:auto-seed``
marker. bin/lucius.py defers to its in-code guidance until the operator edits
the file (which removes the marker), so a stale starter never overrides newer
in-code guidance.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
_LIB = Path(__file__).resolve().parent.parent / "lib"


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


def test_unmodified_auto_seed_is_skipped(lucius, tmp_path):
    seed = tmp_path / "lucius.md"
    seed.write_text(
        "<!-- alfred:auto-seed v1 (delete this line to activate this file as operator guidance) -->\n"
        "<!--\n  Role: feature-dev\n-->\nDo the thing.\n",
        encoding="utf-8",
    )
    assert lucius._is_unmodified_auto_seed(seed) is True


def test_operator_edited_prompt_is_used(lucius, tmp_path):
    edited = tmp_path / "lucius.md"
    edited.write_text("# My own Lucius guidance\nAlways write tests first.\n", encoding="utf-8")
    assert lucius._is_unmodified_auto_seed(edited) is False


def test_missing_file_is_not_auto_seed(lucius, tmp_path):
    assert lucius._is_unmodified_auto_seed(tmp_path / "nope.md") is False


def test_every_shipped_template_carries_the_marker():
    # Guards the gate: if a template loses its marker it would inject as if
    # operator-authored on a fresh install.
    templates = sorted((Path(__file__).resolve().parent.parent / "prompts").glob("*.md"))
    assert templates, "expected seeded prompt templates"
    for tpl in templates:
        first = tpl.read_text(encoding="utf-8").splitlines()[0]
        assert "alfred:auto-seed" in first, f"{tpl.name} missing auto-seed marker"
