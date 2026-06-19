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

import hmac
import json
import logging
import os
import re
import secrets
from contextlib import suppress
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from planning_assistant import (
    PlanningAssistantResult,
    engine_refiner_from_env,
    refine_issue_draft,
)
from slack_control import SlackControlHandler
from slack_trust import (
    SlackTrustStore,
    env_trusted_user_ids,
    normalize_slack_user_id,
    operator_user_id_from_env,
)
from spec_helper import IssueDraft, assess_issue_draft
from starlette.concurrency import run_in_threadpool

from server.plan_approvals import (
    DECISION_APPROVE,
    DECISION_DECLINE,
    issue_num_from_plan_id,
    write_decision,
)
from server.reader import FilesystemReader, PlanDraft

logger = logging.getLogger(__name__)

# Generic message returned to the client when a handler hits an unexpected
# failure. The exception detail (type, message, traceback) is logged
# server-side instead of being placed in the HTTP response body, so the
# localhost API never leaks internals to a same-origin page. Operators read the
# real cause in the runtime logs.
_GENERIC_ERROR = "internal error"

_MEMORY_ID_RE = re.compile(r"^[0-9]{1,18}$")
_LOCAL_CLIENT_USER_ID = "ULOCALCLIENT"

# Header the native client attaches to every state-mutating POST. It carries
# the per-launch server token written under ``state/server-token`` so a
# drive-by same-origin localhost page cannot arm work or mutate trust/plan
# state on the operator's behalf.
SERVER_TOKEN_HEADER = "X-Alfred-Token"
SERVER_TOKEN_FORM_FIELD = "_token"
_SERVER_TOKEN_FILENAME = "server-token"


def server_token_path(state_root: Path) -> Path:
    """Path to the per-launch server token under a state root."""
    return Path(state_root) / _SERVER_TOKEN_FILENAME


def ensure_server_token(state_root: Path) -> str:
    """Generate (once per launch) and persist the mutation token.

    The token is written to ``state/server-token`` with ``0600`` perms so only
    the operator's account can read it. A fresh token is minted on every server
    start, which invalidates any token a previously-running instance handed out.
    """
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path = server_token_path(root)
    # Write to a temp file then atomically replace so a reader never sees a
    # half-written token. Apply 0600 before the rename so the secret is never
    # briefly world-readable.
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    with suppress(OSError):
        os.chmod(path, 0o600)
    return token


def _read_server_token(state_root: Path) -> str | None:
    try:
        token = server_token_path(state_root).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token or None


def _authorized_mutation(request: Request, *, form_token: str | None = None) -> bool:
    """Require the per-launch token for a state-mutating POST.

    The token must match the value persisted at server start with a
    constant-time compare. JSON/Tauri clients present it via the
    ``SERVER_TOKEN_HEADER`` header; server-rendered HTML forms (which cannot
    set a custom header) present it via a hidden ``_token`` field, which the
    GET handler embeds from the same on-disk token. Either path is a valid
    synchronizer token: a cross-origin attacker cannot read the GET response
    body to learn the token, so this still defeats CSRF. ``_same_origin_post``
    remains an additional layer; together they stop a drive-by same-origin
    localhost caller (which cannot read the operator's ``0600`` token file)
    from mutating fleet state.
    """
    expected = _read_server_token(_state_root(request))
    if not expected:
        # No token on disk means the gate cannot be satisfied. Fail closed so a
        # missing/unreadable token never silently downgrades to same-origin-only.
        return False
    presented = request.headers.get(SERVER_TOKEN_HEADER) or form_token
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def _form_token_from_body(raw: bytes) -> str:
    try:
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return ""
    return _first(form, SERVER_TOKEN_FORM_FIELD)


