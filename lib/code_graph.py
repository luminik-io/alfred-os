"""Stable code-graph export helpers for Alfred's local code map.

``bin/code-map-refresh.py`` writes an implementation-shaped JSON snapshot. This
module turns that snapshot into a small public contract agents and local tools
can rely on: ``alfred-codegraph@1``.
"""

from __future__ import annotations

import json
import os
import posixpath
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CODEGRAPH_SCHEMA = "alfred-codegraph@1"


def default_code_map_path() -> Path:
    """Return the installed code-map path for the current Alfred home."""

    alfred_home = Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred"))
    return alfred_home / "state" / "code-map.json"


def load_code_map(path: Path | str | None = None) -> dict[str, Any]:
    """Load a code-map JSON file, returning an empty map when it is absent."""

    resolved = Path(path) if path is not None else default_code_map_path()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"generated_at": None, "repos": {}, "contract_drift": []}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid code-map JSON at {resolved}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid code-map JSON at {resolved}: expected object")
    payload.setdefault("repos", {})
    payload.setdefault("contract_drift", [])
    return payload


def export_codegraph(
    code_map: dict[str, Any] | None = None,
    *,
    path: Path | str | None = None,
    include_files: bool = True,
) -> dict[str, Any]:
    """Export the code map using the stable ``alfred-codegraph@1`` schema."""

    resolved = Path(path) if path is not None else default_code_map_path()
    payload = code_map if code_map is not None else load_code_map(resolved)
    repos = _dict_value(payload.get("repos"))
    source = (
        {"kind": "alfred-code-map", "path": str(resolved)}
        if code_map is None or path is not None
        else {"kind": "in-memory-code-map", "path": None}
    )
    return {
        "schema": CODEGRAPH_SCHEMA,
        "generated_at": _str_or_none(payload.get("generated_at")),
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "repos": [
            _export_repo(name, data, include_files=include_files)
            for name, data in sorted(repos.items())
            if isinstance(data, dict)
        ],
        "contract_drift": _list_of_dicts(payload.get("contract_drift")),
    }


