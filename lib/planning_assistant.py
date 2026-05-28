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

from spec_helper import IssueDraft, IssueReadinessResult, assess_issue_draft

Refiner = Callable[[IssueDraft, tuple[str, ...]], dict[str, Any] | str | None]


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


def refine_issue_draft(
    draft: IssueDraft,
    messages: Iterable[str],
    *,
    refiner: Refiner | None = None,
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
    amendments = _summarize_amendments(clean_messages)
    questions = _planning_questions(amended, readiness)
    summary = _summary_for(readiness, amendments)
    spec_body = render_development_spec(amended, readiness=readiness)
    return PlanningAssistantResult(
        draft=amended,
        readiness=readiness,
        summary=summary,
        amendments=amendments,
        questions=questions,
        issue_body=readiness.issue_body,
        spec_body=spec_body,
    )


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
) -> str:
    """Render a practical spec document from an issue draft."""

    readiness = readiness or assess_issue_draft(draft)
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
        "[ALFRED-PLAN-FEEDBACK] captured your plan update.",
        "",
        "*Understood:*",
    ]
    lines.extend(f"- {item}" for item in amendments[:6])
    if len(amendments) > 6:
        lines.append(f"- ...and {len(amendments) - 6} more amendment(s).")
    if questions:
        lines.extend(["", "*Open questions you asked Alfred to track:*"])
        lines.extend(f"- {question}" for question in questions[:4])
    lines.extend(
        [
            "",
            "Reply with more changes, or approve with :white_check_mark: when the plan is ready.",
        ]
    )
    return "\n".join(lines)


def build_refiner_prompt(draft: IssueDraft, messages: Iterable[str]) -> str:
    """Prompt text for an optional local engine backed refiner."""

    payload = {
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
        "operator_messages": list(messages),
    }
    return (
        "You are Alfred's planning assistant. Tighten the draft so technical and "
        "non-technical teammates can describe work safely before an autonomous "
        "engineering agent starts.\n\n"
        "Return JSON only with any of these keys: title, problem, user, "
        "current_behavior, desired_behavior, repos, acceptance_criteria, test_plan, "
        "out_of_scope, rollout, open_questions. Use arrays for repos and "
        "acceptance_criteria. Keep scope narrow, ask questions when uncertain, "
        "and do not invent repository names.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )


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
    }
    target = mapping.get(field)
    if target is None:
        return None
    return (target, value)


def _apply_action(draft: IssueDraft, action: tuple[str, str]) -> IssueDraft:
    key, value = action
    if key == "remove_repo":
        remove = {item.lower() for item in _split_items(value)}
        return replace(draft, repos=[repo for repo in draft.repos if repo.lower() not in remove])
    if key == "repos":
        return replace(draft, repos=_dedupe([*draft.repos, *_split_items(value)]))
    if key == "acceptance_criteria":
        return replace(
            draft,
            acceptance_criteria=_dedupe([*draft.acceptance_criteria, *_split_items(value)]),
        )
    if key == "open_questions":
        return replace(
            draft, open_questions=_append_paragraphs(draft.open_questions, _split_items(value))
        )
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


def _summary_for(readiness: IssueReadinessResult, amendments: tuple[str, ...]) -> str:
    state = "ready for implementation" if readiness.ok else "needs scope before implementation"
    if amendments:
        return f"{len(amendments)} amendment(s) applied; draft {state}."
    return f"No structured amendments found; draft {state}."


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
