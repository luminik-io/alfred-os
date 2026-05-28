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

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from planning_assistant import (
    PlanningAssistantResult,
    engine_refiner_from_env,
    refine_issue_draft,
)
from spec_helper import IssueDraft, assess_issue_draft


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
        return templates.TemplateResponse(
            request,
            "fleet.html",
            {
                "agents": agents,
                "total_today": sum(a.firings_today for a in agents),
                "reliability": reliability,
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

    @app.get("/planning", response_class=HTMLResponse)
    async def planning(request: Request) -> HTMLResponse:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "planning.html",
            {
                "draft": _empty_draft(),
                "result": None,
                "assistant_result": None,
                "chat_message": "",
                "saved_path": None,
                "spec_saved_path": None,
            },
        )

    @app.post("/planning", response_class=HTMLResponse)
    async def planning_submit(request: Request) -> HTMLResponse:
        templates = request.app.state.templates
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        draft = _draft_from_form(form)
        action = _first(form, "action")
        chat_message = _first(form, "chat_message")
        assistant_result: PlanningAssistantResult | None = None
        if action == "refine":
            assistant_result = refine_issue_draft(
                draft,
                [chat_message],
                refiner=engine_refiner_from_env(workdir=_planning_root(request)),
            )
            draft = assistant_result.draft
            result = assistant_result.readiness
        else:
            result = assess_issue_draft(draft)
        saved_path = None
        spec_saved_path = None
        if action == "save":
            saved_path = str(_save_issue_draft(request, draft, result.issue_body))
        elif action == "save_spec":
            assistant_result = assistant_result or refine_issue_draft(draft, [])
            spec_saved_path = str(
                _save_planning_text(
                    request,
                    draft,
                    assistant_result.spec_body,
                    directory="spec-drafts",
                    suffix="spec",
                )
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
            },
        )

    @app.get("/healthz", response_class=HTMLResponse)
    async def healthz() -> HTMLResponse:
        # Minimal liveness probe. Returns 200 with "ok" body, no template.
        return HTMLResponse("ok")


def _empty_draft() -> IssueDraft:
    return IssueDraft(title="")


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
    values = form.get(key) or [""]
    return values[0].strip()


def _lines(value: str) -> list[str]:
    return [line.strip().lstrip("- ").strip() for line in value.splitlines() if line.strip()]


def _save_issue_draft(request: Request, draft: IssueDraft, body: str) -> Path:
    return _save_planning_text(request, draft, body, directory="planning-drafts", suffix="issue")


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
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base) / directory


def _slug(value: str) -> str:
    text = value.strip().lower() or "draft"
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80] or "draft"
