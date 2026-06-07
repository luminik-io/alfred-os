"""Regression guards for the planner prompt's runtime-critical rules."""

from __future__ import annotations

from pathlib import Path

PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "planner.md"


def test_planner_prompt_requires_closed_shipped_dedupe() -> None:
    """Issue #199: the planner must dedupe against shipped work, not only
    currently open issues."""
    text = PROMPT.read_text(encoding="utf-8")
    assert "Closed/shipped issue sweep" in text
    assert "--state closed" in text
    assert "closed_shipped_matches" in text
    assert "re-grep current code after checkout sync" in text
    assert "do not refile" in text.lower()
