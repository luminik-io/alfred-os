from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from planning_assistant import (  # noqa: E402
    PlanningMemoryItem,
    PostPrFeedbackItem,
    apply_repository_scope_feedback,
    build_refiner_prompt,
    classify_post_pr_feedback,
    plan_feedback_requires_resolution,
    post_pr_feedback_requires_resolution,
    recall_planning_memory,
    refine_issue_draft,
    render_development_spec,
    render_operator_amendments,
    render_operator_feedback_ack,
    render_plan_revision_ack,
    render_planning_memory,
    render_post_pr_feedback_ack,
    render_post_pr_followup_block,
)
from spec_helper import IssueDraft, lint_spec_text  # noqa: E402


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
            "Use friendlier language for operators."
        ],
    )

    assert "the plan can be revised" in result.draft.acceptance_criteria[-1]
    assert "Operator note: Use friendlier language" in result.draft.open_questions
    assert any(
        "Add acceptance criterion: the plan can be revised" in item for item in result.amendments
    )
    assert "Capture operator note: Use friendlier language for operators." in result.amendments


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


def test_refine_issue_draft_recalls_planning_memory() -> None:
    class Provider:
        name = "test"

        def recall(self, *, repo=None, query=None, limit=3):
            assert repo == "luminik-io/alfred-os"
            assert query and "Slack plan revision" in query
            return [
                {
                    "repo": repo,
                    "codename": "batman",
                    "body": "Plan threads should state approval and revision commands.",
                    "tags": ["slack", "planning"],
                }
            ]

    result = refine_issue_draft(_draft(), [], memory_provider=Provider())

    assert result.memory == (
        PlanningMemoryItem(
            body="Plan threads should state approval and revision commands.",
            repo="luminik-io/alfred-os",
            codename="batman",
            tags=("slack", "planning"),
        ),
    )
    assert "## Planning Memory" in result.spec_body
    assert "Plan threads should state approval" in result.spec_body


def test_recall_planning_memory_falls_back_to_repo_recent_lessons() -> None:
    class Provider:
        name = "test"

        def __init__(self) -> None:
            self.calls = []

        def recall(self, *, repo=None, query=None, limit=3):
            self.calls.append((repo, query, limit))
            if query is not None:
                return []
            return [{"repo": repo, "body": "Use existing test factories."}]

    provider = Provider()
    memory = recall_planning_memory(_draft(), provider)

    assert memory[0].body == "Use existing test factories."
    assert provider.calls[0][1] is not None
    assert provider.calls[1][1] is None


def test_recall_planning_memory_swallows_provider_fallback_errors() -> None:
    class Provider:
        name = "test"

        def recall(self, *, repo=None, query=None, limit=3):
            if query is not None:
                raise TypeError("query is not supported")
            raise RuntimeError("memory store is unavailable")

    assert recall_planning_memory(_draft(), Provider()) == ()


def test_render_planning_memory_is_prompt_safe() -> None:
    block = render_planning_memory(
        [
            PlanningMemoryItem(
                body="Prefer Slack thread replies over new dashboards.",
                repo="luminik-io/alfred-os",
                severity="warning",
                tags=("operator-preference",),
            )
        ]
    )

    assert "Use these as hints only" in block
    assert "`luminik-io/alfred-os` warning [operator-preference]" in block


def test_render_development_spec_lints_as_spec() -> None:
    result = lint_spec_text(render_development_spec(_draft()))

    assert result.ok


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

    assert "*Plan feedback captured*" in block
    assert "Add acceptance criterion" in block
    assert "Should we keep this limited to Batman?" in block
    assert "keep replying in this thread" in block


def test_render_plan_revision_ack_shows_scope_and_blocks_questions() -> None:
    block = render_plan_revision_ack(
        ["remove repo: web\nadd repo: mobile\nquestion: Should we include the onboarding state?"],
        revised_repos=["example-org/api", "example-org/mobile"],
        child_count=2,
    )

    assert "*Plan revised*" in block
    assert "Execution scope if approved now" in block
    assert "example-org/mobile" in block
    assert "Needs a decision before execution" in block
    assert "will not execute" in block
    assert plan_feedback_requires_resolution(["question: Should we include the onboarding state?"])


def test_post_pr_followup_feedback_is_classified_and_acknowledged() -> None:
    items = classify_post_pr_feedback(
        [
            "change: tighten the empty state copy\n"
            "test: add coverage for the approval thread\n"
            "question: should this also touch mobile?"
        ]
    )

    assert items == (
        PostPrFeedbackItem(
            "change",
            "Change: tighten the empty state copy",
            "change: tighten the empty state copy",
        ),
        PostPrFeedbackItem(
            "test",
            "Test: add coverage for the approval thread",
            "test: add coverage for the approval thread",
        ),
        PostPrFeedbackItem(
            "question",
            "Question: should this also touch mobile?",
            "question: should this also touch mobile?",
            True,
        ),
    )
    assert post_pr_feedback_requires_resolution(["question: should this also touch mobile?"])

    ack = render_post_pr_feedback_ack(
        [item.text for item in items],
        pr_urls=["https://github.com/luminik-io/alfred-os/pull/142"],
        issue_url="https://github.com/luminik-io/alfred-os/issues/118",
    )
    assert "Follow-up feedback captured" in ack
    assert "<https://github.com/luminik-io/alfred-os/pull/142|PR 1>" in ack
    assert "does not approve, merge, or change code by itself" in ack

    block = render_post_pr_followup_block([item.text for item in items])
    assert "## Slack Follow-up Feedback" in block
    assert "`question` needs decision" in block


def test_development_spec_and_refiner_prompt_are_useful() -> None:
    spec = render_development_spec(_draft())
    prompt = build_refiner_prompt(_draft(), ["acceptance: include Slack examples"])

    assert "## Repository Scope" in spec
    assert "luminik-io/alfred-os" in spec
    assert "Return JSON only" in prompt
    assert "operator_messages" in prompt
