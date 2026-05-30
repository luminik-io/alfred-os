"""Tests for the plain-language intake profile.

These assert two things at once for the non-technical front door:

* The user-facing summary is jargon-free (no "spec", "acceptance",
  "readiness", "PR", etc.).
* The *same* structured ``IssueDraft`` and readiness verdict are still
  produced, so the downstream bridge and fleet are unaffected.

Technical mode must stay byte-for-byte identical to the pre-profile module.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import planning_assistant  # noqa: E402
from intake_profiles import (  # noqa: E402
    ENV_INTAKE_PROFILE,
    PLAIN_BANNED_WORDS,
    PlainIntakeProfile,
    TechnicalIntakeProfile,
    active_intake_profile,
)
from planning_assistant import (  # noqa: E402
    build_refiner_prompt,
    refine_issue_draft,
    render_user_facing_summary,
)
from spec_helper import IssueDraft  # noqa: E402

# The task names these four explicitly; the module's PLAIN_BANNED_WORDS is a
# superset. Assert against the superset so rendering and tests share one list.
REQUIRED_BANNED = ("spec", "acceptance", "readiness", "pr")
BANNED_WORDS = tuple(dict.fromkeys((*REQUIRED_BANNED, *PLAIN_BANNED_WORDS)))


def _ready_draft() -> IssueDraft:
    return IssueDraft(
        title="Make the signup button green",
        problem=(
            "The signup button on the welcome screen is grey and new visitors "
            "miss it, so fewer of them start an account."
        ),
        user="A new visitor on the welcome screen",
        desired_behavior=(
            "The signup button on the welcome screen should be green so it "
            "clearly stands out from the rest of the page."
        ),
        repos=["acme-org/web"],
        acceptance_criteria=["The signup button is green on the welcome screen."],
        test_plan="Open the welcome screen and confirm the button is now green.",
        out_of_scope="No other color or copy changes.",
    )


def _vague_draft() -> IssueDraft:
    return IssueDraft(title="change the thing")


def _assert_no_jargon(text: str) -> None:
    lowered = text.lower()
    for word in BANNED_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", lowered), (
            f"banned word {word!r} leaked into plain user-facing text:\n{text}"
        )


# ---------------------------------------------------------------------------
# Profile selection via env
# ---------------------------------------------------------------------------


def test_active_profile_defaults_to_technical_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(ENV_INTAKE_PROFILE, raising=False)
    assert isinstance(active_intake_profile(), TechnicalIntakeProfile)


def test_active_profile_selects_plain(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    assert isinstance(active_intake_profile(), PlainIntakeProfile)


def test_active_profile_is_case_insensitive_and_trims(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "  PLAIN  ")
    assert isinstance(active_intake_profile(), PlainIntakeProfile)


def test_unknown_profile_value_falls_back_to_technical(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "fancy")
    assert isinstance(active_intake_profile(), TechnicalIntakeProfile)


def test_active_profile_accepts_explicit_env_mapping() -> None:
    assert isinstance(active_intake_profile({}), TechnicalIntakeProfile)
    assert isinstance(active_intake_profile({ENV_INTAKE_PROFILE: "plain"}), PlainIntakeProfile)


# ---------------------------------------------------------------------------
# Technical mode is unchanged (default identical to before the profile seam)
# ---------------------------------------------------------------------------


def test_technical_summary_matches_legacy_text(monkeypatch) -> None:
    monkeypatch.delenv(ENV_INTAKE_PROFILE, raising=False)
    result = refine_issue_draft(_ready_draft(), [])
    assert result.summary == "No structured amendments found; draft ready for implementation."

    amended = refine_issue_draft(_ready_draft(), ["acceptance: also works on mobile"])
    assert amended.summary == "1 amendment(s) applied; draft ready for implementation."


def test_technical_refiner_prompt_is_unchanged(monkeypatch) -> None:
    monkeypatch.delenv(ENV_INTAKE_PROFILE, raising=False)
    prompt = build_refiner_prompt(_ready_draft(), ["make it pop"])
    # The legacy operator-facing persona and the JSON contract are intact.
    assert prompt.startswith("You are Alfred's planning assistant.")
    assert "Return JSON only with any of these keys" in prompt
    assert '"operator_messages"' in prompt
    assert "do not invent repository names" in prompt


# ---------------------------------------------------------------------------
# Plain mode: jargon-free surface, same structured draft
# ---------------------------------------------------------------------------


def test_plain_summary_is_jargon_free_when_ready(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    result = refine_issue_draft(_ready_draft(), [])

    _assert_no_jargon(result.summary)
    assert "Here's what I'll do" in result.summary
    assert "OK to go ahead?" in result.summary
    # Approval is framed around reviewing a preview, not code.
    assert "preview" in result.summary.lower()
    # No readiness score leaks into the user-facing text.
    assert "100" not in result.summary
    assert "/100" not in result.summary


def test_plain_summary_still_produces_valid_structured_draft(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    result = refine_issue_draft(_ready_draft(), [])

    # The technical structure is built invisibly and is unchanged.
    assert result.draft.repos == ["acme-org/web"]
    assert result.draft.acceptance_criteria == ["The signup button is green on the welcome screen."]
    assert result.readiness.ok is True
    assert result.readiness.score == 100
    # The GitHub-ready issue body and spec body are still rendered for the fleet.
    assert "## Acceptance Criteria" in result.issue_body
    assert "## Repository Scope" in result.spec_body
    assert "acme-org/web" in result.issue_body


def test_plain_summary_asks_plain_questions_when_underspecified(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    result = refine_issue_draft(_vague_draft(), [])

    _assert_no_jargon(result.summary)
    # At most two plain clarifying questions are surfaced.
    question_lines = [line for line in result.summary.splitlines() if line.startswith("- ")]
    # one plan line + at most two questions
    assert len(question_lines) <= 3
    assert "quick check" in result.summary.lower()
    # The underlying readiness still knows the draft is not ready.
    assert result.readiness.ok is False
    assert any(f.code == "missing_repo_scope" for f in result.readiness.findings)


def test_required_banned_words_are_covered_by_module_list() -> None:
    lowered = {word.lower() for word in PLAIN_BANNED_WORDS}
    for word in REQUIRED_BANNED:
        assert word in lowered


def test_plain_plan_line_scrubs_jargon_from_free_text(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    # A draft whose desired_behavior text itself carries jargon. The plan line
    # must fall back to a clean source rather than echo the jargon.
    jargony = IssueDraft(
        title="Brighten the welcome screen",
        problem=(
            "New visitors on the welcome screen do not notice the signup button "
            "because it blends into the background."
        ),
        desired_behavior="Update the repo and open a PR so the spec acceptance criteria pass.",
        repos=["acme-org/web"],
        acceptance_criteria=["The signup button stands out on the welcome screen."],
        test_plan="Open the welcome screen and confirm the button stands out.",
    )
    result = refine_issue_draft(jargony, [])

    _assert_no_jargon(result.summary)
    # It fell back to the (clean) problem statement, not the jargon-laden
    # desired_behavior.
    assert "PR" not in result.summary
    assert "welcome screen" in result.summary.lower()


def test_plain_summary_hides_operator_note_bookkeeping(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    # A free-form note lands in open_questions as an "Operator note:" line in
    # the structured draft; the user should never see that bookkeeping.
    result = refine_issue_draft(_ready_draft(), ["please make it feel friendlier"])

    assert "Operator note" not in result.summary
    _assert_no_jargon(result.summary)


def test_plain_refiner_prompt_uses_friendly_persona(monkeypatch) -> None:
    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    prompt = build_refiner_prompt(_ready_draft(), ["make it pop"])

    assert "friendly product helper" in prompt
    assert "one or two short, plain questions" in prompt
    # The persona explicitly forbids jargon to the LLM.
    assert "Do not use" in prompt
    assert "spec" in prompt  # appears only inside the "do not use" instruction
    # Still returns the structured JSON contract so the draft stays buildable.
    assert "Return JSON only" in prompt
    assert "acceptance_criteria" in prompt


def test_render_user_facing_summary_follows_active_profile(monkeypatch) -> None:
    # Render once under technical, then re-render the same result under plain.
    monkeypatch.delenv(ENV_INTAKE_PROFILE, raising=False)
    result = refine_issue_draft(_ready_draft(), [])
    assert "draft ready for implementation" in render_user_facing_summary(result)

    monkeypatch.setenv(ENV_INTAKE_PROFILE, "plain")
    plain = render_user_facing_summary(result)
    assert "Here's what I'll do" in plain
    _assert_no_jargon(plain)


def test_plain_mode_does_not_change_module_default(monkeypatch) -> None:
    # Sanity guard: importing planning_assistant did not bind a profile at
    # import time; selection is per-call so a process can serve both modes.
    monkeypatch.delenv(ENV_INTAKE_PROFILE, raising=False)
    assert not hasattr(planning_assistant, "_INTAKE_PROFILE")
