from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

spec = importlib.util.spec_from_file_location(
    "code_map_refresh", ROOT / "bin" / "code-map-refresh.py"
)
cmr = importlib.util.module_from_spec(spec)
assert spec.loader is not None
_old_alfred_home = os.environ.get("ALFRED_HOME")
os.environ["ALFRED_HOME"] = str(ROOT)
try:
    spec.loader.exec_module(cmr)
finally:
    if _old_alfred_home is None:
        os.environ.pop("ALFRED_HOME", None)
    else:
        os.environ["ALFRED_HOME"] = _old_alfred_home


def test_env_int_defaults_on_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_CODE_MAP_MAX_FILES", "not-a-number")
    assert cmr._env_int("ALFRED_CODE_MAP_MAX_FILES", 2000) == 2000
    monkeypatch.setenv("ALFRED_CODE_MAP_MAX_FILES", "0")
    assert cmr._env_int("ALFRED_CODE_MAP_MAX_FILES", 2000) == 1


def test_scan_repo_graph_extracts_files_symbols_and_import_edges(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        """
import os, sys
import numpy as np
from alfred.runner import main

class Batman:
    pass

def plan_work():
    return main()
""",
        encoding="utf-8",
    )
    (tmp_path / "src" / "panel.ts").write_text(
        """
import { h } from './view'
import type { Run } from '../types'
export function render(run: Run) {
  return h(run)
}
""",
        encoding="utf-8",
    )

    graph = cmr.scan_repo_graph(tmp_path)

    assert graph["graph_summary"]["files"] == 2
    assert graph["graph_summary"]["symbols"] == 3
    assert graph["graph_summary"]["imports"] == 6
    assert graph["graph_summary"]["languages"] == {"python": 1, "typescript": 1}
    assert {"from": "src/main.py", "to": "os", "kind": "import"} in graph["edges"]
    assert {"from": "src/main.py", "to": "numpy", "kind": "import"} in graph["edges"]
    assert {"from": "src/main.py", "to": "numpy as np", "kind": "import"} not in graph["edges"]
    assert {"from": "src/main.py", "to": "alfred.runner", "kind": "import"} in graph["edges"]
    assert {"from": "src/panel.ts", "to": "./view", "kind": "import"} in graph["edges"]

    symbols = {
        (file_info["path"], symbol["name"])
        for file_info in graph["files"]
        for symbol in file_info["symbols"]
    }
    assert ("src/main.py", "Batman") in symbols
    assert ("src/main.py", "plan_work") in symbols
    assert ("src/panel.ts", "render") in symbols


def test_scan_repo_graph_prunes_skip_dirs_and_marks_truncation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cmr, "MAX_GRAPH_FILES", 2)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("import sys\n", encoding="utf-8")
    (tmp_path / "src" / "c.py").write_text("import json\n", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "skip.ts").write_text(
        "import { hidden } from 'hidden'\n",
        encoding="utf-8",
    )

    graph = cmr.scan_repo_graph(tmp_path)

    assert graph["graph_summary"]["files"] == 2
    assert graph["graph_summary"]["truncated"] is True
    assert all("node_modules" not in file_info["path"] for file_info in graph["files"])
    assert all(edge["to"] != "hidden" for edge in graph["edges"])


def test_scan_backend_includes_repo_graph_next_to_endpoints(tmp_path: Path) -> None:
    src = tmp_path / "api" / "src" / "main" / "kotlin" / "com" / "example"
    src.mkdir(parents=True)
    (src / "ThingResource.kt").write_text(
        """
package com.example

import com.example.model.Thing

@Path("/v1/things")
class ThingResource {
    @GET
    @Path("/{id}")
    fun getThing(): Thing = TODO()
}
""",
        encoding="utf-8",
    )

    out = cmr.scan_backend(tmp_path)

    assert out["endpoints"] == [
        {
            "method": "GET",
            "path": "/v1/things/{id}",
            "file": "api/src/main/kotlin/com/example/ThingResource.kt:8",
        }
    ]
    assert out["graph_summary"]["files"] == 1
    assert out["graph_summary"]["symbols"] == 2
    assert out["graph_summary"]["imports"] == 1


def test_load_code_map_summarizes_repo_graph(tmp_path: Path) -> None:
    from compose_converse import load_code_map

    path = tmp_path / "code-map.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-21T12:00:00Z",
                "repos": {
                    "backend": {
                        "endpoints": [{"method": "GET", "path": "/v1/health"}],
                        "graph_summary": {
                            "files": 9,
                            "symbols": 42,
                            "imports": 17,
                            "languages": {"kotlin": 9},
                            "truncated": False,
                        },
                    }
                },
                "contract_drift": [{"caller": "frontend"}],
            }
        ),
        encoding="utf-8",
    )

    summary = load_code_map(path)

    assert "`backend`: 1 server endpoints, 9 files, 42 symbols, 17 imports" in summary
    assert "languages: kotlin:9" in summary
    assert "Contract drift entries: 1" in summary