# Origins the packaged Tauri webview presents. A built .app loads its bundle
# from a custom scheme, so its `Origin` is NOT the localhost server's host:
# macOS/Linux serve from ``tauri://localhost`` and Windows (WebView2) from
# ``http(s)://tauri.localhost``. The dev/browser preview is same-origin through
# the Vite proxy, but a direct localhost hit also needs to be allowed for the
# streaming routes the webview talks to directly (it cannot use the buffered
# Tauri JSON bridge for an incremental body).
_TAURI_WEBVIEW_ORIGINS = frozenset(
    {
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    }
)
_LOCALHOST_STREAM_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def _streaming_origin_allowed(request: Request) -> str | None:
    """Return the request Origin if it may talk to a streaming route, else None.

    Allowed origins are: the packaged Tauri webview schemes, any localhost dev
    origin (``http://127.0.0.1:PORT`` / ``http://localhost:PORT``), and a
    same-origin request (Origin host == the server's Host). A missing Origin is
    treated as allowed (a same-origin ``EventSource`` GET / a CLI client omit
    it); the converse-stream POST is still gated on the per-launch token, which
    is the real CSRF defense, so a bare cross-origin POST without the token is
    rejected regardless.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return None
    if origin in _TAURI_WEBVIEW_ORIGINS:
        return origin
    parsed = urlparse(origin)
    if parsed.hostname in _LOCALHOST_STREAM_HOSTS:
        return origin
    if parsed.netloc and parsed.netloc == request.headers.get("host", ""):
        return origin
    return None


def _streaming_cors_headers(request: Request, base: dict[str, str] | None = None) -> dict[str, str]:
    """Augment ``base`` with CORS headers when the Origin is an allowed webview.

    Echoes the exact Origin (never ``*``) so a credentialed cross-origin fetch
    from the packaged webview can read the stream, and advertises the token
    header on the preflight. Same-origin / no-Origin requests get no CORS
    headers (none are needed), keeping the surface minimal.
    """
    headers = dict(base or {})
    allowed = _streaming_origin_allowed(request)
    if allowed is not None:
        headers["Access-Control-Allow-Origin"] = allowed
        headers["Vary"] = "Origin"
        headers["Access-Control-Allow-Headers"] = f"{SERVER_TOKEN_HEADER}, content-type"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return headers


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
            {
                "plan": plan,
                "server_token": _read_server_token(_state_root(request)) or "",
            },
        )

    @app.post("/plans/{plan_id}/convert-followup")
    async def convert_followup(request: Request, plan_id: str):
        if not _same_origin_post(request):
            return HTMLResponse("Forbidden", status_code=403)
        if not _authorized_mutation(
            request, form_token=_form_token_from_body(await request.body())
        ):
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
        if not _authorized_mutation(
            request, form_token=_form_token_from_body(await request.body())
        ):
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
                    # Today's aggregate spend + ok/fail counts, rolled up from
                    # the same per-agent spend-YYYY-MM-DD.json ledgers metrics
                    # reads. Lets the Review cost strip show real spend instead
                    # of "not surfaced". Stays an honest empty rollup (all
                    # zeros, spend_usd null) when no ledgers exist today.
                    "metrics": _today_cost_rollup(reader),
                    # The active intake profile (server env only), so Compose can
                    # adapt its copy/behavior to plain mode. Defaults to
                    # "technical" when ALFRED_INTAKE_PROFILE is unset.
                    "intake_profile": _active_intake_profile_name(),
                    # Planning context from guided setup. The client can seed
                    # plans from this instead of asking the operator to type an
                    # owner/repo slug Alfred already knows.
                    "setup_repos": _selected_setup_repos_payload(),
                }
            )
        )

    @app.get("/api/schedule", response_class=JSONResponse)
    async def api_schedule(request: Request) -> JSONResponse:
        """Upcoming scheduled runs read from ``launchd/agents.conf``.

        ``cron:`` rows carry a computed ``next_fire_at`` (local ISO-8601);
        ``interval:`` rows carry only a ``cadence`` string ("every 15m")
        because the read-only server has no trustworthy last-fired anchor to
        compute the next fire from. Never 500s: an unreadable/missing conf
        degrades to an empty ``runs`` list so the lane shows an honest empty
        state.
        """
        from server.schedule import upcoming_runs

        try:
            runs = upcoming_runs()
        except Exception:  # never break the client on a parse failure
            logger.exception("api_schedule: failed to read upcoming runs")
            return JSONResponse({"runs": [], "error": _GENERIC_ERROR})
        return JSONResponse(_jsonable({"runs": [run.to_dict() for run in runs]}))

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

    @app.get("/api/shipped", response_class=JSONResponse)
    async def api_shipped(request: Request) -> JSONResponse:
        """Kanban feed: what shipped / is in progress / is queued.

        Human-readable cards (title + repo + age + author), not bare links, so
        the native client and the Slack board render the same payload. Never
        500s: a GitHub/auth failure returns an ``error`` field with empty
        columns.
        """
        from shipped_board import DEFAULT_LOOKBACK_DAYS, build_board, resolve_repos

        params = parse_qs(urlparse(str(request.url)).query)
        try:
            days = int((params.get("days") or [str(DEFAULT_LOOKBACK_DAYS)])[0])
        except (TypeError, ValueError):
            days = DEFAULT_LOOKBACK_DAYS
        days = max(1, min(days, 90))
        repos = (params.get("repos") or [""])[0]
        repo_list = [r.strip() for r in repos.split(",") if r.strip()] or None
        # Demo cards are opt-in: the live board shows only real Alfred work
        # unless the client explicitly asks for the seeded sample via ?demo=1.
        include_demo = (params.get("demo") or ["0"])[0].strip().lower() in (
            "1",
            "true",
            "yes",
        )
        try:

            def _build() -> dict[str, Any]:
                return build_board(resolve_repos(repo_list), days=days, include_demo=include_demo)

            board = await run_in_threadpool(_build)
        except Exception:  # never break the client on a board failure
            logger.exception("api_shipped: failed to build board")
            return JSONResponse(
                _jsonable(
                    {
                        "columns": {"queued": [], "in_progress": [], "shipped": []},
                        "counts": {"queued": 0, "in_progress": 0, "shipped": 0},
                        "repos": repo_list or [],
                        "lookback_days": days,
                        "error": _GENERIC_ERROR,
                    }
                )
            )
        return JSONResponse(_jsonable(board))

    @app.get("/api/usage", response_class=JSONResponse)
    async def api_usage(request: Request) -> JSONResponse:
        """Real subscription-usage headroom from local Claude/Codex logs.

        Reports the active Claude 5-hour rolling-window token usage, time to
        reset, a simple burn projection, and a latest-day Codex row. The
        per-token dollar figure is meaningless under a Max/Pro subscription (and
        $0 for Codex), so this is usage headroom rather than billed spend.

        Reads local JSONL logs in a worker thread so filesystem work never
        stalls the event loop, and degrades to ``{"available": false, "error":
        ...}`` when both sources fail.
        """
        from starlette.concurrency import run_in_threadpool

        from server.usage import build_usage, unavailable_usage_payload

        try:
            payload = await run_in_threadpool(build_usage)
        except Exception:  # never break the client on a usage failure
            logger.exception("api_usage: failed to build usage payload")
            return JSONResponse(unavailable_usage_payload(_GENERIC_ERROR))
        return JSONResponse(_jsonable(payload))

    @app.get("/api/usage/providers", response_class=JSONResponse)
    async def api_usage_providers(request: Request) -> JSONResponse:
        """Provider-normalized usage meters: ``{"claude": {...}, "codex": {...}}``.

        A flat re-projection of ``/api/usage`` that surfaces each engine's
        5-hour and weekly rolling windows under uniform keys (``used_percent``,
        ``remaining_percent``, ``reset_at``, ``minutes_to_reset``). Alfred drives
        Claude Code and Codex through their local subscription CLIs, so there is
        no billing API: figures come straight from the CLIs' own local state
        files. A provider whose local state cannot be read degrades to
        ``available: false`` with an ``unavailable_reason`` rather than guessing.

        Reads run in a worker thread so filesystem work never stalls the event
        loop, and any failure degrades to an honest both-unavailable shape.
        """
        from starlette.concurrency import run_in_threadpool

        from server.usage import build_provider_usage

        try:
            payload = await run_in_threadpool(build_provider_usage)
        except Exception:  # never break the client on a usage failure
            logger.exception("api_usage_providers: failed to build provider usage")
            payload = {
                "available": False,
                "error": _GENERIC_ERROR,
                "claude": {
                    "available": False,
                    "five_hour": None,
                    "weekly": None,
                    "unavailable_reason": _GENERIC_ERROR,
                },
                "codex": {
                    "available": False,
                    "five_hour": None,
                    "weekly": None,
                    "unavailable_reason": _GENERIC_ERROR,
                },
            }
        return JSONResponse(_jsonable(payload))

    @app.post("/api/queue", response_class=JSONResponse)
    async def api_queue(request: Request) -> JSONResponse:
        """Operator queue control: assign, arm, hold, or close an issue.

        Body: ``{"repo": "owner/repo", "number": 12, "action": "assign"|"queue"|"hold"|"done"}``.
        ``assign`` chooses Batman or Lucius and labels the issue for that lane;
        callers may pass ``target_agent`` / ``agent`` as ``batman`` or ``lucius``
        to override the heuristic without bypassing safety gates;
        ``queue`` labels the issue ``agent:implement``; ``hold`` labels it
        ``do-not-pickup`` so no agent claims it; ``done`` closes the issue
        using GitHub's native closed state (no new label taxonomy).

        Each action mutates fleet/repo state, so all require the operator's
        per-launch token (the ``X-Alfred-Token`` header), not just a
        same-origin request. A drive-by localhost page cannot read the
        ``0600`` token file, so it can never arm or close work.
        """
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        from issue_assignment import assign_issue
        from issue_queue import QUEUE_ACTIONS, close_issue, set_issue_pickup

        repo = str(body.get("repo") or "").strip()
        action = str(body.get("action") or "").strip().lower()
        target_agent = str(body.get("target_agent") or body.get("agent") or "").strip()
        try:
            number = int(body.get("number"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "number must be an integer"}, status_code=400)
        allowed_actions = set(QUEUE_ACTIONS) | {"assign"}
        if action not in allowed_actions:
            return JSONResponse(
                {"error": "action must be 'assign', 'queue', 'hold', or 'done'"},
                status_code=400,
            )
        if action == "done":
            ok, detail = close_issue(repo, number)
            response_target_agent = ""
        elif action == "assign":
            assignment = assign_issue(repo, number, target_agent=target_agent)
            ok, detail = assignment.ok, assignment.detail
            response_target_agent = assignment.decision.agent or target_agent or "auto"
            if not ok:
                detail = assignment.error or detail
        else:
            ok, detail = set_issue_pickup(repo, number, hold=(action == "hold"))
            response_target_agent = ""
        if not ok:
            return JSONResponse({"error": detail}, status_code=400)
        payload = {
            "ok": True,
            "repo": repo,
            "number": number,
            "action": action,
            "detail": detail,
        }
        if response_target_agent:
            payload["target_agent"] = response_target_agent
        return JSONResponse(payload)

    @app.get("/api/setup/status", response_class=JSONResponse)
    async def api_setup_status(request: Request) -> JSONResponse:
        """First-run bootstrap status for the Set up tab.

        Read-only. Surfaces GitHub auth, installed engine CLIs (claude/codex),
        the watched-repo selection, whether a demo is seeded, and a ``ready``
        golden-path flag: gh authed + an engine + a repo, with no AWS or Slack
        requirement.
        """
        from server import setup as setup_mod

        try:
            payload = await run_in_threadpool(setup_mod.bootstrap_status)
        except Exception:  # never break the client on a probe failure
            logger.exception("api_setup_status: bootstrap probe failed")
            return JSONResponse(
                {
                    "github": {"ok": False, "account": None, "detail": _GENERIC_ERROR},
                    "engines": [],
                    "engine_ready": False,
                    "repos": {"selected": [], "count": 0, "keys": []},
                    "demo": {"present": False},
                    "ready": False,
                    "error": _GENERIC_ERROR,
                }
            )
        return JSONResponse(_jsonable(payload))

    @app.get("/api/setup/repos", response_class=JSONResponse)
    async def api_setup_repos(request: Request) -> JSONResponse:
        """List GitHub repos for the onboarding repo picker."""
        from server import setup as setup_mod

        params = parse_qs(urlparse(str(request.url)).query)
        try:
            limit = int((params.get("limit") or ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        try:
            payload = await run_in_threadpool(setup_mod.list_owner_repos, limit)
        except Exception:
            logger.exception("api_setup_repos: failed to list owner repos")
            return JSONResponse({"repos": [], "selected": [], "error": _GENERIC_ERROR})
        return JSONResponse(_jsonable(payload))

    @app.post("/api/setup/repos", response_class=JSONResponse)
    async def api_setup_select_repos(request: Request) -> JSONResponse:
        """Persist the repos Alfred may work in.

        Body: ``{"repos": ["owner/repo", ...]}``. Writes the queue + shipped
        allowlist keys to ``$ALFRED_HOME/.env`` and mirrors them into the live
        process so the new scope is effective without a restart.
        """
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body, error_response = await _read_json_body(request)
        if error_response is not None:
            return error_response
        raw_repos = body.get("repos")
        if not isinstance(raw_repos, list):
            return JSONResponse(
                {"error": "repos must be a list of owner/repo slugs"},
                status_code=400,
            )
        from server import setup as setup_mod

        try:
            result = setup_mod.persist_selected_repos(raw_repos)
        except (OSError, ValueError):
            logger.exception("api_setup_select_repos: failed to persist repo selection")
            return JSONResponse(
                {"error": "could not persist repo selection"},
                status_code=400,
            )
        result["ok"] = True
        return JSONResponse(_jsonable(result))

    @app.get("/api/setup/playbooks", response_class=JSONResponse)
    async def api_setup_playbooks(request: Request) -> JSONResponse:
        """Starter playbooks the client offers as first jobs."""
        from server import setup as setup_mod

        rows = [
            {"key": p["key"], "title": p["title"], "summary": p["summary"]}
            for p in setup_mod.STARTER_PLAYBOOKS
        ]
        return JSONResponse({"playbooks": rows})

    @app.post("/api/setup/playbook", response_class=JSONResponse)
    async def api_setup_compose_playbook(request: Request) -> JSONResponse:
        """Compose a starter playbook into a saved request draft."""
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body, error_response = await _read_json_body(request)
        if error_response is not None:
            return error_response
        from server import setup as setup_mod

        key = str(body.get("key") or "").strip()
        playbook = setup_mod.playbook_by_key(key)
        if playbook is None:
            return JSONResponse({"error": "unknown playbook key"}, status_code=400)
        return _compose_playbook_draft(request, playbook, body.get("repos"))

    @app.post("/api/setup/demo", response_class=JSONResponse)
    async def api_setup_seed_demo(request: Request) -> JSONResponse:
        """Seed local demo cards so an empty board teaches the workflow."""
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from server import setup as setup_mod

        try:
            result = setup_mod.seed_demo(_state_root(request))
        except OSError:
            logger.exception("api_setup_seed_demo: failed to seed demo cards")
            return JSONResponse({"error": "could not seed demo"}, status_code=400)
        return JSONResponse(_jsonable(result))

    @app.post("/api/setup/demo/clear", response_class=JSONResponse)
    async def api_setup_clear_demo(request: Request) -> JSONResponse:
        """Remove seeded demo cards. Token-gated and idempotent."""
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        from server import setup as setup_mod

        result = setup_mod.clear_demo(_state_root(request))
        return JSONResponse(_jsonable(result))

    @app.get("/api/slack/trusted-users", response_class=JSONResponse)
    async def api_slack_trusted_users(request: Request) -> JSONResponse:
        store = SlackTrustStore.from_state_root(_state_root(request))
        return JSONResponse(
            store.snapshot(
                operator_user_id=operator_user_id_from_env(),
                env_trusted_user_ids=env_trusted_user_ids(),
            ).to_dict()
        )

    @app.post("/api/slack/trusted-users", response_class=JSONResponse)
    async def api_slack_trust_user(request: Request) -> JSONResponse:
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        user_id = normalize_slack_user_id(body.get("user_id"))
        if user_id is None:
            return JSONResponse({"error": "user_id must be a Slack user id"}, status_code=400)
        store = SlackTrustStore.from_state_root(_state_root(request))
        added, _user = store.add(user_id, added_by="local-client")
        snapshot = store.snapshot(
            operator_user_id=operator_user_id_from_env(),
            env_trusted_user_ids=env_trusted_user_ids(),
        ).to_dict()
        snapshot["added"] = added
        return JSONResponse(snapshot)

    @app.post("/api/slack/trusted-users/{user_id}/remove", response_class=JSONResponse)
    async def api_slack_untrust_user(request: Request, user_id: str) -> JSONResponse:
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        normalized = normalize_slack_user_id(user_id)
        if normalized is None:
            return JSONResponse({"error": "user_id must be a Slack user id"}, status_code=400)
        store = SlackTrustStore.from_state_root(_state_root(request))
        removed = store.remove(normalized)
        snapshot = store.snapshot(
            operator_user_id=operator_user_id_from_env(),
            env_trusted_user_ids=env_trusted_user_ids(),
        ).to_dict()
        snapshot["removed"] = removed
        return JSONResponse(snapshot)

    @app.post("/api/conversation/control", response_class=JSONResponse)
    async def api_conversation_control(request: Request) -> JSONResponse:
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        actor_user_id = _local_conversation_actor(body.get("actor_user_id"))
        handler = SlackControlHandler(
            trust_store=SlackTrustStore.from_state_root(_state_root(request)),
            operator_user_id=operator_user_id_from_env() or actor_user_id,
            state_root=_state_root(request),
            plan_reader=request.app.state.reader,
            memory_provider=_planning_memory_provider(request),
        )
        result = handler.handle(text, trusted=True, actor_user_id=actor_user_id)
        return JSONResponse(
            {
                "handled": result.handled,
                "action": result.action,
                "text": result.text,
                "detail": result.detail,
                "actor_user_id": actor_user_id,
            }
        )

    @app.get("/api/memory/candidates", response_class=JSONResponse)
    async def api_memory_candidates(
        request: Request,
        status: str = "candidate",
        limit: int = 50,
    ) -> JSONResponse:
        if status not in {
            "candidate",
            "pending",
            "validated",
            "promoted",
            "rejected",
            "all",
        }:
            return JSONResponse({"error": "unknown memory candidate status"}, status_code=400)
        brain, error = _memory_brain(request, require_existing=True)
        if brain is None:
            return JSONResponse({"rows": [], "error": error})
        status_filter = _memory_status_filter(status)
        try:
            rows = brain.list_memory_candidates(
                status=status_filter,
                limit=min(max(1, limit), 200),
            )
        except Exception:  # pragma: no cover - local bridge can be down
            logger.exception("api_memory_candidates: failed to list candidates")
            return JSONResponse({"rows": [], "error": _GENERIC_ERROR})
        return JSONResponse({"rows": [_candidate_to_api(row) for row in rows]})

    @app.post("/api/memory/candidates/{candidate_id}/promote", response_class=JSONResponse)
    async def api_promote_memory_candidate(request: Request, candidate_id: str) -> JSONResponse:
        return await _api_memory_candidate_action(request, candidate_id, action="promote")

    @app.post("/api/memory/candidates/{candidate_id}/reject", response_class=JSONResponse)
    async def api_reject_memory_candidate(request: Request, candidate_id: str) -> JSONResponse:
        return await _api_memory_candidate_action(request, candidate_id, action="reject")

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

    @app.get("/api/firings/{firing_id}/tail")
    async def api_firing_tail(
        request: Request,
        firing_id: str,
        offset: int = 0,
        poll: int = 0,
    ) -> Any:
        """Live-tail a running firing's transcript as it grows (#41).

        This is a READ over the on-disk transcript JSONL the runtime tees per
        firing, so it is an open GET like the other read routes (no token), and
        the client consumes it via ``EventSource``. Two transports share the
        same offset reader:

        * Default: a Server-Sent-Events stream that appends new whole lines as
          they land and closes with a ``done`` event once the firing completes
          (or a wall-clock ceiling is hit).
        * ``?poll=1&offset=N``: a single JSON snapshot
          (``{found, offset, lines, done}``) for clients that cannot hold an
          ``EventSource`` open, so the live tail degrades to plain polling
          instead of failing.

        The client always retains its existing 60s firing poll, so a missing
        route (older server) or a stream error never regresses the log view.
        """
        from server import streaming

        state_root = _state_root(request)
        start_offset = max(0, int(offset))
        # The packaged webview reaches this open GET cross-origin (its bundle
        # loads from tauri://localhost), and a cross-origin EventSource is still
        # subject to CORS, so echo the allowed Origin. No token is required: the
        # tail is a read over the on-disk transcript, like the other GET routes.
        cors = _streaming_cors_headers(request)
        if poll:
            snapshot = await run_in_threadpool(
                streaming.tail_transcript_chunk,
                state_root,
                firing_id,
                offset=start_offset,
            )
            return JSONResponse(snapshot, headers=cors)
        generator = streaming.tail_transcript_sse(state_root, firing_id, start_offset=start_offset)
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers=_streaming_cors_headers(
                request,
                {
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            ),
        )

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
        if not _same_origin_post(request) or not _authorized_mutation(request):
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
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if plan.source != "followup":
            return JSONResponse({"error": "plan is not a follow-up"}, status_code=400)
        archived_path = _archive_followup(plan, action="handled")
        return JSONResponse({"archived_path": str(archived_path)})

    @app.post("/api/plans/{plan_id}/discard", response_class=JSONResponse)
    async def api_discard_plan(request: Request, plan_id: str) -> JSONResponse:
        """Discard a local planning draft by archiving, never hard-deleting it."""
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        draft_id = _safe_planning_draft_id(plan_id)
        if draft_id is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        try:
            result = await run_in_threadpool(
                _discard_planning_draft_group,
                _state_root(request),
                draft_id,
            )
        except FileNotFoundError:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        except Exception:  # never let an IO edge crash the server
            logger.exception("api_discard_plan: failed to archive planning draft")
            return JSONResponse({"error": _GENERIC_ERROR}, status_code=500)
        return JSONResponse(result)

    @app.post("/api/plans/{plan_id}/decision", response_class=JSONResponse)
    async def api_plan_decision(request: Request, plan_id: str) -> JSONResponse:
        """Record an in-app go/no-go on a genuine Batman plan.

        Writes the same ``{issue_num}.approved`` / ``.rejected`` marker
        Batman's approval gate watches (see ``lib.batman``), so the operator
        can approve or decline without a Slack round-trip and Batman consumes
        it through the real go/no-go path. Token-gated via
        ``_authorized_mutation`` and same-origin so a
        drive-by localhost page cannot arm or stop work on the operator's
        behalf.
        """
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body, error_response = await _read_json_body(request)
        if error_response is not None:
            return error_response
        decision = str(body.get("decision") or "").strip().lower()
        if decision not in (DECISION_APPROVE, DECISION_DECLINE):
            return JSONResponse(
                {"error": "decision must be 'approve' or 'decline'"},
                status_code=400,
            )
        plan = request.app.state.reader.get_plan(plan_id)
        if plan is None:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        if plan.source != "batman":
            return JSONResponse(
                {"error": "only Batman go/no-go plans can be decided here"},
                status_code=400,
            )
        issue_num = issue_num_from_plan_id(plan.plan_id)
        if issue_num is None:
            return JSONResponse(
                {"error": "plan id has no issue number to signal"},
                status_code=400,
            )
        reason = str(body.get("reason") or "").strip()
        marker_path = write_decision(_state_root(request), issue_num, decision, reason=reason)
        return JSONResponse(
            {
                "plan_id": plan.plan_id,
                "issue_number": issue_num,
                "decision": decision,
                "status": "approved" if decision == DECISION_APPROVE else "declined",
                "marker_path": str(marker_path),
            }
        )

    @app.post("/api/plans/{plan_id}/file-issue", response_class=JSONResponse)
    async def api_file_plan_issue(request: Request, plan_id: str) -> JSONResponse:
        """File labeled GitHub issue work from a ready local planning draft.

        This is the native-client issue filing route for Plan work. It does not run an
        agent, touch a worktree, or bypass the fleet gates: it creates one
        ``agent:implement`` issue, then the normal Alfred queue decides when
        and how to claim it. The route is
        same-origin and token-gated like other local mutations, and it is
        idempotent via the saved draft's ``bridge.issue_url`` field.
        """
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            result = await run_in_threadpool(
                _file_planning_draft_issue,
                _state_root(request),
                plan_id,
            )
        except FileNotFoundError:
            return JSONResponse({"error": "plan not found"}, status_code=404)
        except ValueError:
            # ValueErrors here are rejection reasons (unsafe id, unreadable
            # draft, failed conversion). Log the detail server-side and return a
            # generic 400 so the response never carries exception text; the 400
            # status (the client's "filing rejected" contract) is unchanged.
            logger.exception("api_file_plan_issue: plan draft rejected")
            return JSONResponse(
                {"error": "could not file plan issue from this draft"},
                status_code=400,
            )
        except Exception:  # never let a gh/IO edge crash the server
            logger.exception("api_file_plan_issue: failed to file plan issue")
            return JSONResponse(
                {"error": _GENERIC_ERROR},
                status_code=500,
            )
        if not result.get("ok"):
            return JSONResponse(result, status_code=400)
        return JSONResponse(result)

    @app.post("/api/plans/draft", response_class=JSONResponse)
    async def api_compose_draft(request: Request) -> JSONResponse:
        if not _same_origin_post(request) or not _authorized_mutation(request):
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
        if not base_draft.repos:
            setup_scope = _selected_setup_repos_for_scope()
            if setup_scope:
                base_draft = replace(base_draft, repos=setup_scope)

        if not text and prior_payload is None and not _draft_has_signal(base_draft):
            return JSONResponse(
                {"error": "describe the work in the text field before drafting"},
                status_code=400,
            )

        refiner = engine_refiner_from_env(workdir=_planning_workdir(request)) if text else None
        messages = _compose_draft_messages(text, base_draft)
        synthesized_plain_intent = bool(messages and text and messages[0] != text)
        memory_provider = _planning_memory_provider(request)
        assistant_result: PlanningAssistantResult = refine_issue_draft(
            base_draft,
            messages,
            refiner=refiner if messages else None,
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
                "summary": _compose_draft_response_summary(
                    assistant_result,
                    synthesized_plain_intent=synthesized_plain_intent,
                ),
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

    @app.post("/api/compose/converse", response_class=JSONResponse)
    async def api_compose_converse(request: Request) -> JSONResponse:
        """Run one turn of the conversational, repo-grounded spec-builder.

        Body: ``{ draft_id?, context_repos?: [owner/repo], repos?: [owner/repo], plain?: bool,
        messages: [{role, content}] }``. Each call runs ONE assistant turn via
        the agent-engine dispatch, seeded with the spec-interrogator system
        prompt + repo grounding + code map. ``plain`` toggles jargon-free
        coaching for this turn (it wins over the ALFRED_INTAKE_PROFILE env
        default); the structured draft and readiness are unchanged either way.
        Persists the accumulating spec and conversation as a compose planning
        draft so it shows in Plans and threads into the RequestThread.

        Returns ``{ reply, draft, readiness:{score, ready, missing[]}, done }``.
        Degrades with a 503 when no live engine is configured (the off-Tauri
        browser preview never calls this; it stays on the one-shot rubric form).
        """
        if not _same_origin_post(request) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        return _run_compose_converse(request, body)

    @app.post("/api/compose/converse/stream")
    async def api_compose_converse_stream(request: Request) -> Any:
        """Token-stream one converse turn so chat renders as the model writes (#36).

        Same body + auth + persistence as ``/api/compose/converse`` (a
        token-gated mutation). Because this is a POST it cannot ride
        ``EventSource`` (which is GET-only and cannot send ``X-Alfred-Token``);
        the native client consumes it via ``fetch()`` + a streamed
        ``ReadableStream``, which carries the token header. The response is an
        SSE byte stream:

        * ``open``   once, when the turn starts.
        * ``token``  each new assistant text fragment teed to the transcript
          (BEST EFFORT progress; a model that does not tee interim text simply
          emits none and the reply lands whole on ``result``).
        * ``result`` once, the full reconciled ``ConverseResponse`` (also
          persisted as a compose draft, exactly like the non-streaming route).
        * ``error``  when no live engine is configured or the turn failed, so
          the client falls back to the non-streaming converse / one-shot form.

        Auth is checked BEFORE the stream opens so a forbidden caller gets a
        clean 403 JSON, never a half-open stream.

        Unlike the buffered mutations, the packaged Tauri webview reaches this
        route cross-origin (its bundle loads from ``tauri://localhost``, not the
        server's Host), and it must to stream an incremental body the buffered
        Tauri JSON bridge cannot carry. So instead of strict same-origin we
        require (a) an allowed webview/localhost Origin AND (b) the per-launch
        token via ``_authorized_mutation`` (constant-time compare). The token is
        the real CSRF defense: a drive-by page cannot read the operator's
        ``0600`` token file, so a bare cross-origin POST without it is rejected.
        """
        cors = _streaming_cors_headers(request)
        if (
            _streaming_origin_allowed(request) is None and not _same_origin_post(request)
        ) or not _authorized_mutation(request):
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=cors)
        try:
            body = json.loads((await request.body()).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(
                {"error": "request body must be JSON"}, status_code=400, headers=cors
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "request body must be a JSON object"},
                status_code=400,
                headers=cors,
            )
        return _stream_compose_converse(request, body)

    @app.options("/api/compose/converse/stream")
    async def api_compose_converse_stream_preflight(request: Request) -> Response:
        """CORS preflight for the cross-origin converse stream (#36).

        The packaged webview's token-bearing POST is a non-simple request, so
        the browser sends an ``OPTIONS`` preflight first. Answer it for allowed
        webview/localhost origins; this carries no body and runs no turn, so it
        is not token-gated (the actual POST still is).
        """
        return Response(status_code=204, headers=_streaming_cors_headers(request))

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
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _candidate_to_api(candidate: Any) -> dict[str, Any]:
    payload = _jsonable(asdict(candidate) if is_dataclass(candidate) else candidate)
    if isinstance(payload, dict):
        if payload.get("agent") and not payload.get("codename"):
            payload["codename"] = payload["agent"]
        if payload.get("status") == "pending":
            payload["status"] = "candidate"
        elif payload.get("status") == "promoted":
            payload["status"] = "validated"
        if payload.get("id") is not None:
            payload["id"] = str(payload["id"])
        if not isinstance(payload.get("tags"), list):
            payload["tags"] = []
        if not payload.get("severity"):
            payload["severity"] = "info"
        if not payload.get("source"):
            payload["source"] = "memory"
        if "source_firing_id" not in payload:
            payload["source_firing_id"] = None
        if payload.get("confidence") is None:
            payload["confidence"] = 0.5
        evidence = payload.get("evidence")
        if evidence is None:
            payload["evidence"] = ""
        elif not isinstance(evidence, str):
            payload["evidence"] = json.dumps(evidence, sort_keys=True)
        return payload
    return {}


def _memory_status_filter(status: str) -> str | None:
    if status == "all":
        return None
    if status == "candidate":
        return "pending"
    if status == "validated":
        return "promoted"
    return status


def _memory_brain(
    _request: Request,
    *,
    require_existing: bool,
) -> tuple[Any | None, str | None]:
    try:
        from fleet_brain import FleetBrain

        brain = FleetBrain.from_env()
        if require_existing:
            brain.health()
        return brain, None
    except Exception:  # pragma: no cover - defensive local API path
        logger.exception("_memory_brain: fleet brain unavailable")
        return None, _GENERIC_ERROR


async def _api_memory_candidate_action(
    request: Request,
    candidate_id: str,
    *,
    action: str,
) -> JSONResponse:
    if not _same_origin_post(request) or not _authorized_mutation(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not _MEMORY_ID_RE.fullmatch(candidate_id):
        return JSONResponse({"error": "memory candidate id is invalid"}, status_code=400)
    body, error_response = await _read_json_body(request)
    if error_response is not None:
        return error_response
    note = str(body.get("note") or "").strip()
    reviewer = str(body.get("reviewer") or "local-client").strip() or "local-client"
    brain, error = _memory_brain(request, require_existing=True)
    if brain is None:
        return JSONResponse({"error": error or "fleet brain unavailable"}, status_code=500)
    try:
        if action == "promote":
            lesson = brain.promote_memory_candidate(
                int(candidate_id),
                reviewer=reviewer,
                note=note,
            )
            if lesson is None:
                return JSONResponse({"error": "memory candidate not found"}, status_code=404)
            return JSONResponse(
                {
                    "candidate_id": candidate_id,
                    "lesson_id": f"lesson:memory_candidate:{candidate_id}",
                    "status": "validated",
                    "codename": lesson.get("agent"),
                    "repo": lesson.get("repo"),
                }
            )
        if action == "reject":
            candidate = brain.reject_memory_candidate(
                int(candidate_id),
                reviewer=reviewer,
                note=note,
            )
            if candidate is None:
                return JSONResponse({"error": "memory candidate not found"}, status_code=404)
            return JSONResponse(_candidate_to_api(candidate))
    except ValueError as exc:
        # FleetBrain.promote_memory_candidate / reject_memory_candidate raise
        # ValueError both for an unknown candidate id (a missing resource) and
        # for a found-but-inapplicable action. Distinguish on the message
        # INTERNALLY (it is never echoed) so a stale id stays a clean 404 while a
        # real validation rejection is a 400. Keep the generic body either way so
        # no exception detail leaks (py/stack-trace-exposure).
        logger.exception("memory candidate %s action %r failed", candidate_id, action)
        if "unknown candidate" in str(exc):
            return JSONResponse({"error": "memory candidate not found"}, status_code=404)
        return JSONResponse({"error": _GENERIC_ERROR}, status_code=400)
    return JSONResponse({"error": "unknown memory action"}, status_code=400)


async def _read_json_body(
    request: Request,
) -> tuple[dict[str, Any], JSONResponse | None]:
    raw = await request.body()
    if not raw:
        return {}, None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, JSONResponse({"error": "request body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return {}, JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    return body, None


def _today_cost_rollup(reader: Any) -> dict[str, Any]:
    """Aggregate today's spend + ok/fail counts across the fleet.

    Reuses the existing per-agent spend rollup in :mod:`metrics` over a
    one-day window, summing every agent's ``SpendTotals``. ``spend_usd`` is
    ``None`` (not ``0``) when no spend ledger exists for today, so the client
    can distinguish "no data surfaced" from a genuine zero-dollar day. Any
    failure (missing state root, import error) degrades to an empty rollup
    rather than blanking ``/api/status``.
    """
    empty = {
        "spend_usd": None,
        "firings": 0,
        "successes": 0,
        "failures": 0,
        "agents_with_spend": 0,
    }
    state_root = getattr(reader, "state_root", None)
    if not isinstance(state_root, Path):
        return empty
    try:
        from metrics import fleet_metrics

        report = fleet_metrics(state_root, days=1)
    except Exception:  # pragma: no cover - defensive: metrics is optional
        return empty
    spend = 0.0
    firings = 0
    successes = 0
    failures = 0
    agents_with_spend = 0
    saw_ledger = False
    for metric in report.metrics:
        totals = metric.spend
        if totals.firings or totals.cost_usd or totals.successes or totals.failures:
            saw_ledger = True
            agents_with_spend += 1
        spend += totals.cost_usd
        firings += totals.firings
        successes += totals.successes
        failures += totals.failures
    return {
        "spend_usd": round(spend, 4) if saw_ledger else None,
        "firings": firings,
        "successes": successes,
        "failures": failures,
        "agents_with_spend": agents_with_spend,
    }


def _active_intake_profile_name() -> str:
    """Return the active intake profile name ("plain" or "technical").

    Reads ``ALFRED_INTAKE_PROFILE`` via the same resolver the planning
    assistant uses, so the API and the refiner never disagree about which
    profile is live. Falls back to "technical" if the helper is unavailable.
    """
    try:
        from intake_profiles import active_intake_profile

        return active_intake_profile().name
    except Exception:  # pragma: no cover - defensive: profiles is optional
        return "technical"


def _selected_setup_repos() -> list[str]:
    try:
        from server import setup as setup_mod

        return list(setup_mod.selected_repos())
    except Exception:  # pragma: no cover - setup context is optional
        return []


def _selected_setup_repos_for_scope() -> list[str]:
    """Return setup repos only when they are safe to treat as confirmed scope."""
    repos = _selected_setup_repos()
    return repos if len(repos) == 1 else []


def _draft_with_selected_setup_scope(draft: IssueDraft) -> IssueDraft:
    """Use the selected setup repo as scope only when there is exactly one."""
    if draft.repos:
        return draft
    setup_scope = _selected_setup_repos_for_scope()
    return replace(draft, repos=setup_scope) if setup_scope else draft


def _compose_context_repos(body: dict[str, Any], *, base_draft: IssueDraft) -> list[str]:
    """Repos available for planning context, not necessarily implementation scope."""
    import compose_converse as cc

    return (
        cc.normalize_repos(body.get("context_repos"))
        or cc.normalize_repos(body.get("repos"))
        or list(base_draft.repos)
        or _selected_setup_repos()
    )


def _selected_setup_repos_payload() -> dict[str, Any]:
    repos = _selected_setup_repos()
    return {"selected": repos, "count": len(repos)}


def _resolve_intake_profile_name(body: dict[str, Any]) -> str:
    """Pick the intake profile for one compose turn.

    A per-request ``plain`` boolean wins over the server env: ``true`` forces
    the plain (jargon-free) persona, ``false`` forces technical, and an absent
    flag falls back to ``ALFRED_INTAKE_PROFILE`` (the server default). Any
    non-boolean ``plain`` value is ignored so a malformed body cannot silently
    downgrade the persona. The toggle only changes the conversational surface;
    the structured draft and readiness scoring are identical in both modes.
    """
    plain = body.get("plain")
    if isinstance(plain, bool):
        return "plain" if plain else "technical"
    return _active_intake_profile_name()


def _compose_playbook_draft(
    request: Request, playbook: dict[str, Any], raw_repos: Any
) -> JSONResponse:
    """Compose a starter playbook into the same saved draft shape as Compose."""
    import compose_converse as cc

    spec = playbook.get("draft") or {}
    repos = cc.normalize_repos(raw_repos)
    if not repos:
        try:
            from server import setup as setup_mod

            repos = setup_mod.selected_repos()
        except Exception:  # pragma: no cover - setup is optional
            repos = []
    if not repos:
        repos = _payload_list(spec.get("repos"))
    draft = IssueDraft(
        title=str(spec.get("title") or playbook.get("title") or "").strip(),
        problem=str(spec.get("problem") or "").strip(),
        user=str(spec.get("user") or "").strip(),
        current_behavior=str(spec.get("current_behavior") or "").strip(),
        desired_behavior=str(spec.get("desired_behavior") or "").strip(),
        repos=cc.normalize_repos(repos),
        acceptance_criteria=_payload_list(spec.get("acceptance_criteria")),
        test_plan=str(spec.get("test_plan") or "").strip(),
        out_of_scope=str(spec.get("out_of_scope") or "").strip(),
        rollout=str(spec.get("rollout") or "").strip(),
        open_questions=str(spec.get("open_questions") or "").strip(),
    )
    memory_provider = _planning_memory_provider(request)
    assistant_result: PlanningAssistantResult = refine_issue_draft(
        draft, [], memory_provider=memory_provider
    )
    saved_path, draft_id = _save_compose_draft(
        request,
        draft=assistant_result.draft,
        assistant_result=assistant_result,
        draft_id=None,
        draft_path=None,
        prior_payload=None,
        revisions=[],
    )
    return JSONResponse(
        {
            "ok": True,
            "playbook": playbook.get("key"),
            "draft_id": draft_id,
            "saved_path": str(saved_path),
            "title": assistant_result.draft.title,
            "repos": list(assistant_result.draft.repos),
            "readiness": {
                "ok": assistant_result.readiness.ok,
                "score": assistant_result.readiness.score,
            },
        }
    )


def _run_compose_converse(request: Request, body: dict[str, Any]) -> JSONResponse:
    """One turn of the conversational spec-builder; persists a compose draft.

    Reuses the same compose-draft storage as the one-shot path so the result
    shows in Plans and threads into the RequestThread. The live interrogator is
    routed through the existing agent-engine dispatch; if no engine is
    configured (or it returns nothing usable) we degrade with a clear 503 rather
    than fabricate a turn, and the client falls back to the one-shot form.
    """
    import compose_converse as cc

    messages = cc.parse_messages(body.get("messages"))
    if not messages:
        return JSONResponse(
            {"error": "send at least one message to start the conversation"},
            status_code=400,
        )

    engine = cc.converse_engine_from_env()
    if not engine:
        return JSONResponse(
            {
                "error": "live_session_unavailable",
                "detail": (
                    "No conversational engine is configured for Compose. Set "
                    "ALFRED_COMPOSE_CONVERSE_ENGINE (or the planning-assistant "
                    "engine) to enable the chat, or use the one-shot plan form."
                ),
            },
            status_code=503,
        )

    draft_id = _safe_compose_draft_id(body.get("draft_id"))
    prior_payload, prior_path = _read_compose_draft_payload(request, draft_id)
    base_draft = _converse_base_draft(body, prior_payload)
    base_draft = _draft_with_selected_setup_scope(base_draft)

    repos = _compose_context_repos(body, base_draft=base_draft)
    repo_grounding = cc.build_repo_grounding(
        repos,
        workspace_root=_compose_workspace_root(),
        repo_to_local=_compose_repo_to_local(),
    )
    code_map = cc.load_code_map(_compose_code_map_path())
    # Plain mode is per-request: the client toggle wins when present, and the
    # ALFRED_INTAKE_PROFILE server env is only the default when the body omits
    # the flag. This lets a non-developer flip jargon-free coaching on/off in
    # the app without restarting the runtime.
    intake_guidance = cc.intake_guidance_for(_resolve_intake_profile_name(body))

    try:
        from agent_runner.metadata import load_prompt
    except Exception:  # pragma: no cover - load_prompt is always importable
        return JSONResponse(
            {"error": "compose interrogator prompt loader unavailable"},
            status_code=503,
        )

    try:
        system_prompt = cc.render_system_prompt(
            prompt_path=_compose_interrogator_prompt_path(),
            repo_grounding=repo_grounding,
            code_map=code_map,
            intake_guidance=intake_guidance,
            loader=load_prompt,
        )
    except OSError:
        return JSONResponse(
            {
                "error": "live_session_unavailable",
                "detail": (
                    "The spec-interrogator prompt could not be loaded. Check the "
                    "runtime deploy, or use the one-shot plan form."
                ),
            },
            status_code=503,
        )

    turn = cc.run_turn(
        system_prompt=system_prompt,
        messages=messages,
        repo_grounding=repo_grounding,
        code_map=code_map,
        intake_guidance=intake_guidance,
        base_draft=base_draft,
        engine=engine,
        workdir=_planning_workdir(request),
    )
    if turn is None:
        return JSONResponse(
            {
                "error": "live_session_unavailable",
                "detail": (
                    "The conversational engine did not return a usable turn. "
                    "Try again, or use the one-shot plan form."
                ),
            },
            status_code=503,
        )

    saved_path, saved_id = _save_converse_draft(
        request,
        turn=turn,
        messages=messages,
        draft_id=draft_id,
        draft_path=prior_path,
        prior_payload=prior_payload,
    )
    return JSONResponse(
        {
            "draft_id": saved_id,
            "saved_path": str(saved_path),
            "reply": turn.reply,
            "readiness": {
                "score": turn.readiness.score,
                "ready": turn.readiness.ready,
                "missing": list(turn.readiness.missing),
            },
            "done": turn.done,
            "draft": {
                "title": turn.draft.title,
                "problem": turn.draft.problem,
                "user": turn.draft.user,
                "current_behavior": turn.draft.current_behavior,
                "desired_behavior": turn.draft.desired_behavior,
                "repos": list(turn.draft.repos),
                "acceptance_criteria": list(turn.draft.acceptance_criteria),
                "test_plan": turn.draft.test_plan,
                "out_of_scope": turn.draft.out_of_scope,
                "rollout": turn.draft.rollout,
                "open_questions": turn.draft.open_questions,
            },
        }
    )


def _stream_compose_converse(request: Request, body: dict[str, Any]) -> Any:
    """Token-stream one converse turn, then reconcile + persist (#36).

    Shares the converse contract of ``_run_compose_converse``: same validation,
    same engine resolution + degrade signals, same draft persistence, same final
    ``ConverseResponse`` payload. The only difference is the transport, the turn
    runs on a worker thread while the assistant text it tees to its transcript is
    streamed as ``token`` SSE events, and the reconciled response arrives as a
    ``result`` event. Setup-stage failures return a normal JSON 4xx/503 (no
    stream opened); engine failures after the stream opens arrive as an
    ``error`` event so the client falls back to non-streaming converse.
    """
    import compose_converse as cc

    from server import streaming

    # The packaged webview reaches this route cross-origin, so every response
    # (setup-stage 4xx/503 JSON and the SSE stream alike) must carry CORS
    # headers or the webview cannot read the degrade signal to fall back.
    cors = _streaming_cors_headers(request)

    messages = cc.parse_messages(body.get("messages"))
    if not messages:
        return JSONResponse(
            {"error": "send at least one message to start the conversation"},
            status_code=400,
            headers=cors,
        )

    engine = cc.converse_engine_from_env()
    if not engine:
        return JSONResponse(
            {
                "error": "live_session_unavailable",
                "detail": (
                    "No conversational engine is configured for Compose. Set "
                    "ALFRED_COMPOSE_CONVERSE_ENGINE (or the planning-assistant "
                    "engine) to enable the chat, or use the one-shot plan form."
                ),
            },
            status_code=503,
            headers=cors,
        )

    draft_id = _safe_compose_draft_id(body.get("draft_id"))
    prior_payload, prior_path = _read_compose_draft_payload(request, draft_id)
    base_draft = _converse_base_draft(body, prior_payload)
    base_draft = _draft_with_selected_setup_scope(base_draft)

    repos = _compose_context_repos(body, base_draft=base_draft)
    repo_grounding = cc.build_repo_grounding(
        repos,
        workspace_root=_compose_workspace_root(),
        repo_to_local=_compose_repo_to_local(),
    )
    code_map = cc.load_code_map(_compose_code_map_path())
    # Plain mode is per-request: the client toggle wins when present, and the
    # ALFRED_INTAKE_PROFILE server env is only the default when the body omits
    # the flag. This lets a non-developer flip jargon-free coaching on/off in
    # the app without restarting the runtime.
    intake_guidance = cc.intake_guidance_for(_resolve_intake_profile_name(body))

    try:
        from agent_runner.metadata import load_prompt
    except Exception:  # pragma: no cover - load_prompt is always importable
        return JSONResponse(
            {"error": "compose interrogator prompt loader unavailable"},
            status_code=503,
            headers=cors,
        )

    try:
        system_prompt = cc.render_system_prompt(
            prompt_path=_compose_interrogator_prompt_path(),
            repo_grounding=repo_grounding,
            code_map=code_map,
            intake_guidance=intake_guidance,
            loader=load_prompt,
        )
    except OSError:
        return JSONResponse(
            {
                "error": "live_session_unavailable",
                "detail": (
                    "The spec-interrogator prompt could not be loaded. Check the "
                    "runtime deploy, or use the one-shot plan form."
                ),
            },
            status_code=503,
            headers=cors,
        )

    # Pre-mint the firing id so we can tail its transcript while the model runs.
    firing_id = cc.converse_firing_id()
    transcript = _converse_transcript_path(request, firing_id)
    workdir = _planning_workdir(request)

    def _run() -> Any:
        return cc.run_turn(
            system_prompt=system_prompt,
            messages=messages,
            repo_grounding=repo_grounding,
            code_map=code_map,
            intake_guidance=intake_guidance,
            base_draft=base_draft,
            engine=engine,
            workdir=workdir,
            firing_id=firing_id,
        )

    def _reconcile(turn: Any) -> dict[str, Any]:
        saved_path, saved_id = _save_converse_draft(
            request,
            turn=turn,
            messages=messages,
            draft_id=draft_id,
            draft_path=prior_path,
            prior_payload=prior_payload,
        )
        return _converse_turn_payload(turn, draft_id=saved_id, saved_path=saved_path)

    generator = streaming.stream_converse_turn(
        run_turn=_run,
        extract_tokens=streaming.assistant_text_fragments,
        transcript_path=transcript,
        reconcile=_reconcile,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers=_streaming_cors_headers(
            request,
            {
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        ),
    )


def _converse_turn_payload(turn: Any, *, draft_id: str, saved_path: Path) -> dict[str, Any]:
    """The ``ConverseResponse`` dict both converse routes return.

    Kept identical to the non-streaming route's JSON body so the client's
    reconcile step gets the same shape whether it streamed or not.
    """
    return {
        "draft_id": draft_id,
        "saved_path": str(saved_path),
        "reply": turn.reply,
        "readiness": {
            "score": turn.readiness.score,
            "ready": turn.readiness.ready,
            "missing": list(turn.readiness.missing),
        },
        "done": turn.done,
        "draft": {
            "title": turn.draft.title,
            "problem": turn.draft.problem,
            "user": turn.draft.user,
            "current_behavior": turn.draft.current_behavior,
            "desired_behavior": turn.draft.desired_behavior,
            "repos": list(turn.draft.repos),
            "acceptance_criteria": list(turn.draft.acceptance_criteria),
            "test_plan": turn.draft.test_plan,
            "out_of_scope": turn.draft.out_of_scope,
            "rollout": turn.draft.rollout,
            "open_questions": turn.draft.open_questions,
        },
    }


def _converse_transcript_path(request: Request, firing_id: str) -> Path:
    """Resolve the transcript JSONL a converse turn tees to under the state root.

    Mirrors ``agent_runner.transcript_path`` bucketing
    (``transcripts/<agent>/<YYYY-MM>/<firing_id>.jsonl``) against the serve
    state root so the token stream tails the same file the Claude streaming
    path writes, without importing the full runtime.
    """
    import compose_converse as cc

    month = datetime.now(UTC).strftime("%Y-%m")
    return _state_root(request) / "transcripts" / cc.CONVERSE_AGENT / month / f"{firing_id}.jsonl"


def _converse_base_draft(body: dict[str, Any], prior_payload: dict[str, Any] | None) -> IssueDraft:
    """Carry the spec forward across turns: prior saved draft, then body draft."""
    import compose_converse as cc

    raw_draft = body.get("draft")
    if isinstance(raw_draft, dict):
        return cc.draft_from_payload(raw_draft)
    if prior_payload is not None:
        prior_draft = prior_payload.get("draft")
        if isinstance(prior_draft, dict):
            return cc.draft_from_payload(prior_draft)
    return IssueDraft(title="")


def _save_converse_draft(
    request: Request,
    *,
    turn: Any,
    messages: list[Any],
    draft_id: str | None,
    draft_path: Path | None,
    prior_payload: dict[str, Any] | None,
) -> tuple[Path, str]:
    """Persist the conversation + accumulating spec as a compose planning draft.

    Reuses the compose-draft directory + readiness/spec_body shape so the saved
    record is interchangeable with the one-shot path in Plans listings, while
    adding the conversational transcript and the model-judged readiness.
    """
    from planning_assistant import render_development_spec

    root = _state_planning_root(request)
    root.mkdir(parents=True, exist_ok=True)
    if draft_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        draft_id = f"{_COMPOSE_PREFIX}{stamp}-{_slug(turn.draft.title)}"
        draft_path = root / f"{draft_id}.json"
    elif draft_id is None:
        draft_id = draft_path.stem
    created_at = (
        str(prior_payload.get("created_at"))
        if prior_payload and prior_payload.get("created_at")
        else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    rubric = assess_issue_draft(turn.draft)
    spec_body = render_development_spec(turn.draft, readiness=rubric)
    conversation = [{"role": message.role, "content": message.content} for message in messages]
    conversation.append({"role": "assistant", "content": turn.reply})
    payload = {
        "source": "compose",
        "mode": "converse",
        "draft_id": draft_id,
        "created_at": created_at,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "draft": asdict(turn.draft),
        "issue_body": rubric.issue_body,
        "spec_body": spec_body,
        # Persist BOTH readinesses: the model-judged verdict (primary, drives the
        # UI meter) and the deterministic rubric (the secondary signal), so a
        # later reader can see why a spec was or was not handed off.
        "readiness": {
            "ok": turn.readiness.ready,
            "score": turn.readiness.score,
            "missing": list(turn.readiness.missing),
        },
        "rubric_readiness": asdict(rubric),
        "questions": list(turn.readiness.missing),
        "done": turn.done,
        "conversation": conversation,
        "revision_count": len(conversation),
        "revisions": [message["content"] for message in conversation],
    }
    tmp = draft_path.with_name(f"{draft_path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(draft_path)
    return draft_path, draft_id


def _compose_workspace_root() -> Path:
    try:
        from agent_runner.paths import WORKSPACE

        return Path(WORKSPACE)
    except Exception:  # pragma: no cover - defensive
        base = os.environ.get("WORKSPACE_ROOT") or os.path.expanduser("~/code")
        return Path(base) / "product"


def _compose_repo_to_local() -> dict[str, str]:
    try:
        from agent_runner.github import GH_REPO_TO_LOCAL

        return dict(GH_REPO_TO_LOCAL)
    except Exception:  # pragma: no cover - defensive
        return {}


def _compose_code_map_path() -> Path:
    base = (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    return Path(base) / "state" / "code-map.json"


def _compose_interrogator_prompt_path() -> Path:
    override = os.environ.get("ALFRED_SPEC_INTERROGATOR_PROMPT")
    if override:
        return Path(override)
    relative = Path("prompts") / "spec-interrogator.md"
    candidates: list[Path] = []
    runtime_home = os.environ.get("ALFRED_HOME") or os.environ.get("HERMES_HOME")
    if runtime_home:
        candidates.append(Path(runtime_home) / relative)
    candidates.append(Path(__file__).resolve().parents[2] / relative)
    candidates.append(Path.cwd() / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


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


def _compose_draft_messages(
    text: str,
    base_draft: IssueDraft,
) -> list[str]:
    """Return messages for the deterministic compose fallback.

    The native Plan screen accepts plain prose. When no live refiner is
    configured, sending that prose directly through ``refine_issue_draft`` only
    stores it as an operator note, leaving the draft empty. For the reliable
    one-shot endpoint, synthesize a starter spec from plain prose so the UI can
    show a useful draft even offline. If a refiner is available or the text is
    already field-shaped, preserve the operator's exact message.
    """

    clean = str(text or "").strip()
    if not clean:
        return []
    if _compose_text_has_field_commands(clean):
        return [clean]
    return [_plain_compose_intent_to_fields(clean, base_draft)]


def _compose_text_has_field_commands(text: str) -> bool:
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        field = line.split(":", 1)[0].strip()
        if not field or len(field) > 30 or not field[:1].isalpha():
            continue
        normalized = " ".join(field.replace("_", " ").replace("-", " ").lower().split())
        if normalized in {
            "acceptance",
            "acceptance criteria",
            "context",
            "current",
            "current behavior",
            "desired",
            "desired behavior",
            "non goal",
            "non goals",
            "open question",
            "open questions",
            "out of scope",
            "problem",
            "repo",
            "repos",
            "repositories",
            "rollout",
            "test",
            "test plan",
            "tests",
            "title",
            "user",
        }:
            return True
    return False


def _plain_compose_intent_to_fields(text: str, base_draft: IssueDraft) -> str:
    clean = _compact_plain_text(text)
    title = base_draft.title or _plain_compose_title(clean)
    user = base_draft.user or _plain_compose_user(clean)
    problem = base_draft.problem or f"The current flow does not yet make this outcome easy: {clean}"
    current = (
        base_draft.current_behavior
        or "The operator must manually turn the idea into implementation-ready work."
    )
    desired = base_draft.desired_behavior or clean
    acceptance = list(base_draft.acceptance_criteria) or _plain_compose_acceptance(clean)
    repos = base_draft.repos or _plain_compose_repos(clean)
    test_plan = (
        base_draft.test_plan
        or "From the desktop Plan screen, submit the plain-language request and verify Alfred uses the selected repo context, saves a clear plan, and asks only for genuinely missing details."
    )
    out_of_scope = (
        base_draft.out_of_scope
        or "Starting implementation, opening a PR, or merging work before human approval."
    )
    rollout = base_draft.rollout or "Use the normal Alfred plan review and GitHub issue flow."
    lines = [
        f"title: {title}",
        f"problem: {problem}",
        f"user: {user}",
        f"current: {current}",
        f"desired: {desired}",
        *(f"repo: {repo}" for repo in repos),
        *(f"acceptance: {item}" for item in acceptance),
        f"test: {test_plan}",
        f"out of scope: {out_of_scope}",
        f"rollout: {rollout}",
    ]
    return "\n".join(lines)


def _compact_plain_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


_SCAN_TITLE_SUFFIX = " is hard to scan at small window sizes"


def _scan_title_subject(text: str) -> str:
    """Extract the subject of a "the SUBJECT is hard to scan ..." request.

    Replaces a backtracking ``\\bthe (.+?) is hard to scan at small window
    sizes\\b`` regex that ran in quadratic time on a hostile body of repeated
    ``the ...`` prefixes (py/polynomial-redos on ``POST /api/plans/draft``).
    This walks the (already whitespace-collapsed) text with ``str.find`` only,
    so the cost is strictly linear in the input length: locate the fixed suffix
    once, then take the nearest preceding ``the `` token as the subject start.
    Returns the cleaned subject, or ``""`` when the phrase is absent.
    """
    lowered = text.lower()
    suffix_at = lowered.find(_SCAN_TITLE_SUFFIX)
    if suffix_at < 0:
        return ""
    # Honour the trailing ``\b`` the old regex required after "sizes": the suffix
    # must end at a word boundary (end of text or a non-word char), so a run-on
    # like "...window sizesxyz" does not count as a match.
    suffix_end = suffix_at + len(_SCAN_TITLE_SUFFIX)
    if suffix_end < len(lowered):
        nxt = lowered[suffix_end]
        if nxt.isalnum() or nxt == "_":
            return ""
    # ``\bthe `` before the subject: the first "the " token that begins on a word
    # boundary and falls before the suffix. Scanning left to right mirrors the
    # old ``\bthe`` anchor (which matched the earliest valid occurrence) without
    # any backtracking. Each find advances ``cursor`` past the rejected hit, so
    # the whole loop is linear in the input length.
    token = "the "
    cursor = 0
    while True:
        start = lowered.find(token, cursor, suffix_at)
        if start < 0:
            return ""
        prev = "" if start == 0 else lowered[start - 1]
        if start == 0 or not (prev.isalnum() or prev == "_"):
            # Word boundary before "the" (start of text, or a non-word char).
            break
        # Inside another word (e.g. "breathe "): skip this hit and keep scanning.
        cursor = start + 1
    subject_start = start + len(token)
    subject = _compact_plain_text(text[subject_start:suffix_at]).strip(" ,.;:")
    return subject


def _plain_compose_title(text: str) -> str:
    # Collapse all whitespace runs to single spaces up front. Every regex below
    # then matches single-space separators (" ") instead of unbounded "\s+", so
    # a hostile request body padded with long whitespace runs cannot drive
    # polynomial backtracking (py/polynomial-redos): the search space no longer
    # contains repeated-whitespace input for the quantifiers to chew through.
    # The scan-title heuristic is handled by _scan_title_subject, which uses a
    # single linear str.find pass instead of a backtracking "the (.+?) sizes"
    # regex, so repeated "the ..." prefixes can no longer drive quadratic time.
    text = _compact_plain_text(text)
    lowered = text.lower()
    if "plan work" in lowered and "github issue" in lowered:
        return "Plan work drafts reviewable GitHub issues"
    if "setup" in lowered and ("github" in lowered or "repo" in lowered):
        return "Improve Alfred setup flow"
    scan_subject = _scan_title_subject(text)
    if scan_subject:
        return f"Make {scan_subject} usable at small sizes"
    title = re.sub(
        r"^(please |can you |could you |i want |we need )",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    title = re.split(r" (?:so that|so|because) ", title, maxsplit=1, flags=re.IGNORECASE)[0]
    if len(title) > 92:
        title = title[:92].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return title[:1].upper() + title[1:] if title else "Plan Alfred work"


def _compose_draft_response_summary(
    result: PlanningAssistantResult,
    *,
    synthesized_plain_intent: bool,
) -> str:
    if not synthesized_plain_intent:
        return result.summary
    if result.readiness.ok:
        return "I saved a starter plan that is ready to review."
    missing_codes = {finding.code for finding in result.readiness.findings}
    if missing_codes == {"missing_repo_scope"}:
        return "I saved a starter plan. Tell Alfred which part of the workspace this should change."
    question_count = len(result.questions)
    if question_count:
        label = "question" if question_count == 1 else "questions"
        return (
            f"I saved a starter plan. Answer {question_count} remaining {label} to make it ready."
        )
    return "I saved a starter plan. Review the plan before filing the issue."


def _plain_compose_user(text: str) -> str:
    patterns = [
        r"\bhelp\s+(?:a|an|the)?\s*([^,.]+?)\s+(?:turn|create|file|review|approve|understand|connect|plan|ship|use)\b",
        r"\bfor\s+(?:a|an|the)?\s*([^,.]+?)(?:\s+to\b|\s+so\b|\s+with\b|$)",
        r"\bso\s+(?:a|an|the)?\s*([^,.]+?)\s+can\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            user = _compact_plain_text(match.group(1)).strip(" ,.;:")
            if 2 <= len(user) <= 80:
                return user[:1].upper() + user[1:]
    return "Operator or product user"


def _plain_compose_acceptance(text: str) -> list[str]:
    lowered = text.lower()
    items = [
        "A user can describe the desired outcome in plain language.",
        "Alfred saves a reviewable GitHub issue draft from the request.",
    ]
    if "acceptance" in lowered or "criteria" in lowered:
        items.append("The draft includes concrete acceptance criteria.")
    if "label" in lowered:
        items.append("The draft includes the Alfred agent labels needed for pickup.")
    if "approval" in lowered or "approve" in lowered:
        items.append("The UI shows a clear approval path before any agent starts.")
    if "non-technical" in lowered or "non technical" in lowered:
        items.append("The copy avoids unexplained technical jargon.")
    if len(items) < 4:
        items.append(
            "Alfred uses the selected repo context instead of asking the user to re-enter it."
        )
    return items


def _plain_compose_repos(text: str) -> list[str]:
    repos: list[str] = []
    for match in re.finditer(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", text):
        repo = match.group(1).strip(".,;:()[]{}")
        if repo and repo not in repos:
            repos.append(repo)
    return repos


def _draft_from_payload(payload: dict[str, Any]) -> IssueDraft:
    # Route repos through the same slug gate the converse path uses
    # (cc.normalize_repos -> _valid_repo_slug). The one-shot draft loader must
    # not persist invalid slugs (e.g. "acme/..") into stored draft JSON, where a
    # future consumer resolving them to a workspace path would reopen the
    # traversal that the converse path closes at the chokepoint.
    import compose_converse as cc

    return IssueDraft(
        title=str(payload.get("title") or "").strip(),
        problem=str(payload.get("problem") or "").strip(),
        user=str(payload.get("user") or "").strip(),
        current_behavior=str(payload.get("current_behavior") or "").strip(),
        desired_behavior=str(payload.get("desired_behavior") or "").strip(),
        repos=cc.normalize_repos(_payload_list(payload.get("repos"))),
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
    for path in root.glob(f"{_COMPOSE_PREFIX}*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        saved_id = str(payload.get("draft_id") or path.stem).strip()
        if saved_id == draft_id:
            return payload, path
    return None, None


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


def _file_planning_draft_issue(state_root: Path, plan_id: str) -> dict[str, Any]:
    """Create fleet-pickup GitHub issue work from a saved planning draft.

    The native client calls this only after an explicit local operator action.
    Safety still comes from the same bridge rules as Slack: readiness must pass,
    repos must be allowlisted, and an existing ``bridge.issue_url`` or bundle
    URL map makes the operation idempotent.
    """
    from slack_issue_bridge import BridgeConfig, SlackIssueBridge

    import server.setup as setup_mod

    draft_id = _safe_planning_draft_id(plan_id)
    if draft_id is None:
        raise ValueError("plan id is not a safe planning draft id")
    path = Path(state_root) / "planning-drafts" / f"{draft_id}.json"
    if not path.is_file():
        raise FileNotFoundError(draft_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read planning draft: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ValueError("planning draft is not a JSON object")

    base = BridgeConfig.from_env()
    repos = base.repos or frozenset(setup_mod.selected_repos())
    bridge = SlackIssueBridge(
        config=BridgeConfig(
            enabled=True,
            repos=repos,
            label=base.label,
            approval_phrases=base.approval_phrases,
            min_readiness_score=base.min_readiness_score,
            approval_reactions=base.approval_reactions,
        )
    )
    existing_issue_url = _planning_draft_issue_url(payload)
    outcome = bridge.convert(
        payload,
        trusted=True,
        thread_link="",
        already_converted=bool(existing_issue_url),
    )
    issue_url = outcome.issue_url or existing_issue_url
    repo = outcome.repo or _first_draft_repo(payload)
    issue_urls = [issue_url] if issue_url else []
    issues_by_repo = {repo: issue_url} if repo and issue_url else {}
    repos_out = [repo] if repo else []
    labels_out = [base.label] if base.label else []

    if outcome.status == "already_converted" and issue_url:
        return {
            "ok": True,
            "status": "already_filed",
            "draft_id": draft_id,
            "issue_url": issue_url,
            "issue_urls": issue_urls,
            "issues_by_repo": issues_by_repo,
            "repo": repo,
            "repos": repos_out,
            "label": base.label,
            "labels": labels_out,
            "detail": outcome.detail,
        }
    if not outcome.created:
        return {
            "ok": False,
            "status": outcome.status,
            "draft_id": draft_id,
            "repo": repo,
            "label": base.label,
            "labels": labels_out,
            "error": outcome.detail or outcome.status,
        }

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["bridge"] = {
        "converted": True,
        "issue_url": issue_url,
        "issue_urls": issue_urls,
        "issues_by_repo": issues_by_repo,
        "repo": repo,
        "repos": repos_out,
        "label": base.label,
        "labels": labels_out,
        "filed_at": now,
        "source": "native-client",
    }
    payload["updated_at"] = now
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {
        "ok": True,
        "status": "filed",
        "draft_id": draft_id,
        "issue_url": issue_url,
        "issue_urls": issue_urls,
        "issues_by_repo": issues_by_repo,
        "repo": repo,
        "repos": repos_out,
        "label": base.label,
        "labels": labels_out,
        "detail": outcome.detail,
    }


def _discard_planning_draft_group(state_root: Path, draft_id: str) -> dict[str, Any]:
    """Archive every visible duplicate represented by one planning draft card."""
    root = Path(state_root)
    plan = FilesystemReader(state_root=root).get_plan(draft_id)
    if plan is None:
        return _discard_planning_draft(root, draft_id)

    draft_ids = _planning_draft_discard_group_ids(root, plan)
    results: list[dict[str, Any]] = []
    for candidate_id in draft_ids:
        try:
            results.append(_discard_planning_draft(root, candidate_id))
        except FileNotFoundError:
            if candidate_id == draft_id:
                raise
            continue
    if not results:
        raise FileNotFoundError(draft_id)

    archived_paths = [
        str(result["archived_path"]) for result in results if result.get("archived_path")
    ]
    return {
        "ok": True,
        "status": (
            "discarded"
            if any(result.get("status") == "discarded" for result in results)
            else "already_discarded"
        ),
        "draft_id": draft_id,
        "draft_ids": [str(result.get("draft_id") or "") for result in results],
        "discarded_count": len(results),
        "archived_path": archived_paths[0] if archived_paths else None,
        "archived_paths": archived_paths,
    }


def _planning_draft_discard_group_ids(state_root: Path, plan: PlanDraft) -> list[str]:
    fallback = _safe_planning_draft_id(plan.plan_id)
    if not fallback or not _dedupeable_planning_draft(plan):
        return [fallback] if fallback else []

    title, repos = _plan_dedupe_key(plan)
    if not title or not repos:
        return [fallback]

    ids: list[str] = []
    for candidate in FilesystemReader(state_root=Path(state_root)).list_plans(limit=10_000):
        if not _dedupeable_planning_draft(candidate):
            continue
        if _plan_dedupe_key(candidate) != (title, repos):
            continue
        candidate_id = _safe_planning_draft_id(candidate.plan_id)
        if candidate_id:
            ids.append(candidate_id)
    return ids or [fallback]


def _dedupeable_planning_draft(plan: PlanDraft) -> bool:
    return plan.source in {"compose", "planning", "slack"}


def _plan_dedupe_key(plan: PlanDraft) -> tuple[str, str]:
    title = re.sub(r"\s+", " ", (plan.title or "").strip().lower())
    if title == "alfred planning draft":
        title = ""
    repos = sorted(
        repo.strip().lower()
        for repo in re.split(r"[,\s]+", plan.affected_repos or "")
        if repo.strip()
    )
    return title, ",".join(repos)


def _discard_planning_draft(state_root: Path, draft_id: str) -> dict[str, Any]:
    """Archive a planning draft to ``planning-drafts/archive/``.

    Never hard-deletes: the draft JSON is moved under an ``archive/`` subdir so
    an accidental discard is recoverable. Idempotent: if the live draft is gone
    but an archived copy already exists, this is a no-op success.
    """
    draft_root = Path(state_root) / "planning-drafts"
    live_path = draft_root / f"{draft_id}.json"
    archive_dir = draft_root / "archive"
    archived_path = archive_dir / f"{draft_id}.json"

    if not live_path.is_file():
        existing_archive = _existing_planning_draft_archive(archive_dir, archived_path, draft_id)
        if existing_archive:
            return {
                "ok": True,
                "status": "already_discarded",
                "draft_id": draft_id,
                "archived_path": str(existing_archive),
            }
        raise FileNotFoundError(draft_id)

    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archived_path
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = archive_dir / f"{draft_id}-{stamp}.json"
    try:
        live_path.replace(target)
    except FileNotFoundError:
        existing_archive = _existing_planning_draft_archive(archive_dir, archived_path, draft_id)
        if existing_archive:
            return {
                "ok": True,
                "status": "already_discarded",
                "draft_id": draft_id,
                "archived_path": str(existing_archive),
            }
        raise
    return {
        "ok": True,
        "status": "discarded",
        "draft_id": draft_id,
        "archived_path": str(target),
    }


def _existing_planning_draft_archive(
    archive_dir: Path,
    archived_path: Path,
    draft_id: str,
) -> Path | None:
    if archived_path.is_file():
        return archived_path
    return next(archive_dir.glob(f"{draft_id}-*.json"), None)


def _safe_planning_draft_id(raw: Any) -> str | None:
    candidate = str(raw or "").strip()
    if not candidate or "/" in candidate or "\\" in candidate or candidate.startswith("."):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", candidate):
        return None
    return candidate


def _planning_draft_issue_url(payload: dict[str, Any]) -> str:
    bridge = payload.get("bridge")
    if isinstance(bridge, dict):
        issue_url = str(bridge.get("issue_url") or "").strip()
        if issue_url:
            return issue_url
        issue_urls = bridge.get("issue_urls")
        if isinstance(issue_urls, list):
            for item in issue_urls:
                text = str(item or "").strip()
                if text:
                    return text
        issues_by_repo = bridge.get("issues_by_repo")
        if isinstance(issues_by_repo, dict):
            for item in issues_by_repo.values():
                text = str(item or "").strip()
                if text:
                    return text
    return ""


def _first_draft_repo(payload: dict[str, Any]) -> str:
    draft = payload.get("draft")
    if not isinstance(draft, dict):
        return ""
    repos = draft.get("repos")
    if not isinstance(repos, list):
        return ""
    for repo in repos:
        text = str(repo or "").strip()
        if text:
            return text
    return ""


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
    return _state_root(request) / "planning-drafts"


def _state_root(request: Request) -> Path:
    reader = request.app.state.reader
    state_root = getattr(reader, "state_root", None)
    if isinstance(state_root, Path):
        return state_root
    base = (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.alfred")
    )
    return Path(base) / "state"


def _local_conversation_actor(value: Any) -> str:
    return normalize_slack_user_id(value) or operator_user_id_from_env() or _LOCAL_CLIENT_USER_ID


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
    if not _planning_uses_runtime_state(request):
        return None
    return _load_planning_memory_provider_from_env()


def _load_planning_memory_provider_from_env():
    if not (
        os.environ.get("ALFRED_HOME")
        or os.environ.get("HERMES_HOME")
        or os.environ.get("FLEET_BRAIN_HOST")
    ):
        return None
    try:
        from memory.config import load_provider

        return load_provider()
    except Exception:
        return None


def _planning_uses_runtime_state(request: Request) -> bool:
    reader = getattr(request.app.state, "reader", None)
    state_root = getattr(reader, "state_root", None)
    if not isinstance(state_root, Path):
        return False
    base = os.environ.get("ALFRED_HOME") or os.environ.get("HERMES_HOME")
    if base is None and os.environ.get("FLEET_BRAIN_HOST"):
        base = os.path.expanduser("~/.alfred")
    if not base:
        return False
    try:
        runtime_state = (Path(base).expanduser() / "state").resolve()
        return state_root.expanduser().resolve() == runtime_state
    except OSError:
        runtime_state = (Path(base).expanduser() / "state").absolute()
        return state_root.expanduser().absolute() == runtime_state


def _planning_memory_writer(request: Request, *, provider=None):
    configured = getattr(request.app.state, "planning_memory_writer", None)
    if configured is not None:
        return configured
    return _memory_candidate_writer(provider or _planning_memory_provider(request))


def _memory_candidate_writer(provider):
    if provider is None:
        return None
    if hasattr(provider, "propose_memory"):
        return provider
    brain = getattr(provider, "brain", None)
    if brain is not None and hasattr(brain, "propose_memory"):
        return brain
    providers = getattr(provider, "providers", None)
    if isinstance(providers, (list, tuple)):
        for child in providers:
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
                evidence=json.dumps(evidence, sort_keys=True),
                confidence=0.72,
            )
            candidate_id = getattr(candidate, "id", candidate)
        except TypeError:
            try:
                candidate = writer.propose_memory(
                    agent="planning",
                    repo=repo,
                    topic="planning-spec",
                    body=body,
                    source="planning-ui",
                    evidence=[evidence],
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
