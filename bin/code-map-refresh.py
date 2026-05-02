#!/usr/bin/env python3
"""code-map-refresh - scan configured repos and write a JSON code map.

Produces ${HERMES_HOME}/state/code-map.json with per-repo HEAD SHA, plus
optional per-repo extracts:
  - JAX-RS endpoints (Kotlin/Quarkus)
  - Hono / TS sub-router routes
  - Frontend / mobile / sidecar API calls
  - Flyway head migration
  - contract_drift list (client calls with no matching server endpoint)

Configuration via env vars (all optional - omit a slot to skip that scan):
  ALFRED_CODE_MAP_REPOS        comma-separated local repo dir names under
                                ${WORKSPACE_ROOT}/product to scan for HEAD SHA
                                + optional language-specific extracts
  ALFRED_CODE_MAP_BACKEND_REPO local dir name of the Kotlin/Quarkus backend
                                (its endpoints feed contract_drift's server set)
  ALFRED_CODE_MAP_SIDECAR_REPO local dir name of the Hono TS sidecar (also
                                contributes to contract_drift's server set)
  ALFRED_CODE_MAP_CLIENT_REPOS comma-separated frontend/mobile/etc dirs that
                                emit api_calls (matched against server set)

Honest scope: regex-based, not tree-sitter. Loud false positives, quiet
false negatives — drift is advisory, not a gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (  # noqa: E402
    HERMES_HOME, WORKSPACE, PreflightFailed, PreflightSpec,
    doctor_mode, preflight, slack_post, with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "code-map-refresh")
CODE_MAP_PATH = HERMES_HOME / "state" / "code-map.json"

REPOS = [
    r.strip()
    for r in os.environ.get("ALFRED_CODE_MAP_REPOS", "").split(",")
    if r.strip()
]
BACKEND_REPO = os.environ.get("ALFRED_CODE_MAP_BACKEND_REPO", "").strip()
SIDECAR_REPO = os.environ.get("ALFRED_CODE_MAP_SIDECAR_REPO", "").strip()
CLIENT_REPOS = [
    r.strip()
    for r in os.environ.get("ALFRED_CODE_MAP_CLIENT_REPOS", "").split(",")
    if r.strip()
]

PREFLIGHT = PreflightSpec(
    agent=AGENT,
    bins=["git", "grep"],
    require_workspace_repos=REPOS,
)

KOTLIN_PATH_RE = re.compile(r'@Path\(\s*"([^"]+)"\s*\)')
KOTLIN_METHOD_RE = re.compile(r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
CLIENT_CALL_RE = re.compile(
    r"\b(?:apiClient|axiosInstance|client|api|http)\.(get|post|put|delete|patch)"
    r"\(\s*[`'\"]([^'\"`]+)[`'\"]"
)
FETCH_CALL_RE = re.compile(
    r"\bfetch\(\s*[`'\"]([^'\"`]+)[`'\"]\s*,\s*\{[^}]*method:\s*[`'\"](?:get|post|put|delete|patch)[`'\"]",
    re.IGNORECASE | re.DOTALL,
)
HONO_ROUTE_RE = re.compile(
    r"\bapp\.(get|post|put|delete|patch)\(\s*[`'\"]([^'\"`]+)[`'\"]"
)
ROUTE_MOUNT_RE = re.compile(
    r"\b(\w+)\.route\(\s*[`'\"]([^'\"`]+)[`'\"]\s*,\s*(\w+)\s*\)"
)
SUBROUTER_HANDLER_RE = re.compile(
    r"\b(\w+)\.(get|post|put|delete|patch|all)\(\s*[`'\"]([^'\"`]+)[`'\"]"
)


def _git_head(repo_path: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return res.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def scan_backend(repo_path: Path) -> dict[str, Any]:
    src_root = repo_path / "api" / "src" / "main" / "kotlin"
    endpoints: list[dict[str, Any]] = []

    if not src_root.is_dir():
        return {"head_sha": _git_head(repo_path), "endpoints": [], "flyway_head": None}

    for kt_file in src_root.rglob("*.kt"):
        try:
            text = kt_file.read_text()
        except OSError:
            continue
        if "@Path" not in text:
            continue

        lines = text.splitlines()

        # Pass 1: find class-level @Path
        class_path: str | None = None
        for i, line in enumerate(lines):
            m = KOTLIN_PATH_RE.search(line)
            if not m:
                continue
            window = "\n".join(lines[i:i + 5])
            if re.search(r"\b(class|interface|object)\s+\w+", window):
                class_path = m.group(1)
                break

        # Pass 2: walk @METHOD annotations
        for i, line in enumerate(lines):
            mm = KOTLIN_METHOD_RE.search(line)
            if not mm:
                continue
            method = mm.group(1)
            method_path: str | None = None
            for k in range(i + 1, min(i + 16, len(lines))):
                if KOTLIN_METHOD_RE.search(lines[k]):
                    break
                if re.search(r"\bfun\s+\w+", lines[k]):
                    pm = KOTLIN_PATH_RE.search(lines[k])
                    if pm:
                        method_path = pm.group(1)
                    break
                pm = KOTLIN_PATH_RE.search(lines[k])
                if pm:
                    method_path = pm.group(1)
                    break

            if class_path is None and method_path is None:
                continue
            full = ((class_path or "") + (method_path or "")).replace("//", "/")
            if not full:
                continue
            endpoints.append({
                "method": method,
                "path": full,
                "file": f"{kt_file.relative_to(repo_path)}:{i + 1}",
            })

    flyway_head = None
    for candidate in [
        repo_path / "main-db" / "src" / "main" / "resources" / "db" / "main" / "migration",
        repo_path / "api" / "src" / "main" / "resources" / "db" / "migration",
    ]:
        if candidate.is_dir():
            migs = sorted(candidate.glob("V*.sql"))
            if migs:
                flyway_head = migs[-1].name
                break

    seen: set[tuple[str, str]] = set()
    unique = []
    for ep in endpoints:
        key = (ep["method"], ep["path"])
        if key not in seen:
            seen.add(key)
            unique.append(ep)

    return {
        "head_sha": _git_head(repo_path),
        "endpoints": unique,
        "flyway_head": flyway_head,
    }


def scan_client_repo(repo_path: Path, src_subdir: str = "src") -> dict[str, Any]:
    src_root = repo_path / src_subdir
    api_calls: list[dict[str, Any]] = []

    if not src_root.is_dir():
        return {"head_sha": _git_head(repo_path), "api_calls": []}

    for ts_file in src_root.rglob("*"):
        if not ts_file.is_file():
            continue
        if ts_file.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        if "/__tests__/" in str(ts_file) or ts_file.name.endswith(
            (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        ):
            continue
        try:
            text = ts_file.read_text()
        except OSError:
            continue
        for line_idx, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("*") or stripped.startswith("//"):
                continue
            for m in CLIENT_CALL_RE.finditer(line):
                method = m.group(1).upper()
                path = m.group(2)
                if not (path.startswith("/api/v1/") or path.startswith("/v1/")):
                    continue
                api_calls.append({
                    "method": method,
                    "path": path,
                    "file": f"{ts_file.relative_to(repo_path)}:{line_idx}",
                })

        joined = "\n".join(text.splitlines())
        for m in FETCH_CALL_RE.finditer(joined):
            url = m.group(1)
            if not (url.startswith("/api/v1/") or url.startswith("/v1/")):
                continue
            method_match = re.search(r"method:\s*[`'\"]([^'\"`]+)[`'\"]", m.group(0), re.IGNORECASE)
            method = (method_match.group(1).upper() if method_match else "GET")
            line_idx = joined[:m.start()].count("\n") + 1
            api_calls.append({
                "method": method,
                "path": url,
                "file": f"{ts_file.relative_to(repo_path)}:~{line_idx}",
            })

    return {"head_sha": _git_head(repo_path), "api_calls": api_calls}


def scan_sidecar_routes(repo_path: Path) -> dict[str, Any]:
    """Scan a Hono-based sidecar for routes.

    Two-pass: pass 1 discovers `app.route('/prefix', subRouterVar)` mounts,
    pass 2 walks `<varName>.<method>('<path>', ...)` matches and resolves
    them against the prefix table. Unknown router vars are skipped to avoid
    emitting garbage from non-router method chains.
    """
    src_root = repo_path / "src"
    routes: list[dict[str, Any]] = []

    if not src_root.is_dir():
        return {"head_sha": _git_head(repo_path), "routes": [], "api_calls": []}

    ts_files = [
        f for f in src_root.rglob("*.ts")
        if "/__tests__/" not in str(f)
        and not f.name.endswith((".test.ts", ".spec.ts"))
    ]

    sub_router_prefix: dict[str, str] = {}
    for ts_file in ts_files:
        try:
            text = ts_file.read_text()
        except OSError:
            continue
        for m in ROUTE_MOUNT_RE.finditer(text):
            _root_var, prefix, sub_var = m.group(1), m.group(2), m.group(3)
            if prefix.endswith("/") and len(prefix) > 1:
                prefix = prefix[:-1]
            sub_router_prefix[sub_var] = prefix

    for ts_file in ts_files:
        try:
            text = ts_file.read_text()
        except OSError:
            continue
        for line_idx, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("*") or stripped.startswith("//"):
                continue
            for m in SUBROUTER_HANDLER_RE.finditer(line):
                var_name, method, path = m.group(1), m.group(2).upper(), m.group(3)
                if var_name == "app":
                    full = path
                elif var_name in sub_router_prefix:
                    prefix = sub_router_prefix[var_name]
                    if path.startswith("/"):
                        full = f"{prefix}{path}".replace("//", "/")
                    else:
                        full = f"{prefix}/{path}"
                else:
                    continue
                routes.append({
                    "method": method,
                    "path": full,
                    "file": f"{ts_file.relative_to(repo_path)}:{line_idx}",
                })

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for r in routes:
        key = (r["method"], r["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    client_data = scan_client_repo(repo_path)

    return {
        "head_sha": _git_head(repo_path),
        "routes": unique,
        "api_calls": client_data["api_calls"],
    }


def _normalize_path(path: str) -> str:
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.startswith("/api/v1/"):
        path = "/v1/" + path[len("/api/v1/"):]
    path = re.sub(r"\$\{[^}]*query[^}]*\}$", "", path, flags=re.IGNORECASE)
    path = re.sub(r"\$\{[^}]*search[^}]*\}$", "", path, flags=re.IGNORECASE)
    path = re.sub(r"\$\{[^}]*params[^}]*\}$", "", path, flags=re.IGNORECASE)
    path = re.sub(r"\$\{[^}]+\}", "{*}", path)
    path = re.sub(r"\{[^}]+\}", "{*}", path)
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return path


def compute_contract_drift(code_map: dict[str, Any]) -> list[dict[str, Any]]:
    server_set: set[tuple[str, str]] = set()
    if BACKEND_REPO:
        backend = code_map["repos"].get(BACKEND_REPO, {})
        for ep in backend.get("endpoints", []):
            server_set.add((ep["method"], _normalize_path(ep["path"])))
    if SIDECAR_REPO:
        sidecar = code_map["repos"].get(SIDECAR_REPO, {})
        for rt in sidecar.get("routes", []):
            server_set.add((rt["method"], _normalize_path(rt["path"])))

    drift: list[dict[str, Any]] = []
    for client_repo in CLIENT_REPOS:
        repo_data = code_map["repos"].get(client_repo, {})
        for call in repo_data.get("api_calls", []):
            key = (call["method"], _normalize_path(call["path"]))
            if key not in server_set:
                drift.append({
                    "caller": client_repo,
                    "method": call["method"],
                    "path": call["path"],
                    "normalized": key[1],
                    "file": call["file"],
                })
    return drift


def build_code_map() -> dict[str, Any]:
    repos: dict[str, Any] = {}

    if BACKEND_REPO:
        repos[BACKEND_REPO] = scan_backend(WORKSPACE / BACKEND_REPO)
    if SIDECAR_REPO and SIDECAR_REPO not in repos:
        repos[SIDECAR_REPO] = scan_sidecar_routes(WORKSPACE / SIDECAR_REPO)
    for repo in CLIENT_REPOS:
        if repo in repos:
            continue
        repos[repo] = scan_client_repo(WORKSPACE / repo)
    # HEAD-only entries for any remaining configured repos
    for repo in REPOS:
        if repo in repos:
            continue
        repos[repo] = {"head_sha": _git_head(WORKSPACE / repo)}

    code_map = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repos": repos,
    }
    code_map["contract_drift"] = compute_contract_drift(code_map)
    return code_map


def main() -> int:
    with_lock(AGENT)

    try:
        preflight(PREFLIGHT)
    except PreflightFailed:
        return 0

    if doctor_mode():
        print(f"[{AGENT.upper()}-DOCTOR-OK]")
        return 0

    if not REPOS and not BACKEND_REPO and not SIDECAR_REPO:
        print(f"[{AGENT.upper()}-IDLE] no repos configured "
              "(set ALFRED_CODE_MAP_REPOS, ALFRED_CODE_MAP_BACKEND_REPO, "
              "or ALFRED_CODE_MAP_SIDECAR_REPO)")
        return 0

    code_map = build_code_map()

    CODE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CODE_MAP_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(code_map, indent=2))
    tmp_path.rename(CODE_MAP_PATH)

    n_endpoints = 0
    if BACKEND_REPO:
        n_endpoints = len(code_map["repos"].get(BACKEND_REPO, {}).get("endpoints", []))
    n_routes = 0
    if SIDECAR_REPO:
        n_routes = len(code_map["repos"].get(SIDECAR_REPO, {}).get("routes", []))
    n_calls = sum(
        len(code_map["repos"].get(r, {}).get("api_calls", []))
        for r in CLIENT_REPOS
    )
    n_drift = len(code_map["contract_drift"])

    summary = (
        f"[CODE-MAP-OK] backend={n_endpoints} endpoints, sidecar={n_routes} routes, "
        f"clients={n_calls} calls, drift={n_drift}"
    )
    print(summary)

    if n_drift > 0:
        sample = "\n".join(
            f"  • {d['caller']}: {d['method']} {d['path']} (no matching server)"
            for d in code_map["contract_drift"][:5]
        )
        more = f"\n  ...and {n_drift - 5} more" if n_drift > 5 else ""
        slack_post(f"⚠️ code-map: {n_drift} contract drift entries\n{sample}{more}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
