"""Interactive planning helpers for issue, spec, and Slack amendments.

The module is deliberately split into two layers:

* deterministic parsing for obvious operator commands such as
  ``acceptance: ...`` or ``remove repo ...``;
* an optional refiner callback that can be backed by a local agent engine
  when a fleet wants conversational rewriting.

Tests exercise the deterministic layer and an injected fake refiner. The
server and Batman can use the same public functions without taking a hard
dependency on any model provider.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from intake_profiles import IntakeProfile, active_intake_profile
from spec_helper import IssueDraft, IssueReadinessResult, assess_issue_draft

Refiner = Callable[[IssueDraft, tuple[str, ...]], dict[str, Any] | str | None]


@dataclass(frozen=True)
class PlanningMemoryItem:
    """Prompt-safe memory hint surfaced while shaping a plan."""

    body: str
    repo: str
    codename: str = ""
    severity: str = "info"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningAssistantResult:
    """Result of one planning-assistant turn."""

    draft: IssueDraft
    readiness: IssueReadinessResult
    summary: str
    amendments: tuple[str, ...]
    questions: tuple[str, ...]
    issue_body: str
    spec_body: str
    memory: tuple[PlanningMemoryItem, ...] = ()


@dataclass(frozen=True)
class PostPrFeedbackItem:
    """Trusted Slack follow-up captured after a report or PR link is posted."""

    kind: str
    summary: str
    text: str
    requires_resolution: bool = False


def refine_issue_draft(
    draft: IssueDraft,
    messages: Iterable[str],
    *,
    refiner: Refiner | None = None,
    memory_provider: Any | None = None,
    memory_limit: int = 3,
) -> PlanningAssistantResult:
    """Apply operator messages to ``draft`` and return a readiness snapshot.

    ``messages`` can be Slack thread replies, local UI chat messages, or
    CLI text. Deterministic commands always run first. If ``refiner`` is
    supplied, its structured output is applied after the deterministic
    pass so a model can rewrite prose while still respecting clear
    operator commands.
    """

    clean_messages = tuple(
        _normalize_message(message) for message in messages if _clean_text(message)
    )
    amended = _apply_deterministic_feedback(draft, clean_messages)
    if refiner is not None and clean_messages:
        amended = _apply_refiner(amended, clean_messages, refiner)
    readiness = assess_issue_draft(amended)
    memory = recall_planning_memory(amended, memory_provider, limit=memory_limit)
    amendments = _summarize_amendments(clean_messages)
    questions = _planning_questions(amended, readiness)
    spec_body = render_development_spec(amended, readiness=readiness, memory=memory)
    result = PlanningAssistantResult(
        draft=amended,
        readiness=readiness,
        summary="",
        amendments=amendments,
        questions=questions,
        issue_body=readiness.issue_body,
        spec_body=spec_body,
        memory=memory,
    )
    # The intake profile (technical by default, plain when
    # ``ALFRED_INTAKE_PROFILE=plain``) controls only the user-facing
    # summary. Everything above is identical in both modes.
    return replace(result, summary=active_intake_profile().render_user_summary(result))


def recall_planning_memory(
    draft: IssueDraft,
    provider: Any | None,
    *,
    limit: int = 3,
) -> tuple[PlanningMemoryItem, ...]:
    """Recall relevant promoted lessons for a planning draft.

    Planning memory is advisory. It is shown beside the readiness report
    and embedded in saved specs, but it never bypasses readiness checks or
    invents repository scope.
    """

    if provider is None or getattr(provider, "name", "") == "null" or limit <= 0:
        return ()
    query = _planning_memory_query(draft)
    repos: list[str | None] = [repo for repo in draft.repos if repo]
    if not repos:
        repos = [None]
    out: list[PlanningMemoryItem] = []
    seen: set[str] = set()
    for repo in repos:
        for use_query in (query, None):
            if len(out) >= limit:
                break
            lessons = _safe_memory_recall(
                provider,
                repo=repo,
                query=use_query,
                limit=limit,
            )
            for lesson in lessons or []:
                item = _memory_item_from_lesson(lesson, fallback_repo=repo or "")
                if item is None:
                    continue
                key = f"{item.repo}|{item.body}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= limit:
                    break
            if out and use_query is not None:
                break
    return tuple(out)


def _safe_memory_recall(
    provider: Any,
    *,
    repo: str | None,
    query: str | None,
    limit: int,
) -> Iterable[Any]:
    try:
        return provider.recall(repo=repo, query=query, limit=limit) or ()
    except TypeError:
        try:
            return provider.recall(repo=repo, limit=limit) or ()
        except Exception:
            return ()
    except Exception:
        return ()


def apply_repository_scope_feedback(
    base_repos: Iterable[str],
    messages: Iterable[str],
    *,
    default_org: str | None = None,
) -> tuple[str, ...]:
    """Apply only repo-scope commands from operator feedback.

    This keeps Batman's approval gate honest: if Slack feedback says
    ``remove repo: web`` or ``add repo: owner/api``, execution uses the
    amended repository list rather than merely appending a note.
    """

    repos = _dedupe(str(repo).strip() for repo in base_repos if str(repo).strip())
    clean_messages = tuple(
        _normalize_message(message) for message in messages if _clean_text(message)
    )
    for message in clean_messages:
        for line in _message_lines(message):
            action = _parse_line(line)
            if action is None:
                continue
            key, value = action
            if key == "remove_repo":
                removals = _split_items(value)
                repos = [
                    repo
                    for repo in repos
                    if not any(_repo_scope_matches(repo, item) for item in removals)
                ]
            elif key == "repos":
                for item in _split_items(value):
                    repo = _qualify_repo_scope(item, default_org=default_org)
                    if not any(_repo_scope_matches(existing, repo) for existing in repos):
                        repos.append(repo)
    return tuple(repos)


def render_development_spec(
    draft: IssueDraft,
    *,
    readiness: IssueReadinessResult | None = None,
    memory: Iterable[PlanningMemoryItem] = (),
) -> str:
    """Render a practical spec document from an issue draft."""

    readiness = readiness or assess_issue_draft(draft)
    memory_block = render_planning_memory(memory)
    repos = draft.repos or ["owner/repo"]
    acceptance = draft.acceptance_criteria or ["TODO"]
    repo_lines = "\n".join(f"- `{repo}`" for repo in repos)
    acceptance_lines = "\n".join(f"- [ ] {item}" for item in acceptance)
    questions = "\n".join(f"- {question}" for question in readiness.questions) or "- None."
    findings = (
        "\n".join(
            f"- `{finding.severity}` `{finding.code}`: {finding.message}"
            for finding in readiness.findings
        )
        or "- None."
    )
    return f"""# {draft.title.strip() or "Untitled Alfred work"}

