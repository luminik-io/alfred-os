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
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from planning_assistant import (
    PlanningAssistantResult,
    engine_refiner_from_env,
    refine_issue_draft,
)
from spec_helper import IssueDraft


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

    @app.get("/api/plans/{plan_id}", response_class=JSONResponse)
    async def api_plan_detail(request: Request, plan_id: str) -> JSONResponse:
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        return JSONResponse(_jsonable(plan))

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


def _empty_draft() -> IssueDraft:
    return IssueDraft(title="")


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


def _planning_memory_provider(request: Request):
    configured = getattr(request.app.state, "planning_memory_provider", None)
    if configured is not None:
        return configured
    if _env_disabled("ALFRED_PLANNING_MEMORY"):
        return None
    if not (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("ALFRED_FLEET_BRAIN_DB")
        or _reader_uses_default_state_root(request)
    ):
        return None
    try:
        from memory.config import load_provider

        return load_provider()
    except Exception:
        return None


def _planning_memory_writer(request: Request, *, provider=None):
    configured = getattr(request.app.state, "planning_memory_writer", None)
    if configured is not None:
        return configured
    provider = provider or _planning_memory_provider(request)
    return _memory_candidate_writer(provider)


def _reader_uses_default_state_root(request: Request) -> bool:
    reader = request.app.state.reader
    state_root = getattr(reader, "state_root", None)
    if not isinstance(state_root, Path):
        return True
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    try:
        return state_root.expanduser().resolve() == (Path(base) / "state").expanduser().resolve()
    except OSError:
        return False


def _memory_candidate_writer(provider):
    if provider is None:
        return None
    brain = getattr(provider, "brain", None)
    if brain is not None and hasattr(brain, "propose_memory"):
        return brain
    if hasattr(provider, "propose_memory"):
        return provider
    for child in getattr(provider, "providers", ()) or ():
        writer = _memory_candidate_writer(child)
        if writer is not None:
            return writer
    return None


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
    evidence_json = json.dumps(evidence, sort_keys=True)
    ids: list[str] = []
    for repo in draft.repos or ["planning"]:
        try:
            candidate = writer.propose_memory(
                codename="planning",
                repo=repo,
                body=body,
                tags=["planning", "spec"],
                severity="info",
                source="planning-ui",
                evidence=evidence_json,
                confidence=0.72,
            )
        except TypeError:
            try:
                candidate = writer.propose_memory(
                    agent="planning",
                    repo=repo,
                    topic="planning-spec",
                    body=body,
                    evidence=[evidence],
                    source="planning-ui",
                )
            except Exception:
                continue
        except Exception:
            continue
        candidate_id = getattr(candidate, "id", candidate)
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
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base)


def _slug(value: str) -> str:
    text = value.strip().lower() or "draft"
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80] or "draft"
