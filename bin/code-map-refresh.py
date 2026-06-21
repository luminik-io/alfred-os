#!/usr/bin/env python3
"""code-map-refresh - scan configured repos and write a JSON code map.

Produces ${ALFRED_HOME}/state/code-map.json with per-repo HEAD SHA, plus
optional per-repo extracts:
  - local repo graph: source files, public-ish symbols, imports, import edges
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
  ALFRED_CODE_MAP_MAX_FILES    per-repo source-file cap for graph indexing
                                (default 2000)

Honest scope: regex-based, not tree-sitter. The graph is a local planning map,
not a compiler. Loud false positives, quiet false negatives, drift is advisory,
not a gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(
    0,
    (os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) + "/lib",
)
from agent_runner import (
    ALFRED_HOME,
    WORKSPACE,
    PreflightFailed,
    PreflightSpec,
    doctor_mode,
    local_repo_dir,
    preflight,
    slack_post,
    with_lock,
)

AGENT = os.environ.get("AGENT_CODENAME", "code-map-refresh")
CODE_MAP_PATH = ALFRED_HOME / "state" / "code-map.json"

REPOS = [r.strip() for r in os.environ.get("ALFRED_CODE_MAP_REPOS", "").split(",") if r.strip()]
BACKEND_REPO = os.environ.get("ALFRED_CODE_MAP_BACKEND_REPO", "").strip()
SIDECAR_REPO = os.environ.get("ALFRED_CODE_MAP_SIDECAR_REPO", "").strip()
CLIENT_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_CODE_MAP_CLIENT_REPOS", "").split(",") if r.strip()
]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


MAX_GRAPH_FILES = _env_int("ALFRED_CODE_MAP_MAX_FILES", 2000)

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
HONO_ROUTE_RE = re.compile(r"\bapp\.(get|post|put|delete|patch)\(\s*[`'\"]([^'\"`]+)[`'\"]")
ROUTE_MOUNT_RE = re.compile(r"\b(\w+)\.route\(\s*[`'\"]([^'\"`]+)[`'\"]\s*,\s*(\w+)\s*\)")
SUBROUTER_HANDLER_RE = re.compile(
    r"\b(\w+)\.(get|post|put|delete|patch|all)\(\s*[`'\"]([^'\"`]+)[`'\"]"
)
SOURCE_SUFFIXES = {
    ".go",
    ".js",
    ".jsx",
    ".kt",
    ".kts",
    ".py",
    ".rs",
    ".swift",
    ".ts",
    ".tsx",
}
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}


def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


LANG_BY_SUFFIX = {
    ".go": "go",
    ".js": "javascript",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".py": "python",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
    ".tsx": "typescript",
}
IMPORT_PATTERNS = {
    "go": [
        re.compile(r'^\s*import\s+"([^"]+)"'),
        re.compile(r'^\s*"([^"]+)"\s*$'),
    ],
    "javascript": [
        re.compile(r'^\s*import(?:\s+type)?(?:.+?\s+from\s+)?[\'"]([^\'"]+)[\'"]'),
        re.compile(r'\brequire\(\s*[\'"]([^\'"]+)[\'"]\s*\)'),
    ],
    "kotlin": [re.compile(r"^\s*import\s+([A-Za-z0-9_.*]+)")],
    "python": [
        re.compile(r"^\s*import\s+([A-Za-z0-9_., ]+)"),
        re.compile(r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\s+"),
    ],
    "rust": [
        re.compile(r"^\s*use\s+([^;]+);"),
        re.compile(r"^\s*extern\s+crate\s+([A-Za-z0-9_]+);"),
    ],
    "swift": [re.compile(r"^\s*import\s+([A-Za-z0-9_]+)")],
    "typescript": [
        re.compile(r'^\s*import(?:\s+type)?(?:.+?\s+from\s+)?[\'"]([^\'"]+)[\'"]'),
        re.compile(r'\brequire\(\s*[\'"]([^\'"]+)[\'"]\s*\)'),
    ],
}
SYMBOL_PATTERNS = {
    "go": [
        re.compile(r"^\s*func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b"),
    ],
    "javascript": [
        re.compile(
            r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)"
        ),
        re.compile(r"^\s*export\s+(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
    ],
    "kotlin": [
        re.compile(
            r"^\s*(?:data\s+|sealed\s+)?(?:class|interface|object)\s+([A-Za-z_][A-Za-z0-9_]*)"
        ),
        re.compile(r"^\s*(?:suspend\s+)?fun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ],
    "python": [
        re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    ],
    "rust": [
        re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
        re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ],
    "swift": [
        re.compile(
            r"^\s*(?:public\s+|private\s+|internal\s+|open\s+)?(?:class|struct|enum|protocol)\s+([A-Za-z_][A-Za-z0-9_]*)"
        ),
        re.compile(
            r"^\s*(?:public\s+|private\s+|internal\s+|open\s+)?func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        ),
    ],
    "typescript": [
        re.compile(
            r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)"
        ),
        re.compile(
            r"^\s*export\s+(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"
        ),
        re.compile(r"^\s*export\s+(?:interface|type)\s+([A-Za-z_$][A-Za-z0-9_$]*)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
    ],
}


def _git_head(repo_path: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return res.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _iter_source_files(repo_path: Path) -> tuple[list[Path], bool]:
    if not repo_path.is_dir():
        return [], False
    files: list[Path] = []
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if not _should_skip_dir(d))
        for filename in sorted(filenames):
            path = Path(root) / filename
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            files.append(path)
            if len(files) > MAX_GRAPH_FILES:
                return files[:MAX_GRAPH_FILES], True
    return files, False


def _language_for(path: Path) -> str:
    return LANG_BY_SUFFIX.get(path.suffix, "unknown")


def _extract_imports(language: str, text: str) -> list[str]:
    patterns = IMPORT_PATTERNS.get(language, [])
    imports: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*")):
            continue
        for pattern in patterns:
            for match in pattern.finditer(line):
                value = match.group(1).strip()
                if language == "python" and "," in value:
                    values = [
                        re.sub(r"\s+as\s+\w+$", "", part.strip()) for part in value.split(",")
                    ]
                elif language == "python":
                    values = [re.sub(r"\s+as\s+\w+$", "", value)]
                else:
                    values = [value]
                for item in values:
                    if not item or item in seen:
                        continue
                    seen.add(item)
                    imports.append(item)
        if len(imports) >= 80:
            break
    return imports


def _extract_symbols(language: str, text: str) -> list[dict[str, Any]]:
    patterns = SYMBOL_PATTERNS.get(language, [])
    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for line_idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith(("#", "//", "*")):
            continue
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1).strip()
            key = (name, line_idx)
            if key in seen:
                continue
            seen.add(key)
            symbols.append({"name": name, "line": line_idx})
            break
        if len(symbols) >= 40:
            break
    return symbols


def scan_repo_graph(repo_path: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    source_files, truncated = _iter_source_files(repo_path)

    for source_file in source_files:
        rel_path = str(source_file.relative_to(repo_path))
        language = _language_for(source_file)
        try:
            text = source_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = source_file.read_text(errors="ignore")
        except OSError:
            continue
        imports = _extract_imports(language, text)
        symbols = _extract_symbols(language, text)
        files.append(
            {
                "path": rel_path,
                "language": language,
                "symbols": symbols,
                "imports": imports,
            }
        )
        for target in imports[:80]:
            edges.append({"from": rel_path, "to": target, "kind": "import"})

    language_counts: dict[str, int] = {}
    for file_info in files:
        language = str(file_info.get("language") or "unknown")
        language_counts[language] = language_counts.get(language, 0) + 1

    return {
        "files": files,
        "edges": edges,
        "graph_summary": {
            "files": len(files),
            "symbols": sum(len(file_info.get("symbols", [])) for file_info in files),
            "imports": len(edges),
            "languages": dict(sorted(language_counts.items())),
            "truncated": truncated,
        },
    }


def _with_repo_graph(repo_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    graph = scan_repo_graph(repo_path)
    return {**data, **graph}


def scan_backend(repo_path: Path) -> dict[str, Any]:
    src_root = repo_path / "api" / "src" / "main" / "kotlin"
    endpoints: list[dict[str, Any]] = []

    if not src_root.is_dir():
        return _with_repo_graph(
            repo_path,
            {"head_sha": _git_head(repo_path), "endpoints": [], "flyway_head": None},
        )

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
            window = "\n".join(lines[i : i + 5])
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
            endpoints.append(
                {
                    "method": method,
                    "path": full,
                    "file": f"{kt_file.relative_to(repo_path)}:{i + 1}",
                }
            )

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

    return _with_repo_graph(
        repo_path,
        {
            "head_sha": _git_head(repo_path),
            "endpoints": unique,
            "flyway_head": flyway_head,
        },
    )


def scan_client_repo(repo_path: Path, src_subdir: str = "src") -> dict[str, Any]:
    src_root = repo_path / src_subdir
    api_calls: list[dict[str, Any]] = []

    if not src_root.is_dir():
        return _with_repo_graph(repo_path, {"head_sha": _git_head(repo_path), "api_calls": []})

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
                api_calls.append(
                    {
                        "method": method,
                        "path": path,
                        "file": f"{ts_file.relative_to(repo_path)}:{line_idx}",
                    }
                )

        joined = "\n".join(text.splitlines())
        for m in FETCH_CALL_RE.finditer(joined):
            url = m.group(1)
            if not (url.startswith("/api/v1/") or url.startswith("/v1/")):
                continue
            method_match = re.search(r"method:\s*[`'\"]([^'\"`]+)[`'\"]", m.group(0), re.IGNORECASE)
            method = method_match.group(1).upper() if method_match else "GET"
            line_idx = joined[: m.start()].count("\n") + 1
            api_calls.append(
                {
                    "method": method,
                    "path": url,
                    "file": f"{ts_file.relative_to(repo_path)}:~{line_idx}",
                }
            )

    return _with_repo_graph(repo_path, {"head_sha": _git_head(repo_path), "api_calls": api_calls})


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
        return _with_repo_graph(
            repo_path,
            {"head_sha": _git_head(repo_path), "routes": [], "api_calls": []},
        )

    ts_files = [
        f
        for f in src_root.rglob("*.ts")
        if "/__tests__/" not in str(f) and not f.name.endswith((".test.ts", ".spec.ts"))
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
                routes.append(
                    {
                        "method": method,
                        "path": full,
                        "file": f"{ts_file.relative_to(repo_path)}:{line_idx}",
                    }
                )

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for r in routes:
        key = (r["method"], r["path"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    client_data = scan_client_repo(repo_path)

    return _with_repo_graph(
        repo_path,
        {
            "head_sha": _git_head(repo_path),
            "routes": unique,
            "api_calls": client_data["api_calls"],
        },
    )


def _normalize_path(path: str) -> str:
    if "?" in path:
        path = path.split("?", 1)[0]
    if path.startswith("/api/v1/"):
        path = "/v1/" + path[len("/api/v1/") :]
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
                drift.append(
                    {
                        "caller": client_repo,
                        "method": call["method"],
                        "path": call["path"],
                        "normalized": key[1],
                        "file": call["file"],
                    }
                )
    return drift


def build_code_map() -> dict[str, Any]:
    repos: dict[str, Any] = {}

    if BACKEND_REPO:
        repos[BACKEND_REPO] = scan_backend(WORKSPACE / BACKEND_REPO)
    if SIDECAR_REPO and SIDECAR_REPO not in repos:
        repos[SIDECAR_REPO] = scan_sidecar_routes(WORKSPACE / local_repo_dir(SIDECAR_REPO))
    for repo in CLIENT_REPOS:
        if repo in repos:
            continue
        repos[repo] = scan_client_repo(WORKSPACE / local_repo_dir(repo))
    # HEAD-only entries for any remaining configured repos
    for repo in REPOS:
        if repo in repos:
            continue
        repo_path = WORKSPACE / local_repo_dir(repo)
        repos[repo] = _with_repo_graph(repo_path, {"head_sha": _git_head(repo_path)})

    code_map = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        print(
            f"[{AGENT.upper()}-IDLE] no repos configured "
            "(set ALFRED_CODE_MAP_REPOS, ALFRED_CODE_MAP_BACKEND_REPO, "
            "or ALFRED_CODE_MAP_SIDECAR_REPO)"
        )
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
    n_calls = sum(len(code_map["repos"].get(r, {}).get("api_calls", [])) for r in CLIENT_REPOS)
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
