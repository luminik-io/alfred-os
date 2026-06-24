#!/usr/bin/env python3
"""The tool-output digest distills verbose test logs and diffs into signal."""

from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

import pytest  # noqa: E402
from agent_runner.tool_digest import (  # noqa: E402
    digest_diff,
    digest_stream_tool_result,
    digest_test_log,
    digest_tool_output,
    tool_digest_enabled,
    tool_digest_min_chars,
)


def test_digest_test_log_extracts_failures_and_tail() -> None:
    log = """
collecting ...
tests/test_a.py::test_one PASSED
FAILED tests/test_b.py::test_two - AssertionError: 1 != 2
PASSED tests/test_c.py::test_three
ERROR tests/test_d.py::test_four - ImportError
===== 1 failed, 2 passed, 1 error in 3.10s =====
"""
    digest = digest_test_log(log)
    assert digest.kind == "test"
    assert "1 failed" in digest.summary
    assert any("test_b.py::test_two" in f for f in digest.failures)
    assert any("test_d.py::test_four" in f for f in digest.failures)
    # PASSED nodes are noise and must not appear as failures.
    assert not any("PASSED" in f for f in digest.failures)


def test_digest_diff_lists_files_and_counts() -> None:
    diff = """diff --git a/lib/foo.py b/lib/foo.py
index 111..222 100644
--- a/lib/foo.py
+++ b/lib/foo.py
@@ -1,3 +1,4 @@
 unchanged
-removed line
+added line one
+added line two
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""
    digest = digest_diff(diff)
    assert digest.kind == "diff"
    assert "lib/foo.py" in digest.files
    assert "README.md" in digest.files
    assert "+3" in digest.summary  # three added lines total
    assert "-2" in digest.summary  # two removed lines total


def test_digest_tool_output_autodetects_diff() -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n+added\n"
    digest = digest_tool_output(diff)
    assert digest.kind == "diff"


def test_digest_tool_output_autodetects_test_log() -> None:
    log = "FAILED tests/test_z.py::test_q - ValueError\n=== 1 failed in 0.1s ===\n"
    digest = digest_tool_output(log)
    assert digest.kind == "test"


def test_digest_tool_output_generic_excerpts() -> None:
    text = "\n".join(f"line {i}" for i in range(200))
    digest = digest_tool_output(text)
    assert digest.kind == "generic"
    assert "elided" in digest.excerpt
    assert len(digest.render()) < len(text)


def test_render_is_compact_and_structured() -> None:
    log = "FAILED tests/test_z.py::test_q - ValueError\n=== 1 failed in 0.1s ===\n"
    rendered = digest_tool_output(log).render()
    assert "Failures:" in rendered
    assert "test_z.py::test_q" in rendered


# --------------------------------------------------------------------------
# Firing-loop wiring (digest_stream_tool_result)
# --------------------------------------------------------------------------


def _big_test_log(n: int = 400) -> str:
    passed = "\n".join(f"PASSED tests/test_mod.py::test_{i}" for i in range(n))
    return (
        f"{passed}\n"
        "FAILED tests/test_mod.py::test_boom - AssertionError: nope\n"
        "=== 1 failed, "
        f"{n} passed in 12.3s ===\n"
    )


def _big_diff(n: int = 400) -> str:
    head = "diff --git a/lib/foo.py b/lib/foo.py\n--- a/lib/foo.py\n+++ b/lib/foo.py\n"
    body = "\n".join(f"+added line {i}" for i in range(n))
    return head + body + "\n"


def test_stream_result_digests_large_test_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_TOOL_DIGEST", raising=False)
    monkeypatch.delenv("ALFRED_TOOL_DIGEST_MIN_CHARS", raising=False)
    raw = _big_test_log()
    assert len(raw) >= tool_digest_min_chars()
    out = digest_stream_tool_result(raw)
    assert out != raw
    assert len(out) < len(raw)
    assert "digested" in out
    # Signal survives: the failing node and the tail counts.
    assert "test_boom" in out
    assert "failed" in out


def test_stream_result_digests_large_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_TOOL_DIGEST", raising=False)
    monkeypatch.delenv("ALFRED_TOOL_DIGEST_MIN_CHARS", raising=False)
    raw = _big_diff()
    out = digest_stream_tool_result(raw)
    assert out != raw
    assert "lib/foo.py" in out
    assert "file(s) changed" in out


def test_stream_result_passes_small_output_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_TOOL_DIGEST", raising=False)
    monkeypatch.delenv("ALFRED_TOOL_DIGEST_MIN_CHARS", raising=False)
    small = "FAILED tests/test_z.py::test_q - ValueError\n=== 1 failed in 0.1s ===\n"
    assert len(small) < tool_digest_min_chars()
    assert digest_stream_tool_result(small) == small


def test_stream_result_opt_out_returns_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_TOOL_DIGEST", "0")
    raw = _big_test_log()
    assert tool_digest_enabled() is False
    assert digest_stream_tool_result(raw) == raw


def test_min_chars_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_TOOL_DIGEST", raising=False)
    monkeypatch.setenv("ALFRED_TOOL_DIGEST_MIN_CHARS", "10")
    # A short blob that would normally pass through is now over the floor.
    text = "FAILED tests/test_z.py::test_q - ValueError\n=== 1 failed in 0.1s ===\n"
    assert tool_digest_min_chars() == 10
    out = digest_stream_tool_result(text)
    assert "digested" in out


def test_min_chars_bad_value_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_TOOL_DIGEST_MIN_CHARS", "not-a-number")
    assert tool_digest_min_chars() == 2000


def test_empty_output_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_TOOL_DIGEST", raising=False)
    assert digest_stream_tool_result("") == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