def summarize_codegraph(
    code_map: dict[str, Any] | None = None,
    *,
    repo: str | None = None,
    path: Path | str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Return repo-level graph summaries without raw file lists."""

    payload = code_map if code_map is not None else load_code_map(path)
    repos = _dict_value(payload.get("repos"))
    selected = {
        name: data
        for name, data in sorted(repos.items())
        if isinstance(data, dict) and (repo is None or name == repo)
    }
    if repo is not None and repo not in selected:
        raise ValueError(f"repo not found in code map: {repo}")
    max_items = _clamped_int(limit, default=25, max_value=100)
    summaries = [_repo_summary(name, data) for name, data in list(selected.items())[:max_items]]
    selected_names = {str(item["name"]) for item in summaries}
    filtered_drift = [
        drift
        for drift in _list_of_dicts(payload.get("contract_drift"))
        if drift.get("caller") in selected_names
    ]
    return {
        "schema": CODEGRAPH_SCHEMA,
        "generated_at": _str_or_none(payload.get("generated_at")),
        "repos": summaries,
        "repo_count": len(summaries),
        "contract_drift_count": len(filtered_drift),
    }


def impact_for_path(
    code_map: dict[str, Any] | None,
    *,
    repo: str,
    path: str,
    code_map_path: Path | str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return local impact hints for a repo path from the code-map graph."""

    payload = code_map if code_map is not None else load_code_map(code_map_path)
    repos = _dict_value(payload.get("repos"))
    repo_data = repos.get(repo)
    if not isinstance(repo_data, dict):
        raise ValueError(f"repo not found in code map: {repo}")

    max_items = _clamped_int(limit, default=50, max_value=200)
    target_path = _normalize_file_path(path)
    files = _list_of_dicts(repo_data.get("files"))
    file_by_path = {_normalize_file_path(str(f.get("path") or "")): f for f in files}
    matched_path, match_status, candidate_matches = _match_path(target_path, file_by_path)
    matched_file = file_by_path.get(matched_path or "")
    candidate_paths = set(file_by_path)

    incoming: list[dict[str, Any]] = []
    outgoing: list[dict[str, Any]] = []
    for edge in _list_of_dicts(repo_data.get("edges")):
        source = _normalize_file_path(str(edge.get("from") or ""))
        target = str(edge.get("to") or "").strip()
        if not source or not target:
            continue
        resolved = _resolve_import(source, target, candidate_paths)
        row = {
            "from": source,
            "to": target,
            "resolved_to": resolved,
            "kind": str(edge.get("kind") or "import"),
        }
        if matched_path is not None and source == matched_path:
            outgoing.append(row)
        if matched_path is not None and (
            resolved == matched_path or _normalize_file_path(target) == matched_path
        ):
            incoming.append(row)

    contracts = _contracts_for_file(repo_data, matched_path)
    drift = [
        d
        for d in _list_of_dicts(payload.get("contract_drift"))
        if d.get("caller") == repo and _file_matches(str(d.get("file") or ""), matched_path)
    ]
    nearby = [
        _normalize_file_path(str(file_info.get("path") or ""))
        for file_info in files
        if _same_directory(str(file_info.get("path") or ""), matched_path)
        and _normalize_file_path(str(file_info.get("path") or "")) != matched_path
    ][:max_items]

    return {
        "schema": CODEGRAPH_SCHEMA,
        "repo": repo,
        "path": target_path,
        "matched_file": matched_path,
        "match_status": match_status,
        "candidate_matches": candidate_matches[:max_items],
        "head_sha": _str_or_none(repo_data.get("head_sha")),
        "language": _str_or_none(matched_file.get("language")) if matched_file else None,
        "symbols": _list_of_dicts(matched_file.get("symbols"))[:max_items] if matched_file else [],
        "imports": list(matched_file.get("imports") or [])[:max_items] if matched_file else [],
        "imported_by": incoming[:max_items],
        "imports_resolved": outgoing[:max_items],
        "contracts": contracts,
        "contract_drift": drift[:max_items],
        "nearby_files": nearby,
        "graph_summary": _clean_summary(repo_data.get("graph_summary")),
    }


def _export_repo(name: str, data: dict[str, Any], *, include_files: bool) -> dict[str, Any]:
    repo: dict[str, Any] = {
        "name": name,
        "head_sha": _str_or_none(data.get("head_sha")),
        "summary": _clean_summary(data.get("graph_summary")),
        "contracts": _repo_contracts(data),
    }
    if include_files:
        repo["files"] = [_clean_file(f) for f in _list_of_dicts(data.get("files"))]
        repo["edges"] = [_clean_edge(e) for e in _list_of_dicts(data.get("edges"))]
    return repo


def _repo_summary(name: str, data: dict[str, Any]) -> dict[str, Any]:
    contracts = _repo_contracts(data)
    return {
        "name": name,
        "head_sha": _str_or_none(data.get("head_sha")),
        "summary": _clean_summary(data.get("graph_summary")),
        "endpoint_count": len(contracts["endpoints"]),
        "route_count": len(contracts["routes"]),
        "api_call_count": len(contracts["api_calls"]),
    }


def _repo_contracts(data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "endpoints": _list_of_dicts(data.get("endpoints")),
        "routes": _list_of_dicts(data.get("routes")),
        "api_calls": _list_of_dicts(data.get("api_calls")),
    }


def _contracts_for_file(data: dict[str, Any], path: str | None) -> dict[str, list[dict[str, Any]]]:
    if not path:
        return {"endpoints": [], "routes": [], "api_calls": []}
    return {
        "endpoints": [
            row
            for row in _list_of_dicts(data.get("endpoints"))
            if _file_matches(row.get("file"), path)
        ],
        "routes": [
            row
            for row in _list_of_dicts(data.get("routes"))
            if _file_matches(row.get("file"), path)
        ],
        "api_calls": [
            row
            for row in _list_of_dicts(data.get("api_calls"))
            if _file_matches(row.get("file"), path)
        ],
    }


def _clean_summary(value: Any) -> dict[str, Any]:
    raw = _dict_value(value)
    languages = _dict_value(raw.get("languages"))
    return {
        "files": _nonnegative_int(raw.get("files")),
        "symbols": _nonnegative_int(raw.get("symbols")),
        "imports": _nonnegative_int(raw.get("imports")),
        "languages": {
            str(k): _nonnegative_int(v)
            for k, v in sorted(languages.items())
            if _nonnegative_int(v) > 0
        },
        "truncated": bool(raw.get("truncated")),
    }


def _clean_file(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _normalize_file_path(str(value.get("path") or "")),
        "language": _str_or_none(value.get("language")),
        "symbols": _list_of_dicts(value.get("symbols")),
        "imports": list(value.get("imports") or []),
    }


def _clean_edge(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": _normalize_file_path(str(value.get("from") or "")),
        "to": str(value.get("to") or "").strip(),
        "kind": str(value.get("kind") or "import"),
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dict_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return value


def _nonnegative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _clamped_int(value: Any, *, default: int, max_value: int) -> int:
    try:
        parsed = int(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, max_value))


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_file_path(path: str) -> str:
    clean = path.split(":", 1)[0].strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.strip("/")


def _match_path(path: str, files: dict[str, dict[str, Any]]) -> tuple[str | None, str, list[str]]:
    if path in files:
        return path, "exact", [path]
    suffix_matches = sorted(candidate for candidate in files if candidate.endswith(f"/{path}"))
    if len(suffix_matches) == 1:
        return suffix_matches[0], "suffix", suffix_matches
    if suffix_matches:
        return None, "ambiguous", suffix_matches
    return None, "not_found", []


def _file_matches(file_ref: Any, path: str | None) -> bool:
    if not path:
        return False
    return _normalize_file_path(str(file_ref or "")) == path


def _same_directory(candidate: str, path: str | None) -> bool:
    if not path:
        return False
    return Path(_normalize_file_path(candidate)).parent == Path(path).parent


def _resolve_import(source: str, target: str, candidates: set[str]) -> str | None:
    if not target.startswith("."):
        return None
    normalized = _relative_import_path(source, target)
    stems = [
        normalized,
        f"{normalized}.py",
        f"{normalized}.ts",
        f"{normalized}.tsx",
        f"{normalized}.js",
        f"{normalized}.jsx",
        f"{normalized}.kt",
        f"{normalized}.go",
        f"{normalized}.rs",
        f"{normalized}.swift",
        f"{normalized}/index.ts",
        f"{normalized}/index.tsx",
        f"{normalized}/index.js",
        f"{normalized}/index.jsx",
        f"{normalized}/__init__.py",
    ]
    for candidate in stems:
        clean = _normalize_file_path(candidate)
        if clean in candidates:
            return clean
    return None


def _relative_import_path(source: str, target: str) -> str:
    base = Path(source).parent
    if target.startswith(".") and not target.startswith(("./", "../")) and "/" not in target:
        dot_count = len(target) - len(target.lstrip("."))
        module = target[dot_count:].replace(".", "/")
        for _ in range(max(0, dot_count - 1)):
            base = base.parent
        return posixpath.normpath((base / module).as_posix())
    return posixpath.normpath((base / target).as_posix())
