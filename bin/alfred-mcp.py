#!/usr/bin/env python3
"""Read-only MCP-style stdio tools for Alfred local memory.

The script speaks the JSON-RPC methods used by MCP clients:
``initialize``, ``tools/list``, and ``tools/call``. It intentionally
depends only on the standard library and exposes allowlisted summaries,
not raw transcripts, prompts, stdout, stderr, or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fleet_brain import FleetBrain, default_db_path  # noqa: E402
from fleet_brain.doctor import run_memory_doctor  # noqa: E402

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "alfred_brain_status",
        "description": "Return local fleet-brain row counts and health status.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "alfred_memory_recall",
        "description": "Recall trusted lessons scoped by codename or repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codename": {"type": "string"},
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "alfred_memory_candidates",
        "description": "List reviewable memory candidates scoped by codename or repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["candidate", "validated", "rejected", "retired", "all"],
                },
                "repo": {"type": "string"},
                "codename": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "alfred_recent_file_touches",
        "description": "List recent files touched by Alfred firings, scoped by codename or repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "codename": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "alfred_failure_patterns",
        "description": "List normalized non-success events scoped by codename or repo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "codename": {"type": "string"},
                "subtype": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "alfred_memory_doctor",
        "description": "Run read-only health checks over fleet-brain memory.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
)


def _brain(db_path: str | None = None) -> FleetBrain:
    return FleetBrain(
        db_path=db_path or os.environ.get("ALFRED_FLEET_BRAIN_DB") or default_db_path()
    )


def call_tool(
    name: str, arguments: dict[str, Any] | None = None, *, db_path: str | None = None
) -> Any:
    args = arguments or {}
    if name == "alfred_brain_status":
        return run_memory_doctor(db_path or os.environ.get("ALFRED_FLEET_BRAIN_DB"))
    if name == "alfred_memory_doctor":
        return run_memory_doctor(db_path or os.environ.get("ALFRED_FLEET_BRAIN_DB"))
    brain = _brain(db_path)
    if name == "alfred_memory_recall":
        _require_scope(args)
        return [
            _lesson_to_dict(L)
            for L in brain.recall(
                codename=_str_or_none(args.get("codename")),
                repo=_str_or_none(args.get("repo")),
                query=_str_or_none(args.get("query")),
                limit=int(args.get("limit") or 8),
            )
        ]
    if name == "alfred_memory_candidates":
        _require_scope(args)
        status = args.get("status") or "candidate"
        return [
            _candidate_to_dict(C, include_raw=_raw_memory_allowed())
            for C in brain.list_memory_candidates(
                status=None if status == "all" else status,
                repo=_str_or_none(args.get("repo")),
                codename=_str_or_none(args.get("codename")),
                limit=int(args.get("limit") or 50),
            )
        ]
    if name == "alfred_recent_file_touches":
        _require_scope(args)
        return [
            _touch_to_dict(T)
            for T in brain.list_file_touches(
                repo=_str_or_none(args.get("repo")),
                codename=_str_or_none(args.get("codename")),
                path=_str_or_none(args.get("path")),
                limit=int(args.get("limit") or 50),
            )
        ]
    if name == "alfred_failure_patterns":
        _require_scope(args)
        failures = brain.list_failures(
            repo=_str_or_none(args.get("repo")),
            codename=_str_or_none(args.get("codename")),
            subtype=_str_or_none(args.get("subtype")),
            limit=int(args.get("limit") or 50),
        )
        by_subtype: dict[str, int] = {}
        for event in failures:
            by_subtype[event.subtype] = by_subtype.get(event.subtype, 0) + 1
        return {"by_subtype": by_subtype, "events": [_failure_to_dict(F) for F in failures]}
    raise ValueError(f"unknown tool: {name}")


def handle_request(request: dict[str, Any], *, db_path: str | None = None) -> dict[str, Any] | None:
    method = request.get("method")
    req_id = request.get("id")
    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "alfred-memory", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": list(TOOLS)}}
        if method == "tools/call":
            params = request.get("params") or {}
            result = call_tool(
                str(params.get("name") or ""),
                params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
                db_path=db_path,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    "isError": False,
                },
            }
        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        raise ValueError(f"unsupported method: {method}")
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def serve_stdio(*, db_path: str | None = None) -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }
        else:
            response = handle_request(request, db_path=db_path)
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_scope(args: dict[str, Any]) -> None:
    """Require local MCP callers to narrow row-returning memory queries."""
    if _str_or_none(args.get("codename")) or _str_or_none(args.get("repo")):
        return
    raise ValueError("memory tools require a codename or repo scope")


def _raw_memory_allowed() -> bool:
    return os.environ.get("ALFRED_MCP_ALLOW_RAW_MEMORY", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _lesson_to_dict(lesson) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": lesson.id,
        "codename": lesson.codename,
        "repo": lesson.repo,
        "body": lesson.body,
        "tags": lesson.tags,
        "severity": lesson.severity,
        "firing_id": lesson.firing_id,
        "created_at": lesson.created_at.astimezone(UTC).isoformat(),
    }


def _candidate_to_dict(candidate, *, include_raw: bool = False) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    payload = {
        "id": candidate.id,
        "codename": candidate.codename,
        "repo": candidate.repo,
        "tags": candidate.tags,
        "severity": candidate.severity,
        "source": candidate.source,
        "source_firing_id": candidate.source_firing_id,
        "confidence": candidate.confidence,
        "status": candidate.status,
        "created_at": candidate.created_at.astimezone(UTC).isoformat(),
        "promoted_lesson_id": candidate.promoted_lesson_id,
    }
    if include_raw:
        payload.update(
            {
                "body": candidate.body,
                "evidence": candidate.evidence,
                "reviewed_at": candidate.reviewed_at.astimezone(UTC).isoformat()
                if candidate.reviewed_at
                else None,
                "reviewed_by": candidate.reviewed_by,
                "review_note": candidate.review_note,
            }
        )
    else:
        payload["body_preview"] = _preview(candidate.body)
        payload["has_evidence"] = bool(candidate.evidence)
        payload["reviewed"] = bool(candidate.reviewed_at or candidate.reviewed_by)
    return payload


def _preview(value: str, limit: int = 120) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _touch_to_dict(touch) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": touch.id,
        "repo": touch.repo,
        "path": touch.path,
        "codename": touch.codename,
        "firing_id": touch.firing_id,
        "pr_url": touch.pr_url,
        "change_type": touch.change_type,
        "touched_at": touch.touched_at.astimezone(UTC).isoformat(),
    }


def _failure_to_dict(event) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": event.id,
        "codename": event.codename,
        "repo": event.repo,
        "firing_id": event.firing_id,
        "subtype": event.subtype,
        "summary": event.summary,
        "engine": event.engine,
        "severity": event.severity,
        "created_at": event.created_at.astimezone(UTC).isoformat(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alfred-mcp", description="Read-only Alfred MCP tools")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="serve JSON-RPC over stdio")
    serve.add_argument("--db", help="path to the SQLite brain file")
    serve.set_defaults(func=lambda args: serve_stdio(db_path=args.db))
    parser.set_defaults(func=lambda args: serve_stdio(db_path=getattr(args, "db", None)))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
