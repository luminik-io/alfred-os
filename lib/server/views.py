"""Route handlers for ``alfred serve``.

Three views:

* ``GET /``                  Fleet status (HTMX auto-refresh every 10s).
* ``GET /firings``           Recent firings (optionally filtered by codename).
* ``GET /firings/{id}``      Single firing detail.

Two HTMX partials live behind the same URLs via the ``HX-Request`` header,
``htmx-only`` reduces the round trip to just the table body rather than
re-rendering the whole shell. Keeps the dashboard cheap to refresh.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse


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
                {"firing_id": firing_id},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "firing_detail.html",
            {"firing": record},
        )

    @app.get("/healthz", response_class=HTMLResponse)
    async def healthz() -> HTMLResponse:
        # Minimal liveness probe. Returns 200 with "ok" body, no template.
        return HTMLResponse("ok")