## Objective

{draft.problem.strip() or "TODO"}

## Intended User

{draft.user.strip() or "Operator or product user"}

## Current Behavior

{draft.current_behavior.strip() or "Not specified."}

## Desired Behavior

{draft.desired_behavior.strip() or "TODO"}

## Repository Scope

{repo_lines}

## Acceptance Criteria

{acceptance_lines}

## Verification Plan

{draft.test_plan.strip() or "TODO"}

## Non-goals

{draft.out_of_scope.strip() or "Not specified."}

## Rollout

{draft.rollout.strip() or "Normal Alfred PR review."}

## Open Questions

{draft.open_questions.strip() or "None."}

{memory_block}

## Alfred Readiness

- Score: {readiness.score}
- Status: {"ready" if readiness.ok else "needs scope"}

### Findings

{findings}

### Questions To Resolve

{questions}

## Implementation Guardrails

- Keep the PR scoped to the repository scope above.
- Prefer existing project patterns over new abstractions.
- Do not expand beyond the non-goals without operator approval.
- Treat acceptance criteria and verification plan as the merge gate.
"""


def render_planning_memory(memory: Iterable[PlanningMemoryItem]) -> str:
    """Render recalled planning memory for a spec or UI panel."""

    items = tuple(memory)
    if not items:
        return ""
    lines = [
        "## Planning Memory",
        "",
        "Use these as hints only. Trust the current repository and issue first.",
        "",
    ]
    for item in items:
        tags = f" [{', '.join(item.tags)}]" if item.tags else ""
        severity = "" if item.severity == "info" else f" {item.severity}"
        repo = item.repo or "all repos"
        lines.append(f"- `{repo}`{severity}{tags}: {item.body}")
    return "\n".join(lines).rstrip() + "\n"


def render_operator_amendments(feedback: Iterable[str]) -> str:
    """Render Slack thread replies as a structured prompt/issue block."""

    clean = tuple(_normalize_message(item) for item in feedback if _clean_text(item))
    if not clean:
        return ""
    amendments = _summarize_amendments(clean)
    questions = _explicit_questions_from_messages(clean)
    lines = [
        "## Operator Slack Amendments",
        "",
        "Treat these as approved plan changes captured before implementation.",
        "",
    ]
    lines.extend(f"- {item}" for item in clean)
    if amendments:
        lines.extend(["", "### Planning Assistant Interpretation", ""])
        lines.extend(f"- {item}" for item in amendments)
    if questions:
        lines.extend(["", "### Follow-up Questions", ""])
        lines.extend(f"- {question}" for question in questions)
    return "\n".join(lines).rstrip() + "\n"


def render_operator_feedback_ack(feedback: Iterable[str]) -> str:
    """Render a concise Slack acknowledgement for newly-captured feedback."""

    clean = tuple(_normalize_message(item) for item in feedback if _clean_text(item))
    if not clean:
        return ""
    amendments = _summarize_amendments(clean)
    questions = _explicit_questions_from_messages(clean)
    lines = [
        "*Plan feedback captured*",
        "",
        f"*Captured:* {len(amendments)} update(s)",
        "",
        "*Applied now:*",
    ]
    lines.extend(f"- {item}" for item in amendments[:6])
    if len(amendments) > 6:
        lines.append(f"- ...and {len(amendments) - 6} more update(s).")
    if questions:
        lines.extend(["", "*Needs a decision before execution:*"])
        lines.extend(f"- {question}" for question in questions[:4])
    lines.extend(
        [
            "",
            "*Next:* keep replying in this thread to shape the work. React "
            ":white_check_mark: only when the plan is ready to run.",
        ]
    )
    return "\n".join(lines)


def render_plan_revision_ack(
    feedback: Iterable[str],
    *,
    revised_repos: Iterable[str] = (),
    child_count: int | None = None,
) -> str:
    """Render a Slack acknowledgement with the plan state if approved now."""

    clean = tuple(_normalize_message(item) for item in feedback if _clean_text(item))
    if not clean:
        return ""
    amendments = _summarize_amendments(clean)
    questions = _explicit_questions_from_messages(clean)
    repos = tuple(str(repo).strip() for repo in revised_repos if str(repo).strip())
    lines = [
        "*Plan revised*",
        "",
        f"*Captured so far:* {len(amendments)} update(s)",
        "",
        "*Current draft now includes:*",
    ]
    lines.extend(f"- {item}" for item in amendments[:8])
    if len(amendments) > 8:
        lines.append(f"- ...and {len(amendments) - 8} more update(s).")
    if repos:
        scope_label = "repo" if len(repos) == 1 else "repos"
        child_label = ""
        if child_count is not None:
            child_label = f", {child_count} child issue(s)"
        lines.extend(
            ["", f"*Execution scope if approved now ({len(repos)} {scope_label}{child_label}):*"]
        )
        lines.extend(f"- `{repo}`" for repo in repos[:10])
        if len(repos) > 10:
            lines.append(f"- ...and {len(repos) - 10} more repo(s).")
    if questions:
        lines.extend(["", "*Needs a decision before execution:*"])
        lines.extend(f"- {question}" for question in questions[:6])
        lines.append("")
        lines.append("Alfred will not execute until these are resolved in this thread.")
    lines.extend(
        [
            "",
            "*Next:* keep replying with changes, or react :white_check_mark: "
            "when this is the right plan.",
        ]
    )
    return "\n".join(lines)


def plan_feedback_requires_resolution(feedback: Iterable[str]) -> bool:
    """Return True when Slack feedback contains explicit open questions."""

    clean = tuple(_normalize_message(item) for item in feedback if _clean_text(item))
    return bool(_explicit_questions_from_messages(clean))


def classify_post_pr_feedback(feedback: Iterable[str]) -> tuple[PostPrFeedbackItem, ...]:
    """Classify trusted follow-up replies posted after a report or PR link.

    The output is intentionally descriptive, not executable. A later agent
    can use these items as context, but this helper never grants approval,
    changes code, or widens scope by itself.
    """

    clean = tuple(_normalize_message(item) for item in feedback if _clean_text(item))
    out: list[PostPrFeedbackItem] = []
    for message in clean:
        for line in _message_lines(message):
            item = _post_pr_feedback_item(line)
            if item is not None:
                out.append(item)
    return tuple(out)


def render_post_pr_feedback_ack(
    feedback: Iterable[str],
    *,
    pr_urls: Iterable[str] = (),
    issue_url: str | None = None,
) -> str:
    """Render a Slack acknowledgement for trusted report/PR follow-up replies."""

    items = classify_post_pr_feedback(feedback)
    if not items:
        return ""
    context_links = _feedback_context_links(pr_urls=pr_urls, issue_url=issue_url)
    blockers = [item for item in items if item.requires_resolution]
    action_items = [item for item in items if not item.requires_resolution]
    lines = [
        "*Follow-up feedback captured*",
    ]
    if context_links:
        lines.extend(["", f"*Context:* {', '.join(context_links)}"])
    if action_items:
        lines.extend(["", "*Action items for the next pass:*"])
        lines.extend(f"- `{item.kind}` {item.summary}" for item in action_items[:8])
        if len(action_items) > 8:
            lines.append(f"- ...and {len(action_items) - 8} more item(s).")
    if blockers:
        lines.extend(["", "*Needs a decision before more work:*"])
        lines.extend(f"- {item.summary}" for item in blockers[:6])
    lines.extend(
        [
            "",
            "*Safety:* this reply does not approve, merge, or change code by itself. "
            "Alfred will use it as follow-up context for the next plan or PR pass.",
        ]
    )
    return "\n".join(lines)


def render_post_pr_followup_block(
    feedback: Iterable[str],
    *,
    pr_urls: Iterable[str] = (),
    issue_url: str | None = None,
) -> str:
    """Render trusted post-report replies as a Markdown context block."""

    items = classify_post_pr_feedback(feedback)
    if not items:
        return ""
    context_links = _feedback_context_links(
        pr_urls=pr_urls,
        issue_url=issue_url,
        markdown=True,
    )
    lines = [
        "## Slack Follow-up Feedback",
        "",
        "These trusted thread replies were captured after Alfred posted a report or PR link.",
        "Treat them as follow-up context only; do not merge or expand scope without operator approval.",
        "",
    ]
    if context_links:
        lines.extend(["### Linked Context", ""])
        lines.extend(f"- {link}" for link in context_links)
        lines.append("")
    lines.extend(["### Items", ""])
    lines.extend(
        f"- `{item.kind}`{' needs decision' if item.requires_resolution else ''}: {item.summary}"
        for item in items
    )
    return "\n".join(lines).rstrip() + "\n"


def post_pr_feedback_requires_resolution(feedback: Iterable[str]) -> bool:
    """Return True when report/PR follow-up includes a blocking question."""

    return any(item.requires_resolution for item in classify_post_pr_feedback(feedback))


def build_refiner_prompt(
    draft: IssueDraft,
    messages: Iterable[str],
    *,
    profile: IntakeProfile | None = None,
) -> str:
    """Prompt text for an optional local engine backed refiner.

    The persona is supplied by the active intake profile. The default
    technical profile keeps the original operator-facing prompt; the plain
    profile swaps in a friendly, jargon-free persona. The structured draft
    that downstream code builds is identical either way.
    """

    selected = profile or active_intake_profile()
    return selected.refiner_prompt(draft, messages)


def render_user_facing_summary(result: PlanningAssistantResult) -> str:
    """Render the user-facing summary for ``result`` under the active profile.

    Technical mode reports amendment counts and readiness state. Plain mode
    returns a short "Here's what I'll do ... OK to go ahead?" plan with no
    technical fields. ``result.summary`` already carries this string; this
    helper lets callers re-render after changing profiles in the same
    process (mainly for tests and the desktop client).
    """

    return active_intake_profile().render_user_summary(result)


def engine_refiner_from_env(*, workdir: Path | None = None) -> Refiner | None:
    """Return an optional local-engine refiner when explicitly enabled.

    Set ``ALFRED_PLANNING_ASSISTANT_ENGINE`` to an engine name to enable this
    path. The default UI stays deterministic and offline.
    """

    engine = (os.environ.get("ALFRED_PLANNING_ASSISTANT_ENGINE") or "").strip()
    if not engine:
        return None
    timeout = _env_int("ALFRED_PLANNING_ASSISTANT_TIMEOUT", 180)
    root = workdir or Path.cwd()

    def _refiner(draft: IssueDraft, messages: tuple[str, ...]) -> dict[str, Any] | None:
        try:
            from agent_runner import invoke_agent_engine
        except Exception:
            return None
        firing_id = datetime.now(UTC).strftime("planning-%Y%m%d-%H%M%S")
        result, _engine_used = invoke_agent_engine(
            build_refiner_prompt(draft, messages),
            engine=engine,
            agent="planning-assistant",
            firing_id=firing_id,
            workdir=root,
            claude_allowed_tools="Read",
            timeout=timeout,
            claude_max_turns=8,
            codex_timeout=timeout,
        )
        if not result.success or not result.result_text:
            return None
        try:
            parsed = json.loads(_extract_json_object(result.result_text))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    return _refiner


def _apply_deterministic_feedback(draft: IssueDraft, messages: tuple[str, ...]) -> IssueDraft:
    current = draft
    freeform: list[str] = []
    for message in messages:
        for line in _message_lines(message):
            action = _parse_line(line)
            if action is None:
                freeform.append(line)
                continue
            current = _apply_action(current, action)
    if freeform:
        notes = _append_paragraphs(
            current.open_questions, [f"Operator note: {item}" for item in freeform]
        )
        current = replace(current, open_questions=notes)
    return current


def _apply_refiner(
    draft: IssueDraft,
    messages: tuple[str, ...],
    refiner: Refiner,
) -> IssueDraft:
    raw = refiner(draft, messages)
    if raw is None:
        return draft
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return draft
    if not isinstance(raw, dict):
        return draft
    return _merge_patch(draft, raw)


def _merge_patch(draft: IssueDraft, patch: dict[str, Any]) -> IssueDraft:
    fields: dict[str, Any] = {}
    scalar_fields = {
        "title",
        "problem",
        "user",
        "current_behavior",
        "desired_behavior",
        "test_plan",
        "out_of_scope",
        "rollout",
        "open_questions",
    }
    for key in scalar_fields:
        value = patch.get(key)
        if isinstance(value, str) and value.strip():
            fields[key] = value.strip()
    for key in ("repos", "acceptance_criteria"):
        value = patch.get(key)
        if isinstance(value, str):
            value = _split_items(value)
        if isinstance(value, list):
            clean = [str(item).strip() for item in value if str(item).strip()]
            if clean:
                fields[key] = _dedupe(clean)
    return replace(draft, **fields) if fields else draft


def _parse_line(line: str) -> tuple[str, str] | None:
    cleaned = _clean_text(line).strip("-* ").strip()
    if not cleaned:
        return None
    remove_value = _prefixed_command_value(cleaned, "remove repos")
    if remove_value:
        return ("remove_repo", remove_value)
    remove_value = _prefixed_command_value(cleaned, "remove repo")
    if remove_value:
        return ("remove_repo", remove_value)
    add_value = _prefixed_command_value(cleaned, "add repos")
    if add_value:
        return ("repos", add_value)
    add_value = _prefixed_command_value(cleaned, "add repo")
    if add_value:
        return ("repos", add_value)
    if ":" not in cleaned:
        return None
    raw_field, raw_value = cleaned.split(":", 1)
    raw_field = raw_field.strip()
    value = raw_value.strip()
    if not value or not raw_field[:1].isalpha() or len(raw_field) > 30:
        return None
    field = " ".join(raw_field.replace("_", " ").replace("-", " ").lower().split())
    mapping = {
        "title": "title",
        "problem": "problem",
        "context": "problem",
        "user": "user",
        "persona": "user",
        "current": "current_behavior",
        "current behavior": "current_behavior",
        "desired": "desired_behavior",
        "desired behavior": "desired_behavior",
        "repo": "repos",
        "repos": "repos",
        "repositories": "repos",
        "acceptance": "acceptance_criteria",
        "acceptance criteria": "acceptance_criteria",
        "test": "test_plan",
        "tests": "test_plan",
        "test plan": "test_plan",
        "non goal": "out_of_scope",
        "non goals": "out_of_scope",
        "non-goal": "out_of_scope",
        "out of scope": "out_of_scope",
        "rollout": "rollout",
        "question": "open_questions",
        "questions": "open_questions",
        "open question": "open_questions",
        "open questions": "open_questions",
        "resolve question": "resolved_open_questions",
        "resolve questions": "resolved_open_questions",
        "resolved question": "resolved_open_questions",
        "resolved questions": "resolved_open_questions",
        "clear question": "resolved_open_questions",
        "clear questions": "resolved_open_questions",
        "clear open questions": "resolved_open_questions",
    }
    target = mapping.get(field)
    if target is None:
        return None
    return (target, value)


def _apply_action(draft: IssueDraft, action: tuple[str, str]) -> IssueDraft:
    key, value = action
    if key == "remove_repo":
        removals = _split_items(value)
        return replace(
            draft,
            repos=[
                repo
                for repo in draft.repos
                if not any(_repo_scope_matches(repo, item) for item in removals)
            ],
        )
    if key == "repos":
        return replace(draft, repos=_dedupe([*draft.repos, *_split_items(value)]))
    if key == "acceptance_criteria":
        return replace(
            draft,
            acceptance_criteria=_dedupe([*draft.acceptance_criteria, *_split_items(value)]),
        )
    if key == "open_questions":
        if _questions_resolved_value(value):
            return replace(draft, open_questions=_normalized_resolved_questions(value))
        return replace(
            draft, open_questions=_append_paragraphs(draft.open_questions, _split_items(value))
        )
    if key == "resolved_open_questions":
        return replace(draft, open_questions=_normalized_resolved_questions(value))
    if key == "test_plan":
        return replace(draft, test_plan=_append_paragraphs(draft.test_plan, [value]))
    if key == "out_of_scope":
        return replace(draft, out_of_scope=_append_paragraphs(draft.out_of_scope, [value]))
    if key == "rollout":
        return replace(draft, rollout=_append_paragraphs(draft.rollout, [value]))
    if key == "problem":
        return replace(draft, problem=_append_paragraphs(draft.problem, [value]))
    if key == "current_behavior":
        return replace(draft, current_behavior=_append_paragraphs(draft.current_behavior, [value]))
    if key == "desired_behavior":
        return replace(draft, desired_behavior=_append_paragraphs(draft.desired_behavior, [value]))
    if key == "title":
        return replace(draft, title=value)
    if key == "user":
        return replace(draft, user=value)
    return draft


def _qualify_repo_scope(value: str, *, default_org: str | None = None) -> str:
    cleaned = _clean_text(value).strip()
    if default_org and cleaned and "/" not in cleaned:
        return f"{default_org}/{cleaned}"
    return cleaned


def _repo_scope_matches(repo: str, candidate: str) -> bool:
    repo_clean = _clean_text(repo).lower()
    candidate_clean = _clean_text(candidate).lower()
    if not repo_clean or not candidate_clean:
        return False
    if repo_clean == candidate_clean:
        return True
    if "/" not in candidate_clean:
        tail = repo_clean.rsplit("/", 1)[-1]
        return tail == candidate_clean or tail.endswith(
            (f"-{candidate_clean}", f"_{candidate_clean}")
        )
    return False


def _summarize_amendments(messages: tuple[str, ...]) -> tuple[str, ...]:
    summaries: list[str] = []
    for message in messages:
        for line in _message_lines(message):
            action = _parse_line(line)
            if action is None:
                summaries.append(f"Capture operator note: {line}")
                continue
            key, value = action
            if key == "remove_repo":
                summaries.append(f"Remove repository scope: {value}")
            elif key == "repos":
                summaries.append(f"Add repository scope: {value}")
            elif key == "acceptance_criteria":
                summaries.append(f"Add acceptance criterion: {value}")
            elif key == "open_questions":
                summaries.append(f"Track open question: {value}")
            elif key == "resolved_open_questions":
                summaries.append(f"Resolve open questions: {value}")
            else:
                summaries.append(f"Update {key.replace('_', ' ')}: {value}")
    if not summaries:
        summaries = [f"Capture operator note: {message}" for message in messages]
    return tuple(_dedupe(summaries))


def _explicit_questions_from_messages(messages: tuple[str, ...]) -> tuple[str, ...]:
    questions: list[str] = []
    for message in messages:
        for line in _message_lines(message):
            action = _parse_line(line)
            if action is not None and action[0] == "open_questions":
                questions.extend(_split_items(action[1]))
    return tuple(_dedupe(questions))


def _post_pr_feedback_item(line: str) -> PostPrFeedbackItem | None:
    cleaned = _clean_text(line)
    if not cleaned:
        return None
    action = _parse_line(cleaned)
    if action is not None:
        key, value = action
        if key == "open_questions":
            return PostPrFeedbackItem("question", f"Question: {value}", cleaned, True)
        if key == "acceptance_criteria":
            return PostPrFeedbackItem("acceptance", f"Acceptance: {value}", cleaned)
        if key == "test_plan":
            return PostPrFeedbackItem("test", f"Test: {value}", cleaned)
        if key in {"repos", "remove_repo"}:
            label = "Scope change"
            return PostPrFeedbackItem("scope", f"{label}: {value}", cleaned, True)
        return PostPrFeedbackItem("change", f"{key.replace('_', ' ')}: {value}", cleaned)
    prefixed = _feedback_prefix(cleaned)
    if prefixed is not None:
        kind, value = prefixed
        requires = kind in {"blocker", "hold", "question"}
        summary_prefix = {
            "blocker": "Blocker",
            "bug": "Bug",
            "change": "Change",
            "fix": "Fix",
            "follow_up": "Follow-up",
            "hold": "Hold",
            "question": "Question",
        }.get(kind, kind.replace("_", " ").title())
        return PostPrFeedbackItem(kind, f"{summary_prefix}: {value}", cleaned, requires)
    requires = cleaned.endswith("?")
    kind = "question" if requires else "note"
    prefix = "Question" if requires else "Note"
    return PostPrFeedbackItem(kind, f"{prefix}: {cleaned}", cleaned, requires)


def _feedback_prefix(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    raw, value = line.split(":", 1)
    field = " ".join(raw.replace("_", " ").replace("-", " ").lower().split())
    value = value.strip()
    if not value:
        return None
    mapping = {
        "blocker": "blocker",
        "bug": "bug",
        "change": "change",
        "fix": "fix",
        "follow up": "follow_up",
        "followup": "follow_up",
        "hold": "hold",
        "question": "question",
    }
    kind = mapping.get(field)
    return (kind, value) if kind else None


def _feedback_context_links(
    *,
    pr_urls: Iterable[str] = (),
    issue_url: str | None = None,
    markdown: bool = False,
) -> tuple[str, ...]:
    links: list[str] = []
    if issue_url and issue_url.strip():
        url = issue_url.strip()
        links.append(f"[issue]({url})" if markdown else f"<{url}|issue>")
    for index, raw in enumerate(pr_urls, start=1):
        url = str(raw or "").strip()
        if not url:
            continue
        label = f"PR {index}"
        links.append(f"[{label}]({url})" if markdown else f"<{url}|{label}>")
    return tuple(links)


def _planning_memory_query(draft: IssueDraft) -> str | None:
    for value in (
        draft.title,
        draft.problem,
        draft.desired_behavior,
        " ".join(draft.acceptance_criteria),
    ):
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned[:120]
    return None


def _memory_item_from_lesson(
    lesson: Any,
    *,
    fallback_repo: str,
) -> PlanningMemoryItem | None:
    if isinstance(lesson, dict):
        body = str(lesson.get("body") or lesson.get("text") or "").strip()
        repo = str(lesson.get("repo") or fallback_repo).strip()
        codename = str(
            lesson.get("codename") or lesson.get("agent") or lesson.get("source") or ""
        ).strip()
        severity = str(lesson.get("severity") or "info").strip().lower()
        tags_raw = lesson.get("tags") or ()
    else:
        body = str(getattr(lesson, "body", "") or "").strip()
        repo = str(getattr(lesson, "repo", "") or fallback_repo).strip()
        codename = str(
            getattr(lesson, "codename", "")
            or getattr(lesson, "agent", "")
            or getattr(lesson, "source", "")
        ).strip()
        severity = str(getattr(lesson, "severity", "info") or "info").strip().lower()
        tags_raw = getattr(lesson, "tags", ()) or ()
    if not body:
        return None
    if isinstance(tags_raw, str):
        tags = tuple(_split_items(tags_raw))
    else:
        tags = tuple(str(tag).strip() for tag in tags_raw if str(tag).strip())
    return PlanningMemoryItem(
        body=body,
        repo=repo,
        codename=codename,
        severity=severity or "info",
        tags=tags,
    )


def _prefixed_command_value(cleaned: str, command: str) -> str | None:
    lowered = cleaned.lower()
    if not lowered.startswith(command):
        return None
    rest = cleaned[len(command) :].lstrip()
    if rest.startswith(":"):
        rest = rest[1:].lstrip()
    return rest or None


def _planning_questions(
    draft: IssueDraft,
    readiness: IssueReadinessResult,
) -> tuple[str, ...]:
    questions = list(readiness.questions)
    if draft.open_questions and draft.open_questions.strip().lower() not in {"none", "none."}:
        questions.append("Resolve or explicitly accept the open questions before implementation.")
    return tuple(_dedupe(questions))


def _questions_resolved_value(value: str) -> bool:
    cleaned = _clean_text(value).lower().strip(".")
    return cleaned in {
        "accepted as risk",
        "accepted risk",
        "n/a",
        "no",
        "no open questions",
        "none",
        "not applicable",
        "resolved",
    }


def _normalized_resolved_questions(value: str) -> str:
    cleaned = _clean_text(value).lower().strip(".")
    if cleaned in {"accepted as risk", "accepted risk"}:
        return "accepted as risk"
    return "None."


def _message_lines(message: str) -> list[str]:
    return [line.strip() for line in message.splitlines() if line.strip()]


def _split_items(value: str) -> list[str]:
    parts: list[str] = []
    for line in value.splitlines():
        for part in re.split(r",|;", line):
            cleaned = part.strip().lstrip("-*").strip()
            if cleaned:
                parts.append(cleaned)
    return parts


def _append_paragraphs(existing: str, additions: Iterable[str]) -> str:
    chunks = [_clean_text(existing)] if _clean_text(existing) else []
    chunks.extend(_clean_text(item) for item in additions if _clean_text(item))
    return "\n".join(_dedupe(chunks))


def _extract_json_object(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_message(value: str) -> str:
    lines = [_clean_text(line) for line in str(value or "").splitlines()]
    return "\n".join(line for line in lines if line)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value.strip())
    return out
