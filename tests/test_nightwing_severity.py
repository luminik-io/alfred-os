from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "bin"
LIB_DIR = ROOT / "lib"


@pytest.fixture
def nightwing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    sys.path.insert(0, str(LIB_DIR))
    module_name = "nightwing_under_test"
    spec = importlib.util.spec_from_file_location(module_name, BIN_DIR / "nightwing.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(LIB_DIR))


def test_full_review_with_p0_none_and_p1_secret_context_is_p1(nightwing) -> None:
    body = """Rasalghul - review

## Blockers (P0)
- None.

## Should fix before merge (P1)
- api/src/main/kotlin/Foo.kt:45 - Fix the race so stale delivery cannot
  resurrect a cancelled request and leave secret purge state inconsistent.

Ship-ready: no
"""

    assert nightwing.comment_severity(body) == "P1"
    assert nightwing.SECURITY_KEYWORDS.search(body)


def test_p0_security_comment_still_requires_manual_gate(nightwing) -> None:
    body = "Rasalghul P0: token exposure lets one tenant read another tenant's data."

    assert nightwing.comment_severity(body) == "P0"
    assert nightwing.SECURITY_KEYWORDS.search(body)
