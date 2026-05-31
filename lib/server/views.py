"""Route handlers for ``alfred serve``.

Three views:

* ``GET /``                  Fleet status (HTMX auto-refresh every 10s).
* ``GET /firings``           Recent firings (optionally filtered by codename).
* ``GET /firings/{id}``      Single firing detail.
* ``GET /plans``             Saved Batman plans.
* ``GET /plans/{id}``        Single saved Batman plan.
* ``GET/POST /planning``     Local issue/spec readiness helper.

Two HTMX partials live behind the same URLs via the ``HX-Request`` header,
``htmx-only`` reduces the round trip to just the table body rather than
re-rendering the whole shell. Keeps the dashboard cheap to refresh.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from planning_assistant import (
    PlanningAssistantResult,
    engine_refiner_from_env,
    refine_issue_draft,
)
from spec_helper import IssueDraft

from server.reader import PlanDraft


def register_routes(app: FastAPI) -> None:
    """Bind the three GET routes to ``app``."""

    @app.get("/", response_class=HTMLResponse)
    async def fleet(request: Request) -> HTMLResponse:
        reader = request.app.state.reader
        templates = request.app.state.templates
        agents = reader.list_agents()
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "fleet_table.html",
                {
                    "agents": agents,
                    "total_today": sum(a.firings_today for a in agents),
                },
            )
        reliability = reader.reliability_report()
        recent_firings = reader.list_recent_firings(limit=5)
        recent_plans = reader.list_plans(limit=4)
        return templates.TemplateResponse(
            request,
            "fleet.html",
            {
                "agents": agents,
                "total_today": sum(a.firings_today for a in agents),
                "reliability": reliability,
                "recent_firings": recent_firings,
                "recent_plans": recent_plans,
                "fleet_counts": _fleet_counts(agents, recent_firings),
            },
        )

    @app.get("/firings", response_class=HTMLResponse)
    async def firings(request: Request, codename: str | None = None) -> HTMLResponse:
        reader = request.app.state.reader
        templates = request.app.state.templates
        rows = reader.list_recent_firings(limit=50, codename=codename)
        # Sidebar codename filter list is derived from list_agents so the
        # filter renders even when the currently filtered view is empty.
        all_agents = reader.list_agents()
        return templates.TemplateResponse(
            request,
            "firings.html",
            {
                "rows": rows,
                "codename": codename,
                "all_codenames": [a.codename for a in all_agents],
            },
        )

    @app.get("/firings/{firing_id}", response_class=HTMLResponse)
    async def firing_detail(request: Request, firing_id: str) -> HTMLResponse:
        reader = request.app.state.reader
        templates = request.app.state.templates
        record = reader.get_firing(firing_id)
        if record is None:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {
                    "title": "Firing not found",
                    "item_id": firing_id,
                    "back_url": "/firings",
                    "back_label": "back to firings",
                },
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "firing_detail.html",
            {"firing": record},
        )

    @app.get("/plans", response_class=HTMLResponse)
    async def plans(request: Request) -> HTMLResponse:
        reader = request.app.state.reader
        templates = request.app.state.templates
        rows = reader.list_plans(limit=50)
        return templates.TemplateResponse(
            request,
            "plans.html",
            {"rows": rows},
        )

    @app.get("/plans/{plan_id}", response_class=HTMLResponse)
    async def plan_detail(request: Request, plan_id: str) -> HTMLResponse:
        reader = request.app.state.reader
        templates = request.app.state.templates
        plan = reader.get_plan(plan_id)
        if plan is None:
            return templates.TemplateResponse(
                request,
                "not_found.html",
                {
                    "title": "Plan not found",
                    "item_id": plan_id,
                    "back_url": "/plans",
                    "back_label": "back to plans",
                },
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "plan_detail.html",
            {"plan": plan},
        )

    @app.post("/plans/{plan_id}/convert-followup")
    async def convert_followup(request: Request, plan_id: str):
        if not _same_origin_post(request):
            return HTMLResponse("Forbidden", status_code=403)
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None or plan.source != "followup":
            return RedirectResponse("/plans", status_code=303)
        draft_path, _archived_path = _convert_and_archive_followup(request, plan)
        return RedirectResponse(f"/plans/{draft_path.stem}", status_code=303)

    @app.post("/plans/{plan_id}/mark-handled")
    async def mark_followup_handled(request: Request, plan_id: str):
        if not _same_origin_post(request):
            return HTMLResponse("Forbidden", status_code=403)
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is not None and plan.source == "followup":
            _archive_followup(plan, action="handled")
        return RedirectResponse("/plans", status_code=303)

    @app.get("/api/status", response_class=JSONResponse)
    async def api_status(request: Request) -> JSONResponse:
        reader = request.app.state.reader
        agents = reader.list_agents()
        reliability = reader.reliability_report()
        return JSONResponse(
            _jsonable(
                {
                    "agents": agents,
                    "total_today": sum(agent.firings_today for agent in agents),
                    "reliability": reliability,
                }
            )
        )

    @app.get("/api/actions", response_class=JSONResponse)
    async def api_actions(request: Request) -> JSONResponse:
        reliability = request.app.state.reader.reliability_report()
        return JSONResponse(
            _jsonable(
                {
                    "status": reliability.get("status", "unknown"),
                    "actions": reliability.get("actions", []),
                    "failure_patterns": reliability.get("failure_patterns", []),
                    "stale_workers": reliability.get("stale_workers", []),
                    "promotion_suggestions": reliability.get("promotion_suggestions", []),
                    "error": reliability.get("error"),
                    "errors": reliability.get("errors", {}),
                }
            )
        )

    @app.get("/api/firings", response_class=JSONResponse)
    async def api_firings(
        request: Request,
        codename: str | None = None,
        limit: int = 50,
    ) -> JSONResponse:
        rows = request.app.state.reader.list_recent_firings(
            limit=min(max(1, limit), 200),
            codename=codename,
        )
        return JSONResponse(_jsonable({"rows": rows}))

    @app.get("/api/firings/{firing_id}", response_class=JSONResponse)
    async def api_firing_detail(request: Request, firing_id: str) -> JSONResponse:
        record = request.app.state.reader.get_firing(firing_id)
        if record is None:
            return JSONResponse({"error": "firing not found"}, status_code=404)
        return JSONResponse(_jsonable(record))

    @app.get("/api/plans", response_class=JSONResponse)
    async def api_plans(request: Request, limit: int = 50) -> JSONResponse:
        rows = request.app.state.reader.list_plans(limit=min(max(1, limit), 200))
        return JSONResponse(_jsonable({"rows": rows}))

    @app.get("/api/plans/drafts", response_class=JSONResponse)
    async def api_list_compose_drafts(request: Request) -> JSONResponse:
        rows = _list_compose_drafts(request)
        return JSONResponse({"rows": rows})

    @app.get("/api/plans/{plan_id}", response_class=JSONResponse)
    async def api_plan_detail(request: Request, plan_id: str) -> JSONResponse:
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        return JSONResponse(_jsonable(plan))

    @app.post("/api/plans/{plan_id}/convert-followup", response_class=JSONResponse)
    async def api_convert_followup(request: Request, plan_id: str) -> JSONResponse:
        if not _same_origin_post(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if plan.source != "followup":
            return JSONResponse({"error": "plan is not a follow-up"}, status_code=400)
        draft_path, archived_path = _convert_and_archive_followup(request, plan)
        return JSONResponse(
            {
                "draft_id": draft_path.stem,
                "draft_path": str(draft_path),
                "archived_path": str(archived_path),
            }
        )

    @app.post("/api/plans/{plan_id}/mark-handled", response_class=JSONResponse)
    async def api_mark_followup_handled(request: Request, plan_id: str) -> JSONResponse:
        if not _same_origin_post(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if plan.source != "followup":
            return JSONResponse({"error": "plan is not a follow-up"}, status_code=400)
        archived_path = _archive_followup(plan, action="handled")
        return JSONResponse({"archived_path": str(archived_path)})

    @app.post("/api/plans/draft", response_class=JSONResponse)
    async def api_compose_draft(request: Request) -> JSONResponse:
        if not _same_origin_post(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        text = str(body.get("text") or "").strip()
        draft_id = _safe_compose_draft_id(body.get("draft_id"))
        prior_payload, prior_path = _read_compose_draft_payload(request, draft_id)
        base_draft = _compose_base_draft(body, prior_payload)

        if not text and prior_payload is None and not _draft_has_signal(base_draft):
            return JSONResponse(
                {"error": "describe the work in the text field before drafting"},
                status_code=400,
            )

        messages = [text] if text else []
        memory_provider = _planning_memory_provider(request)
        assistant_result: PlanningAssistantResult = refine_issue_draft(
            base_draft,
            messages,
            refiner=(
                engine_refiner_from_env(workdir=_planning_workdir(request)) if messages else None
            ),
            memory_provider=memory_provider,
        )
        draft = assistant_result.draft
        readiness = assistant_result.readiness
        revisions = list(_existing_revisions(prior_payload))
        if text:
            revisions.append(text)
        saved_path, draft_id = _save_compose_draft(
            request,
            draft=draft,
            assistant_result=assistant_result,
            draft_id=draft_id,
            draft_path=prior_path,
            prior_payload=prior_payload,
            revisions=revisions,
        )
        return JSONResponse(
            {
                "draft_id": draft_id,
                "saved_path": str(saved_path),
                "title": draft.title,
                "readiness": {
                    "ok": readiness.ok,
                    "score": readiness.score,
                },
                "questions": list(assistant_result.questions),
                "findings": [
                    {
                        "code": finding.code,
                        "severity": finding.severity,
                        "message": finding.message,
                    }
                    for finding in readiness.findings
                ],
                "summary": assistant_result.summary,
                "spec_body": assistant_result.spec_body,
                "revision_count": len(revisions),
                "draft": {
                    "title": draft.title,
                    "problem": draft.problem,
                    "user": draft.user,
                    "current_behavior": draft.current_behavior,
                    "desired_behavior": draft.desired_behavior,
                    "repos": list(draft.repos),
                    "acceptance_criteria": list(draft.acceptance_criteria),
                    "test_plan": draft.test_plan,
                    "out_of_scope": draft.out_of_scope,
                    "rollout": draft.rollout,
                    "open_questions": draft.open_questions,
                },
            }
        )

    @app.get("/planning", response_class=HTMLResponse)
    async def planning(request: Request) -> HTMLResponse:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "planning.html",
            {
                "draft": IssueDraft(title=""),
                "result": None,
                "assistant_result": None,
                "chat_message": "",
                "saved_path": None,
                "spec_saved_path": None,
                "memory_candidate_ids": (),
            },
        )

    @app.post("/planning", response_class=HTMLResponse)
    async def planning_submit(request: Request) -> HTMLResponse:
        templates = request.app.state.templates
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        draft = _draft_from_form(form)
        action = _first(form, "action")
        chat_message = _first(form, "chat_message")
        memory_provider = _planning_memory_provider(request)
        should_refine = action == "refine" or (action in {"save", "save_spec"} and chat_message)
        assistant_result: PlanningAssistantResult = refine_issue_draft(
            draft,
            [chat_message] if should_refine else [],
            refiner=(
                engine_refiner_from_env(workdir=_planning_workdir(request))
                if should_refine
                else None
            ),
            memory_provider=memory_provider,
        )
        draft = assistant_result.draft
        result = assistant_result.readiness
        saved_path = None
        spec_saved_path = None
        memory_candidate_ids: tuple[str, ...] = ()
        if action == "save":
            saved_path = str(_save_issue_draft(request, draft, result.issue_body))
        elif action == "save_spec":
            spec_path = _save_planning_text(
                request,
                draft,
                assistant_result.spec_body,
                directory="spec-drafts",
                suffix="spec",
            )
            spec_saved_path = str(spec_path)
            memory_candidate_ids = _propose_planning_memory_candidate(
                request,
                draft,
                spec_path=spec_path,
                spec_body=assistant_result.spec_body,
                memory_provider=memory_provider,
            )
        return templates.TemplateResponse(
            request,
            "planning.html",
            {
                "draft": draft,
                "result": result,
                "assistant_result": assistant_result,
                "chat_message": "",
                "saved_path": saved_path,
                "spec_saved_path": spec_saved_path,
                "memory_candidate_ids": memory_candidate_ids,
            },
        )

    @app.get("/healthz", response_class=HTMLResponse)
    async def healthz() -> HTMLResponse:
        # Minimal liveness probe. Returns 200 with "ok" body, no template.
        return HTMLResponse("ok")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _fleet_counts(agents: list[Any], recent_firings: list[Any]) -> dict[str, int]:
    return {
        "live": sum(1 for agent in agents if getattr(agent, "status", "") == "live"),
        "idle": sum(1 for agent in agents if getattr(agent, "status", "") == "idle"),
        "error": sum(1 for agent in agents if getattr(agent, "status", "") == "error"),
        "running": sum(
            1 for firing in recent_firings if getattr(firing, "status", "") == "running"
        ),
    }


def _draft_from_form(form: dict[str, list[str]]) -> IssueDraft:
    return IssueDraft(
        title=_first(form, "title"),
        problem=_first(form, "problem"),
        user=_first(form, "user"),
        current_behavior=_first(form, "current_behavior"),
        desired_behavior=_first(form, "desired_behavior"),
        repos=_lines(_first(form, "repos")),
        acceptance_criteria=_lines(_first(form, "acceptance_criteria")),
        test_plan=_first(form, "test_plan"),
        out_of_scope=_first(form, "out_of_scope"),
        rollout=_first(form, "rollout"),
        open_questions=_first(form, "open_questions"),
    )


def _first(form: dict[str, list[str]], key: str) -> str:
    return (form.get(key) or [""])[0].strip()


def _lines(value: str) -> list[str]:
    return [line.strip().lstrip("- ").strip() for line in value.splitlines() if line.strip()]


def _save_issue_draft(request: Request, draft: IssueDraft, body: str) -> Path:
    return _save_planning_text(request, draft, body, directory="planning-drafts", suffix="issue")


_COMPOSE_PREFIX = "compose-"


def _safe_compose_draft_id(raw: Any) -> str | None:
    """Validate a caller-supplied compose draft id, or return ``None``."""
    if raw is None:
        return None
    candidate = str(raw).strip()
    if not candidate:
        return None
    if not candidate.startswith(_COMPOSE_PREFIX):
        return None
    if "/" in candidate or "\\" in candidate or candidate.startswith("."):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
        return None
    return candidate


def _compose_base_draft(body: dict[str, Any], prior_payload: dict[str, Any] | None) -> IssueDraft:
    """Build the starting draft from an explicit ``draft`` block or the prior save."""
    raw_draft = body.get("draft")
    if isinstance(raw_draft, dict):
        return _draft_from_payload(raw_draft)
    if prior_payload is not None:
        prior_draft = prior_payload.get("draft")
        if isinstance(prior_draft, dict):
            return _draft_from_payload(prior_draft)
    return IssueDraft(title=str(body.get("title") or "").strip())


def _draft_from_payload(payload: dict[str, Any]) -> IssueDraft:
    return IssueDraft(
        title=str(payload.get("title") or "").strip(),
        problem=str(payload.get("problem") or "").strip(),
        user=str(payload.get("user") or "").strip(),
        current_behavior=str(payload.get("current_behavior") or "").strip(),
        desired_behavior=str(payload.get("desired_behavior") or "").strip(),
        repos=_payload_list(payload.get("repos")),
        acceptance_criteria=_payload_list(payload.get("acceptance_criteria")),
        test_plan=str(payload.get("test_plan") or "").strip(),
        out_of_scope=str(payload.get("out_of_scope") or "").strip(),
        rollout=str(payload.get("rollout") or "").strip(),
        open_questions=str(payload.get("open_questions") or "").strip(),
    )


def _payload_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return _lines(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _draft_has_signal(draft: IssueDraft) -> bool:
    return bool(
        draft.title
        or draft.problem
        or draft.desired_behavior
        or draft.repos
        or draft.acceptance_criteria
    )


def _existing_revisions(prior_payload: dict[str, Any] | None) -> tuple[str, ...]:
    if not prior_payload:
        return ()
    raw = prior_payload.get("revisions")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _read_compose_draft_payload(
    request: Request, draft_id: str | None
) -> tuple[dict[str, Any] | None, Path | None]:
    if not draft_id:
        return None, None
    root = _state_planning_root(request)
    if not root.is_dir():
        return None, None
    path = next(
        (
            candidate
            for candidate in root.glob(f"{_COMPOSE_PREFIX}*.json")
            if candidate.stem == draft_id
        ),
        None,
    )
    if path is None:
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    return (payload, path) if isinstance(payload, dict) else (None, None)


def _save_compose_draft(
    request: Request,
    *,
    draft: IssueDraft,
    assistant_result: PlanningAssistantResult,
    draft_id: str | None,
    draft_path: Path | None,
    prior_payload: dict[str, Any] | None,
    revisions: list[str],
) -> tuple[Path, str]:
    root = _state_planning_root(request)
    root.mkdir(parents=True, exist_ok=True)
    if draft_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        draft_id = f"{_COMPOSE_PREFIX}{stamp}-{_slug(draft.title)}"
        draft_path = root / f"{draft_id}.json"
    elif draft_id is None:
        draft_id = draft_path.stem
    created_at = (
        str(prior_payload.get("created_at"))
        if prior_payload and prior_payload.get("created_at")
        else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    payload = {
        "source": "compose",
        "draft_id": draft_id,
        "created_at": created_at,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "draft": asdict(draft),
        "issue_body": assistant_result.issue_body,
        "spec_body": assistant_result.spec_body,
        "readiness": asdict(assistant_result.readiness),
        "questions": list(assistant_result.questions),
        "memory": [asdict(item) for item in assistant_result.memory],
        "revision_count": len(revisions),
        "revisions": revisions,
    }
    tmp = draft_path.with_name(f"{draft_path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(draft_path)
    return draft_path, draft_id


def _list_compose_drafts(request: Request) -> list[dict[str, Any]]:
    root = _state_planning_root(request)
    if not root.is_dir():
        return []
    drafts: list[tuple[float, dict[str, Any]]] = []
    for path in root.glob(f"{_COMPOSE_PREFIX}*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_draft = payload.get("draft")
        draft = raw_draft if isinstance(raw_draft, dict) else {}
        raw_readiness = payload.get("readiness")
        readiness = raw_readiness if isinstance(raw_readiness, dict) else {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        drafts.append(
            (
                mtime,
                {
                    "draft_id": path.stem,
                    "title": str(draft.get("title") or "Compose draft"),
                    "readiness": {
                        "ok": bool(readiness.get("ok")),
                        "score": readiness.get("score"),
                    },
                    "revision_count": payload.get("revision_count") or 0,
                    "updated_at": payload.get("updated_at") or payload.get("created_at"),
                },
            )
        )
    drafts.sort(key=lambda item: item[0], reverse=True)
    return [row for _mtime, row in drafts]


def _convert_and_archive_followup(request: Request, plan: PlanDraft) -> tuple[Path, Path]:
    draft_path = _convert_followup_to_planning_draft(request, plan)
    try:
        archived_path = _archive_followup(plan, action="converted", target_path=draft_path)
    except Exception:
        draft_path.unlink(missing_ok=True)
        raise
    return draft_path, archived_path


def _convert_followup_to_planning_draft(request: Request, plan: PlanDraft) -> Path:
    draft = _draft_from_followup(plan)
    memory_provider = _planning_memory_provider(request)
    assistant_result = refine_issue_draft(draft, [], memory_provider=memory_provider)
    issue_body = _with_followup_context(assistant_result.issue_body, plan)
    spec_body = _with_followup_context(assistant_result.spec_body, plan)
    root = _state_planning_root(request)
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
    repos = _repos_from_followup(plan)
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
        repos=repos,
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


def _archive_followup(
    plan: PlanDraft,
    *,
    action: str,
    target_path: Path | None = None,
) -> Path:
    path = Path(plan.path)
    handled_dir = path.parent / "handled"
    handled_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_path = handled_dir / path.name
    if archive_path.exists():
        archive_path = handled_dir / f"{path.stem}-{stamp}{path.suffix}"
    try:
        content = path.read_text(encoding="utf-8").rstrip()
    except OSError:
        content = ""
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


def _state_planning_root(request: Request) -> Path:
    reader = request.app.state.reader
    state_root = getattr(reader, "state_root", None)
    if isinstance(state_root, Path):
        return state_root / "planning-drafts"
    base = (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    return Path(base) / "state" / "planning-drafts"


def _save_planning_text(
    request: Request,
    draft: IssueDraft,
    body: str,
    *,
    directory: str,
    suffix: str,
) -> Path:
    root = _planning_root(request, directory=directory)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = root / f"{stamp}-{_slug(draft.title)}-{suffix}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _planning_root(request: Request, *, directory: str = "planning-drafts") -> Path:
    reader = request.app.state.reader
    state_root = getattr(reader, "state_root", None)
    if isinstance(state_root, Path):
        return state_root.parent / directory
    base = (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    return Path(base) / directory


def _planning_memory_provider(request: Request):
    configured = getattr(request.app.state, "planning_memory_provider", None)
    if configured is not None:
        return configured
    if _env_disabled("ALFRED_PLANNING_MEMORY"):
        return None
    if not (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.environ.get("FLEET_BRAIN_HOST")
    ):
        return None
    try:
        from fleet_brain import FleetBrain

        return FleetBrain.from_env()
    except Exception:
        return None


def _planning_memory_writer(request: Request, *, provider=None):
    configured = getattr(request.app.state, "planning_memory_writer", None)
    if configured is not None:
        return configured
    return provider or _planning_memory_provider(request)


def _propose_planning_memory_candidate(
    request: Request,
    draft: IssueDraft,
    *,
    spec_path: Path,
    spec_body: str,
    memory_provider=None,
) -> tuple[str, ...]:
    if _env_disabled("ALFRED_PLANNING_MEMORY_CANDIDATES"):
        return ()
    writer = _planning_memory_writer(request, provider=memory_provider)
    if writer is None or not hasattr(writer, "propose_memory"):
        return ()
    body = _memory_candidate_body(draft)
    evidence = {
        "kind": "planning_spec",
        "path": str(spec_path),
        "title": draft.title,
        "readiness_chars": len(spec_body),
    }
    ids: list[str] = []
    for repo in draft.repos or ["planning"]:
        try:
            candidate_id = writer.propose_memory(
                agent="planning",
                repo=repo,
                topic="planning-spec",
                body=body,
                evidence=[evidence],
                source="planning-ui",
            )
        except TypeError:
            try:
                candidate = writer.propose_memory(
                    codename="planning",
                    repo=repo,
                    body=body,
                    tags=["planning", "spec"],
                    severity="info",
                    source="planning-ui",
                    evidence=str(spec_path),
                    confidence=0.72,
                )
                candidate_id = getattr(candidate, "id", candidate)
            except Exception:
                continue
        except Exception:
            continue
        if candidate_id is not None:
            ids.append(str(candidate_id))
    return tuple(ids)


def _memory_candidate_body(draft: IssueDraft) -> str:
    criteria = "; ".join(draft.acceptance_criteria[:3]) or "No acceptance criteria."
    repos = ", ".join(draft.repos) or "unspecified repo"
    return (
        f"Planning spec saved for {draft.title or 'untitled work'} across {repos}. "
        f"Acceptance gates: {criteria}"
    )


def _env_disabled(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"0", "false", "no", "off"}


def _planning_workdir(request: Request) -> Path:
    reader = request.app.state.reader
    state_root = getattr(reader, "state_root", None)
    if isinstance(state_root, Path):
        return state_root.parent
    base = (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    return Path(base)


def _repo_from_github_url(url: str) -> str:
    match = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)(?:/|$)", url)
    if not match:
        return ""
    return match.group(1)


def _same_origin_post(request: Request) -> bool:
    """Reject browser form posts from another origin while preserving CLI use."""
    expected_host = request.headers.get("host", "")
    for header in ("origin", "referer"):
        raw_value = request.headers.get(header)
        if not raw_value:
            continue
        parsed = urlparse(raw_value)
        if parsed.netloc != expected_host:
            return False
    return True


def _slug(value: str) -> str:
    text = value.strip().lower() or "draft"
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80] or "draft"
