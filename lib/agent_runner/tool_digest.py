"""Digest verbose tool output into structured signal before it enters context.

Test logs and diffs are mostly noise: thousands of "PASSED" lines, hunk
headers, unchanged context. Feeding all of it back into an agent's context
window burns tokens and buries the part that actually matters (which tests
failed, which files changed, the first real error). This module distills that
verbose output into a compact, structured summary the runner can inject instead
of the raw blob.

Honest scope: regex + line heuristics, stdlib only, no LLM call. It recognizes
the common shapes (pytest, generic test runners, unified diffs) and otherwise
falls back to a head/tail excerpt. It is lossy on purpose; the agent can always
re-run the tool for the raw output when it needs detail.

This is a building block. Wiring it into the firing loop (digesting Bash tool
results before they round-trip into the next turn) is a follow-up; landing the
helper + its tests first keeps the diff tight and reviewable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["ToolDigest", "digest_diff", "digest_test_log", "digest_tool_output"]

# pytest short-summary lines, e.g. "FAILED tests/test_x.py::test_y - AssertionError"
_PYTEST_OUTCOME_RE = re.compile(
    r"^(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(?P<node>\S+)",
)
# pytest result tail, e.g. "===== 3 failed, 12 passed, 1 skipped in 4.21s ====="
_PYTEST_TAIL_RE = re.compile(r"=+\s*(?P<body>[\d].*?(?:passed|failed|error|skipped).*?)\s*=+\s*$")
# Generic error / traceback markers worth surfacing first.
_ERROR_MARKERS = re.compile(
    r"\b(Error|Exception|Traceback|assert|FAIL|panic|SyntaxError|undefined)\b",
    re.IGNORECASE,
)
# Unified-diff file headers.
_DIFF_FILE_RE = re.compile(r"^\+\+\+\s+b/(?P<path>.+)$")
_DIFF_OLD_RE = re.compile(r"^---\s+a/(?P<path>.+)$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


@dataclass
class ToolDigest:
    """Structured, compact summary of one tool's verbose output."""

    kind: str  # "test", "diff", or "generic"
    summary: str  # one-line headline
    failures: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    excerpt: str = ""  # short raw excerpt for the agent's eyes

    def render(self) -> str:
        """Render the digest as a compact prompt-ready block."""
        lines = [self.summary]
        if self.failures:
            lines.append("Failures:")
            lines.extend(f"  - {item}" for item in self.failures)
        if self.files:
            lines.append("Files changed:")
            lines.extend(f"  - {item}" for item in self.files)
        if self.excerpt:
            lines.append("Excerpt:")
            lines.append(self.excerpt)
        return "\n".join(lines).strip()


def _excerpt(text: str, *, head: int = 8, tail: int = 8, max_chars: int = 1200) -> str:
    """Head + tail excerpt of ``text``, elided in the middle."""
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    if len(raw_lines) <= head + tail:
        chosen = raw_lines
    else:
        chosen = [
            *raw_lines[:head],
            f"... ({len(raw_lines) - head - tail} lines elided) ...",
            *raw_lines[-tail:],
        ]
    out = "\n".join(chosen)
    if len(out) > max_chars:
        out = out[: max_chars - 3].rstrip() + "..."
    return out


def digest_test_log(text: str, *, max_failures: int = 20) -> ToolDigest:
    """Distill a test runner log into pass/fail counts and failing nodes."""
    failures: list[str] = []
    tail: str = ""
    error_lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _PYTEST_OUTCOME_RE.match(stripped)
        if m and m.group("outcome") in {"FAILED", "ERROR"}:
            if len(failures) < max_failures:
                failures.append(stripped)
            continue
        tm = _PYTEST_TAIL_RE.match(stripped)
        if tm:
            tail = tm.group("body").strip()
            continue
        if len(error_lines) < 6 and _ERROR_MARKERS.search(stripped):
            error_lines.append(stripped)

    if tail:
        summary = f"Test run: {tail}"
    elif failures:
        summary = f"Test run: {len(failures)} failing node(s) detected"
    else:
        summary = "Test run: no structured outcome found"
    excerpt = "\n".join(error_lines) if error_lines else _excerpt(text)
    return ToolDigest(kind="test", summary=summary, failures=failures, excerpt=excerpt)


def digest_diff(text: str, *, max_files: int = 40) -> ToolDigest:
    """Distill a unified diff into the set of touched files and a line delta."""
    files: list[str] = []
    seen: set[str] = set()
    added = 0
    removed = 0
    for line in (text or "").splitlines():
        gm = _DIFF_GIT_RE.match(line)
        if gm:
            path = gm.group("b")
            if path not in seen:
                seen.add(path)
                if len(files) < max_files:
                    files.append(path)
            continue
        fm = _DIFF_FILE_RE.match(line) or _DIFF_OLD_RE.match(line)
        if fm:
            path = fm.group("path")
            if path != "/dev/null" and path not in seen:
                seen.add(path)
                if len(files) < max_files:
                    files.append(path)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    summary = f"Diff: {len(seen)} file(s) changed, +{added}/-{removed} lines"
    return ToolDigest(kind="diff", summary=summary, files=files, excerpt="")


def _looks_like_diff(text: str) -> bool:
    return bool(_DIFF_GIT_RE.search(text or "")) or "\n--- a/" in f"\n{text or ''}"


def _looks_like_test_log(text: str) -> bool:
    if _PYTEST_TAIL_RE.search(text or ""):
        return True
    return any(_PYTEST_OUTCOME_RE.match(ln.strip()) for ln in (text or "").splitlines())


def digest_tool_output(text: str, *, kind: str | None = None) -> ToolDigest:
    """Auto-detect and digest verbose tool output.

    ``kind`` may force ``"test"``, ``"diff"``, or ``"generic"``; otherwise the
    shape is sniffed. Generic output gets a head/tail excerpt so nothing is
    fully dropped.
    """
    text = text or ""
    if kind == "test" or (kind is None and _looks_like_test_log(text)):
        return digest_test_log(text)
    if kind == "diff" or (kind is None and _looks_like_diff(text)):
        return digest_diff(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    summary = f"Output: {len(lines)} non-empty line(s)"
    return ToolDigest(kind="generic", summary=summary, excerpt=_excerpt(text))
