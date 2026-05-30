"""Intake profiles: how the planning assistant talks to a human.

The planning assistant always produces the same structured ``IssueDraft``
and readiness verdict. *How* it talks to the person describing the work is
a separate concern, selected by the ``ALFRED_INTAKE_PROFILE`` environment
variable:

* unset / ``technical`` (default): the original operator-facing behavior.
  The refiner persona talks in spec/acceptance/repo/readiness terms and the
  user-facing summary reports amendment counts and readiness state. This
  path is intentionally byte-for-byte identical to the pre-profile module
  so existing operators, Slack threads, and the dashboard are unchanged.

* ``plain``: a non-technical front door. A designer (or anyone) can describe
  work in plain language. The refiner persona asks at most one or two plain
  clarifying questions and never uses jargon. The user-facing summary is a
  short "Here's what I'll do ... OK to go ahead?" plan that hides readiness
  scores and technical fields and frames approval around reviewing a
  preview, not PRs or diffs.

This is a strategy seam, not a fork: only the refiner *prompt persona* and
the *user-facing rendering* are overridden. The structured draft, readiness
scoring, issue body, spec body, and downstream bridge/fleet are identical in
both modes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from planning_assistant import PlanningAssistantResult
    from spec_helper import IssueDraft, IssueReadinessResult

ENV_INTAKE_PROFILE = "ALFRED_INTAKE_PROFILE"

# Words the plain-mode user-facing surface must never show a non-technical
# person. Kept here so the rendering and its tests agree on one list.
PLAIN_BANNED_WORDS: tuple[str, ...] = (
    "spec",
    "acceptance",
    "acceptance criteria",
    "readiness",
    "repo",
    "repository",
    "pr",
    "pull request",
    "diff",
    "merge gate",
    "issue body",
)


class IntakeProfile(Protocol):
    """Strategy for how the planning assistant addresses a human.

    Implementations only customize the conversational surface. They never
    change the structured draft, readiness scoring, or downstream files.
    """

    name: str

    def refiner_prompt(self, draft: IssueDraft, messages: Iterable[str]) -> str:
        """Return the prompt text for the optional LLM refiner."""

    def render_user_summary(
        self,
        result: PlanningAssistantResult,
    ) -> str:
        """Return the short user-facing summary line(s) for this result."""


def _draft_payload(draft: IssueDraft) -> dict[str, object]:
    return {
        "draft": {
            "title": draft.title,
            "problem": draft.problem,
            "user": draft.user,
            "current_behavior": draft.current_behavior,
            "desired_behavior": draft.desired_behavior,
            "repos": draft.repos,
            "acceptance_criteria": draft.acceptance_criteria,
            "test_plan": draft.test_plan,
            "out_of_scope": draft.out_of_scope,
            "rollout": draft.rollout,
            "open_questions": draft.open_questions,
        },
    }


class TechnicalIntakeProfile:
    """Default operator-facing intake. Preserves the original behavior."""

    name = "technical"

    def refiner_prompt(self, draft: IssueDraft, messages: Iterable[str]) -> str:
        payload = _draft_payload(draft)
        payload["operator_messages"] = list(messages)
        return (
            "You are Alfred's planning assistant. Tighten the draft so technical and "
            "non-technical operators can describe work safely before an autonomous "
            "engineering agent starts.\n\n"
            "Return JSON only with any of these keys: title, problem, user, "
            "current_behavior, desired_behavior, repos, acceptance_criteria, test_plan, "
            "out_of_scope, rollout, open_questions. Use arrays for repos and "
            "acceptance_criteria. Keep scope narrow, ask questions when uncertain, "
            "and do not invent repository names.\n\n"
            f"{json.dumps(payload, indent=2)}"
        )

    def render_user_summary(self, result: PlanningAssistantResult) -> str:
        state = (
            "ready for implementation"
            if result.readiness.ok
            else "needs scope before implementation"
        )
        if result.amendments:
            return f"{len(result.amendments)} amendment(s) applied; draft {state}."
        return f"No structured amendments found; draft {state}."


class PlainIntakeProfile:
    """Non-technical intake for designers and other plain-language users.

    The same structured draft is produced invisibly. The person only ever
    sees a friendly plan and approves an outcome, never code.
    """

    name = "plain"

    def refiner_prompt(self, draft: IssueDraft, messages: Iterable[str]) -> str:
        payload = _draft_payload(draft)
        payload["recent_messages"] = list(messages)
        return (
            "You are a friendly product helper. Someone is describing a change "
            "they want made to an app, in their own words. Your job is to "
            "understand exactly what they want so it can be built for them.\n\n"
            "Talk like a helpful teammate, never like an engineer. Do not use "
            "words like spec, acceptance criteria, repository, readiness, pull "
            "request, or diff. If anything important is unclear, ask at most one "
            'or two short, plain questions, for example "Which screen is this '
            'on?" or "What color did you have in mind?". If it is already '
            "clear enough, do not ask anything.\n\n"
            "Return JSON only. You may fill in any of these keys: title, "
            "problem, user, current_behavior, desired_behavior, repos, "
            "acceptance_criteria, test_plan, out_of_scope, rollout, "
            "open_questions. Put any plain clarifying questions in "
            "open_questions, one per line. Use arrays for repos and "
            "acceptance_criteria. Capture what the person actually said; do not "
            "invent details or guess where the work lives.\n\n"
            f"{json.dumps(payload, indent=2)}"
        )

    def render_user_summary(self, result: PlanningAssistantResult) -> str:
        draft = result.draft
        questions = _plain_open_questions(draft, result.readiness)
        lines = ["Here's what I'll do:"]
        plan = _plain_plan_line(draft)
        lines.append(f"- {plan}")
        if questions:
            lines.append("")
            lines.append("First, a quick check:")
            lines.extend(f"- {question}" for question in questions[:2])
            lines.append("")
            lines.append(
                "Once you let me know, I'll get started and show you a preview to look over."
            )
        else:
            lines.append("")
            lines.append(
                "I'll put this together and show you a preview to look over before anything goes live."
            )
            lines.append("")
            lines.append("OK to go ahead?")
        return "\n".join(lines)


def _plain_plan_line(draft: IssueDraft) -> str:
    """One plain sentence describing the work, with no technical fields.

    The candidate sentences come from free text a person (or the refiner)
    wrote, so each is screened against ``PLAIN_BANNED_WORDS`` before it is
    shown. A candidate carrying jargon is skipped in favor of the next
    source, and the final fallback is always jargon-free.
    """

    desired = _first_sentence(draft.desired_behavior)
    if desired and not _has_jargon(desired):
        return desired
    problem = _first_sentence(draft.problem)
    if problem and not _has_jargon(problem):
        return f"Sort out {problem[0].lower()}{problem[1:]}"
    title = draft.title.strip()
    if title and not _has_jargon(title):
        return title
    return "Make the change you described."


def _has_jargon(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in PLAIN_BANNED_WORDS)


def _plain_open_questions(
    draft: IssueDraft,
    readiness: IssueReadinessResult,
) -> list[str]:
    """Plain-language clarifying questions, with jargon translated away.

    The structured readiness questions are operator-oriented (they mention
    repos, PRs, acceptance criteria). In plain mode we surface friendlier
    equivalents and any plain questions the person or refiner already left
    in open_questions.
    """

    out: list[str] = []
    seen: set[str] = set()

    def _add(question: str) -> None:
        cleaned = question.strip()
        # Skip empties and anything carrying jargon back to a plain user.
        if not cleaned or _has_jargon(cleaned):
            return
        key = cleaned.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(cleaned)

    for raw in _open_question_lines(draft.open_questions):
        _add(raw)

    for finding in readiness.findings:
        if finding.severity != "error":
            continue
        friendly = _PLAIN_QUESTION_FOR_FINDING.get(finding.code)
        if friendly:
            _add(friendly)

    return out


# Friendly, jargon-free stand-ins for the operator-facing readiness gaps.
# Only blocking gaps map to a question; warnings stay invisible in plain mode.
_PLAIN_QUESTION_FOR_FINDING: dict[str, str] = {
    "missing_title": "What should I call this?",
    "missing_problem": "What's not working well right now?",
    "missing_desired_behavior": "What should it look like or do when it's done?",
    "missing_repo_scope": "Which part of the product is this for?",
    "missing_acceptance_criteria": "How will you know it's right when you see the preview?",
    "missing_test_plan": "Anything specific you'd like me to double-check before showing you?",
    "open_questions_unresolved": "Want me to go ahead, or is there still something to decide?",
}


def _open_question_lines(value: str) -> list[str]:
    cleaned = (value or "").strip()
    if not cleaned or cleaned.lower().strip(".") in {"none", "n/a", "accepted as risk"}:
        return []
    lines: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip().lstrip("-*").strip()
        # Drop operator-note bookkeeping; it is not a user question.
        if not line or line.lower().startswith("operator note:"):
            continue
        lines.append(line)
    return lines


def _first_sentence(value: str) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        return ""
    for terminator in (". ", "! ", "? "):
        index = cleaned.find(terminator)
        if index != -1:
            return cleaned[: index + 1].strip()
    return cleaned


def active_intake_profile(env: dict[str, str] | None = None) -> IntakeProfile:
    """Return the intake profile selected by ``ALFRED_INTAKE_PROFILE``.

    Unset or any unrecognized value falls back to the technical default, so
    a typo never silently downgrades an operator into plain mode.
    """

    source = os.environ if env is None else env
    raw = (source.get(ENV_INTAKE_PROFILE) or "").strip().lower()
    if raw == "plain":
        return PlainIntakeProfile()
    return TechnicalIntakeProfile()
