from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from planning_assistant import (  # noqa: E402
    apply_repository_scope_feedback,
    build_refiner_prompt,
    refine_issue_draft,
    render_development_spec,
    render_operator_amendments,
    render_operator_feedback_ack,
)
from spec_helper import IssueDraft  # noqa: E402


def _draft() -> IssueDraft:
    return IssueDraft(
        title="Add Slack plan revision flow",
        problem=(
            "Operators and teammates need a way to correct Alfred plans before implementation "
            "so the wrong workflow is not developed."
        ),
        user="Repo owner or teammate",
        current_behavior="Batman posts a plan and waits for emoji approval.",
        desired_behavior=(
            "Batman keeps implementation paused while plan questions are open "
            "and passes approved feedback into each child issue."
        ),
        repos=["luminik-io/alfred-os", "example-org/web"],
        acceptance_criteria=["Slack plan messages say how to reply with changes."],
        test_plan="Run Batman lifecycle tests and inspect the local planning page.",
        out_of_scope="No hosted multi-tenant UI.",
    )


def test_refine_issue_draft_applies_structured_chat_commands() -> None:
    result = refine_issue_draft(
        _draft(),
        [
            "acceptance: Replies with unresolved questions keep the plan in scope mode.\n"
            "test: Add unit coverage for Slack thread feedback parsing.\n"
            "remove repo: example-org/web\n"
            "question: Should the operator approve after edits or request a new plan?"
        ],
    )

    assert result.draft.repos == ["luminik-io/alfred-os"]
    assert "Replies with unresolved questions" in result.draft.acceptance_criteria[-1]
    assert "Slack thread feedback parsing" in result.draft.test_plan
    assert "Should the operator approve" in result.draft.open_questions
    assert result.readiness.ok is True
    assert "Add acceptance criterion" in result.amendments[0]
    assert any(
        "Resolve or explicitly accept the open questions" in item for item in result.questions
    )
    assert "## Implementation Guardrails" in result.spec_body


def test_refine_issue_draft_preserves_mixed_freeform_notes() -> None:
    result = refine_issue_draft(
        _draft(),
        [
            "acceptance: the plan can be revised before approval\n"
            "Use friendlier language for non-technical teammates."
        ],
    )

    assert "the plan can be revised" in result.draft.acceptance_criteria[-1]
    assert "Operator note: Use friendlier language" in result.draft.open_questions
    assert any(
        "Add acceptance criterion: the plan can be revised" in item for item in result.amendments
    )
    assert (
        "Capture operator note: Use friendlier language for non-technical teammates."
        in result.amendments
    )


def test_refine_issue_draft_handles_plural_repo_commands() -> None:
    result = refine_issue_draft(
        _draft(),
        ["add repos: example-org/api, example-org/mobile\nremove repos: example-org/web"],
    )

    assert result.draft.repos == [
        "luminik-io/alfred-os",
        "example-org/api",
        "example-org/mobile",
    ]
    assert "s: example-org" not in " ".join(result.draft.repos)


def test_repository_scope_feedback_updates_execution_repos() -> None:
    repos = apply_repository_scope_feedback(
        ["example-org/api", "example-org/web"],
        ["remove repo: web\nadd repo: mobile"],
        default_org="example-org",
    )

    assert repos == ("example-org/api", "example-org/mobile")


def test_refine_issue_draft_accepts_injected_refiner_patch() -> None:
    def fake_refiner(draft: IssueDraft, messages: tuple[str, ...]) -> dict:
        assert messages == ("Make the title friendlier.",)
        return {
            "title": "Guide teammates through Slack plan edits",
            "acceptance_criteria": [
                *draft.acceptance_criteria,
                "The planning assistant rewrites vague operator notes into reviewable scope.",
            ],
        }

    result = refine_issue_draft(_draft(), ["Make the title friendlier."], refiner=fake_refiner)

    assert result.draft.title == "Guide teammates through Slack plan edits"
    assert "rewrites vague operator notes" in result.draft.acceptance_criteria[-1]


def test_render_operator_amendments_includes_interpretation() -> None:
    block = render_operator_amendments(
        [
            "acceptance: the PR body links to the original GitHub issue",
            "Use simpler copy for teammates.",
        ]
    )

    assert "## Operator Slack Amendments" in block
    assert "the PR body links" in block
    assert "Planning Assistant Interpretation" in block
    assert "Capture operator note: Use simpler copy" in block
    assert "What problem is the user facing today?" not in block
    assert "What should Alfred call this work?" not in block


def test_render_operator_amendments_only_lists_explicit_questions() -> None:
    block = render_operator_amendments(
        [
            "acceptance: the PR body links to the original GitHub issue\n"
            "question: Should the operator approve after edits?"
        ]
    )

    assert "### Follow-up Questions" in block
    assert "Should the operator approve after edits?" in block
    assert "Which repository or repositories should Alfred touch?" not in block


def test_render_operator_feedback_ack_is_concise_for_slack() -> None:
    block = render_operator_feedback_ack(
        [
            "acceptance: the Slack thread acknowledges plan edits\n"
            "question: Should we keep this limited to Batman?"
        ]
    )

    assert "[ALFRED-PLAN-FEEDBACK]" in block
    assert "Add acceptance criterion" in block
    assert "Should we keep this limited to Batman?" in block
    assert "Reply with more changes" in block


def test_development_spec_and_refiner_prompt_are_useful() -> None:
    spec = render_development_spec(_draft())
    prompt = build_refiner_prompt(_draft(), ["acceptance: include Slack examples"])

    assert "## Repository Scope" in spec
    assert "luminik-io/alfred-os" in spec
    assert "Return JSON only" in prompt
    assert "operator_messages" in prompt
