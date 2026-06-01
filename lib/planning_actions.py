"""Local planning inbox actions shared by Slack and the control center."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from planning_assistant import refine_issue_draft
from server.reader import PlanDraft
from spec_helper import IssueDraft


@dataclass(frozen=True)
class FollowupConversion:
    draft_id: str
    draft_path: Path
    archived_path: Path


def convert_followup_to_draft(
    plan: PlanDraft,
    *,
    state_root: Path,
    memory_provider: Any | None = None,
) -> FollowupConversion:
    """Convert one captured Slack follow-up into a local planning draft."""

    if plan.source != "followup":
        raise ValueError("plan is not a follow-up")
    draft_path = _write_followup_draft(plan, state_root=state_root, memory_provider=memory_provider)
    try:
        archived_path = archive_followup(plan, action="converted", target_path=draft_path)
    except Exception:
        draft_path.unlink(missing_ok=True)
        raise
    return FollowupConversion(
        draft_id=draft_path.stem,
        draft_path=draft_path,
        archived_path=archived_path,
    )


def mark_followup_handled(plan: PlanDraft) -> Path:
    """Archive a captured Slack follow-up as handled without creating a draft."""

    if plan.source != "followup":
        raise ValueError("plan is not a follow-up")
    return archive_followup(plan, action="handled")


def archive_followup(
    plan: PlanDraft,
    *,
    action: str,
    target_path: Path | None = None,
) -> Path:
    path = Path(plan.path)
    content = path.read_text(encoding="utf-8").rstrip()
    handled_dir = path.parent / "handled"
    handled_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_path = handled_dir / path.name
    if archive_path.exists():
        archive_path = handled_dir / f"{path.stem}-{stamp}{path.suffix}"
    metadata = [
        "",
        "---",
        "",
        f"- Follow-up action: {action}",
        f"- Follow-up action at: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    if target_path is not None:
        metadata.append(f"- Planning draft: {target_path}")
    tmp = archive_path.with_name(f"{archive_path.name}.tmp")
    tmp.write_text(content + "\n".join(metadata) + "\n", encoding="utf-8")
    tmp.replace(archive_path)
    path.unlink(missing_ok=True)
    return archive_path


def _write_followup_draft(
    plan: PlanDraft,
    *,
    state_root: Path,
    memory_provider: Any | None,
) -> Path:
    draft = _draft_from_followup(plan)
    assistant_result = refine_issue_draft(draft, [], memory_provider=memory_provider)
    issue_body = _with_followup_context(assistant_result.issue_body, plan)
    spec_body = _with_followup_context(assistant_result.spec_body, plan)
    root = state_root / "planning-drafts"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"followup-{_slug(plan.plan_id)}-{_slug(draft.title)}.json"
    payload = {
        "source": "planning",
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "converted_from": {
            "plan_id": plan.plan_id,
            "path": plan.path,
            "parent": plan.parent,
            "title": plan.title,
        },
        "draft": asdict(assistant_result.draft),
        "issue_body": issue_body,
        "spec_body": spec_body,
        "readiness": asdict(assistant_result.readiness),
        "memory": [asdict(item) for item in assistant_result.memory],
        "revision_count": 0,
        "revisions": [],
    }
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _draft_from_followup(plan: PlanDraft) -> IssueDraft:
    clean_title = re.sub(r"^follow-up for\s+", "", plan.title, flags=re.IGNORECASE).strip()
    title = f"Follow up: {clean_title or 'captured Slack feedback'}"
    return IssueDraft(
        title=title,
        problem=(
            "A trusted Slack follow-up was captured after Alfred posted a report "
            "or PR link. It needs an explicit planning pass before any code or "
            "docs change."
        ),
        user="Repo owner, teammate, or operator following up on shipped work",
        current_behavior=plan.preview or "Follow-up context is captured in the local Plans inbox.",
        desired_behavior=(
            "Decide whether the follow-up needs code, docs, tests, a scoped "
            "issue, or an explicit no-change response."
        ),
        repos=_repos_from_followup(plan),
        acceptance_criteria=[
            "The captured follow-up is addressed or explicitly declined.",
            "Any resulting work links back to the original issue, PR, or Slack thread.",
        ],
        test_plan=(
            "Run the smallest relevant tests for the affected area and verify "
            "the follow-up is covered."
        ),
        out_of_scope=(
            "No automatic merge, deployment, or broad scope expansion from captured feedback."
        ),
        open_questions=(
            "Confirm the intended response before implementation if the follow-up changes scope."
        ),
    )


def _repos_from_followup(plan: PlanDraft) -> list[str]:
    repos: list[str] = []
    urls = [plan.parent or ""]
    urls.extend(re.findall(r"https://github\.com/[^\s),>`]+", plan.content))
    for url in urls:
        repo = _repo_from_github_url(url)
        if repo and repo not in repos:
            repos.append(repo)
    return repos


def _with_followup_context(body: str, plan: PlanDraft) -> str:
    return (
        body.rstrip()
        + "\n\n## Captured Follow-up Context\n\n"
        + f"- Source: `{plan.plan_id}`\n"
        + (f"- Parent: {plan.parent}\n" if plan.parent else "")
        + "\n"
        + plan.content.strip()
        + "\n"
    )


def _repo_from_github_url(url: str) -> str | None:
    match = re.search(r"github\.com/([^/\s),>`]+)/([^/\s),>`]+)", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _slug(value: str, *, limit: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return (slug or "item")[:limit]


__all__ = [
    "FollowupConversion",
    "archive_followup",
    "convert_followup_to_draft",
    "mark_followup_handled",
]
