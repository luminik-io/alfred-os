from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from code_graph import (  # noqa: E402
    CODEGRAPH_SCHEMA,
    export_codegraph,
    impact_for_path,
    summarize_codegraph,
)


def _sample_code_map() -> dict:
    return {
        "generated_at": "2026-06-30T20:00:00Z",
        "repos": {
            "web": {
                "head_sha": "abc123",
                "graph_summary": {
                    "files": 3,
                    "symbols": 4,
                    "imports": 2,
                    "languages": {"typescript": 3},
                    "truncated": False,
                },
                "files": [
                    {
                        "path": "src/App.tsx",
                        "language": "typescript",
                        "symbols": [{"name": "App", "line": 4}],
                        "imports": ["./Widget"],
                    },
                    {
                        "path": "src/Widget.tsx",
                        "language": "typescript",
                        "symbols": [{"name": "Widget", "line": 2}],
                        "imports": ["./api"],
                    },
                    {
                        "path": "src/api.ts",
                        "language": "typescript",
                        "symbols": [{"name": "loadThing", "line": 8}],
                        "imports": [],
                    },
                ],
                "edges": [
                    {"from": "src/App.tsx", "to": "./Widget", "kind": "import"},
                    {"from": "src/Widget.tsx", "to": "./api", "kind": "import"},
                ],
                "api_calls": [{"method": "GET", "path": "/api/v1/things", "file": "src/api.ts:9"}],
            }
        },
        "contract_drift": [
            {
                "caller": "web",
                "method": "GET",
                "path": "/api/v1/things",
                "normalized": "/v1/things",
                "file": "src/api.ts:9",
            }
        ],
    }


def test_export_codegraph_uses_stable_schema() -> None:
    exported = export_codegraph(_sample_code_map(), path=Path("/tmp/code-map.json"))

    assert exported["schema"] == CODEGRAPH_SCHEMA
    assert exported["source"] == {"kind": "alfred-code-map", "path": "/tmp/code-map.json"}
    assert exported["repos"][0]["name"] == "web"
    assert exported["repos"][0]["summary"]["files"] == 3
    assert exported["repos"][0]["contracts"]["api_calls"][0]["path"] == "/api/v1/things"


def test_export_codegraph_marks_in_memory_source() -> None:
    exported = export_codegraph(_sample_code_map())

    assert exported["source"] == {"kind": "in-memory-code-map", "path": None}


def test_summarize_codegraph_omits_raw_files() -> None:
    summary = summarize_codegraph(_sample_code_map(), repo="web")

    assert summary["repo_count"] == 1
    assert summary["repos"][0]["name"] == "web"
    assert summary["repos"][0]["api_call_count"] == 1
    assert "files" not in summary["repos"][0]


def test_summarize_codegraph_filters_drift_count_by_repo() -> None:
    code_map = _sample_code_map()
    code_map["repos"]["api"] = {"head_sha": "def456", "graph_summary": {"files": 1}}
    code_map["contract_drift"].append(
        {
            "caller": "api",
            "method": "GET",
            "path": "/v1/other",
            "normalized": "/v1/other",
            "file": "src/other.py:1",
        }
    )

    web_summary = summarize_codegraph(code_map, repo="web")
    all_summary = summarize_codegraph(code_map)

    assert web_summary["contract_drift_count"] == 1
    assert all_summary["contract_drift_count"] == 2


def test_summarize_codegraph_filters_drift_count_by_truncated_repos() -> None:
    code_map = _sample_code_map()
    code_map["repos"]["api"] = {"head_sha": "def456", "graph_summary": {"files": 1}}
    code_map["contract_drift"].append(
        {
            "caller": "api",
            "method": "GET",
            "path": "/v1/other",
            "normalized": "/v1/other",
            "file": "src/other.py:1",
        }
    )

    summary = summarize_codegraph(code_map, limit=1)

    assert [repo["name"] for repo in summary["repos"]] == ["api"]
    assert summary["contract_drift_count"] == 1


def test_impact_for_path_resolves_local_imports_and_contracts() -> None:
    impact = impact_for_path(_sample_code_map(), repo="web", path="src/api.ts")

    assert impact["matched_file"] == "src/api.ts"
    assert impact["match_status"] == "exact"
    assert impact["symbols"] == [{"name": "loadThing", "line": 8}]
    assert impact["imported_by"] == [
        {
            "from": "src/Widget.tsx",
            "to": "./api",
            "resolved_to": "src/api.ts",
            "kind": "import",
        }
    ]
    assert impact["contracts"]["api_calls"][0]["method"] == "GET"
    assert impact["contract_drift"][0]["normalized"] == "/v1/things"


def test_impact_for_path_resolves_parent_directory_imports() -> None:
    code_map = _sample_code_map()
    repo = code_map["repos"]["web"]
    repo["files"].append(
        {
            "path": "src/components/Card.tsx",
            "language": "typescript",
            "symbols": [{"name": "Card", "line": 3}],
            "imports": ["../api"],
        }
    )
    repo["edges"].append({"from": "src/components/Card.tsx", "to": "../api", "kind": "import"})

    impact = impact_for_path(code_map, repo="web", path="src/api.ts")

    assert {
        "from": "src/components/Card.tsx",
        "to": "../api",
        "resolved_to": "src/api.ts",
        "kind": "import",
    } in impact["imported_by"]


def test_impact_for_path_resolves_jsx_directory_imports() -> None:
    code_map = _sample_code_map()
    repo = code_map["repos"]["web"]
    repo["files"].extend(
        [
            {
                "path": "src/Page.jsx",
                "language": "javascript",
                "symbols": [{"name": "Page", "line": 1}],
                "imports": ["./components"],
            },
            {
                "path": "src/components/index.jsx",
                "language": "javascript",
                "symbols": [{"name": "Components", "line": 1}],
                "imports": [],
            },
        ]
    )
    repo["edges"].append({"from": "src/Page.jsx", "to": "./components", "kind": "import"})

    impact = impact_for_path(code_map, repo="web", path="src/components/index.jsx")

    assert impact["imported_by"] == [
        {
            "from": "src/Page.jsx",
            "to": "./components",
            "resolved_to": "src/components/index.jsx",
            "kind": "import",
        }
    ]


def test_impact_for_path_resolves_python_relative_module_imports() -> None:
    code_map = _sample_code_map()
    repo = code_map["repos"]["web"]
    repo["files"].extend(
        [
            {
                "path": "pkg/service.py",
                "language": "python",
                "symbols": [{"name": "Service", "line": 1}],
                "imports": [".utils"],
            },
            {
                "path": "pkg/utils.py",
                "language": "python",
                "symbols": [{"name": "parse", "line": 1}],
                "imports": [],
            },
        ]
    )
    repo["edges"].append({"from": "pkg/service.py", "to": ".utils", "kind": "import"})

    impact = impact_for_path(code_map, repo="web", path="pkg/utils.py")

    assert impact["imported_by"] == [
        {
            "from": "pkg/service.py",
            "to": ".utils",
            "resolved_to": "pkg/utils.py",
            "kind": "import",
        }
    ]


def test_impact_for_missing_path_does_not_match_unresolved_imports() -> None:
    impact = impact_for_path(_sample_code_map(), repo="web", path="src/Missing.ts")

    assert impact["matched_file"] is None
    assert impact["match_status"] == "not_found"
    assert impact["imported_by"] == []
    assert impact["imports_resolved"] == []


def test_impact_for_ambiguous_suffix_match_names_candidates() -> None:
    code_map = _sample_code_map()
    repo = code_map["repos"]["web"]
    repo["files"].append(
        {
            "path": "tests/api.ts",
            "language": "typescript",
            "symbols": [{"name": "testApi", "line": 1}],
            "imports": [],
        }
    )

    impact = impact_for_path(code_map, repo="web", path="api.ts")

    assert impact["matched_file"] is None
    assert impact["match_status"] == "ambiguous"
    assert impact["candidate_matches"] == ["src/api.ts", "tests/api.ts"]


def test_code_map_cli_exports_contract(tmp_path: Path) -> None:
    code_map = tmp_path / "code-map.json"
    code_map.write_text(json.dumps(_sample_code_map()), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "alfred"),
            "code-map",
            "export",
            "--map",
            str(code_map),
            "--summary-only",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == CODEGRAPH_SCHEMA
    assert payload["repos"][0]["name"] == "web"
    assert "files" not in payload["repos"][0]
