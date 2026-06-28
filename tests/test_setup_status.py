"""Setup-status probes used by the desktop onboarding flow."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from server import setup as setup_mod  # noqa: E402


def _stub_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octocat", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/usr/local/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "selected_repos", lambda: ["octocat/web"])
    monkeypatch.setattr(setup_mod, "load_demo_cards", lambda: {})


def _isolate_launcher_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    alfred_home = tmp_path / ".alfred"
    home.mkdir(exist_ok=True)
    alfred_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(alfred_home))
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_WORKSPACE_SUBDIR", raising=False)
    monkeypatch.delenv("WORKSPACE_SUBDIR", raising=False)


def test_bootstrap_status_reports_code_memory_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_BIN", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_AUTOFETCH", raising=False)

    payload = setup_mod.bootstrap_status()

    code_memory = payload["code_memory"]
    assert code_memory["enabled"] is True
    assert code_memory["autofetch"] is True
    assert code_memory["binary"]["resolved"] is False
    assert code_memory["binary"]["source"] == "none"
    assert code_memory["version_pin"] == "v0.8.1"
    assert code_memory["repo"] == "DeusData/codebase-memory-mcp"
    assert code_memory["index_dir"] == str(tmp_path / ".alfred" / "state" / "code-memory")
    assert code_memory["index_present"] is False


def test_bootstrap_status_reports_configured_code_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    index_dir = tmp_path / "index"
    graph_dir = index_dir / ".cache" / "codebase-memory-mcp"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.db").write_text("ok", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "api" / ".git").mkdir(parents=True)
    (workspace / "web" / ".git").mkdir(parents=True)

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "api, web, api")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")

    payload = setup_mod.bootstrap_status()

    code_memory = payload["code_memory"]
    assert code_memory["binary"] == {
        "resolved": True,
        "path": str(binary),
        "source": "env",
        "configured": str(binary),
    }
    assert code_memory["index_present"] is True
    assert code_memory["repos"] == {
        "configured": ["api", "web"],
        "configured_existing": ["api", "web"],
        "discovered": [],
        "selected": ["api", "web"],
        "source": "configured",
        "count": 2,
        "limit": 25,
    }
    assert code_memory["detail"] == "Code-memory binary and index are present."


def test_bootstrap_status_ignores_legacy_index_dir_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    (index_dir / "legacy.db").write_text("stale", encoding="utf-8")

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_INDEX_DIR", str(index_dir))

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["index_dir"] == str(index_dir)
    assert code_memory["graph_dir"] == str(index_dir / ".cache" / "codebase-memory-mcp")
    assert code_memory["index_present"] is False
    assert (
        code_memory["detail"]
        == "Code-memory binary is present; run an index before relying on graph queries."
    )


def test_bootstrap_status_checks_code_memory_home_cache_for_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    index_dir = tmp_path / "legacy-index"
    code_home = tmp_path / "code-memory-home"
    graph_dir = code_home / ".cache" / "codebase-memory-mcp"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.db").write_text("ok", encoding="utf-8")

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_HOME", str(code_home))

    payload = setup_mod.bootstrap_status()

    code_memory = payload["code_memory"]
    assert code_memory["index_dir"] == str(index_dir)
    assert code_memory["index_home"] == str(code_home)
    assert code_memory["graph_dir"] == str(graph_dir)
    assert code_memory["index_present"] is True
    assert code_memory["detail"] == "Code-memory binary and index are present."


def test_bootstrap_status_checks_upstream_cbm_cache_dir_for_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    index_dir = tmp_path / "legacy-index"
    code_home = tmp_path / "code-memory-home"
    cbm_cache = tmp_path / "upstream-cache"
    cbm_cache.mkdir(parents=True)
    (cbm_cache / "graph.db").write_text("ok", encoding="utf-8")

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_HOME", str(code_home))
    monkeypatch.setenv("CBM_CACHE_DIR", str(cbm_cache))

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["index_dir"] == str(index_dir)
    assert code_memory["index_home"] == str(code_home)
    assert code_memory["graph_dir"] == str(cbm_cache)
    assert code_memory["index_present"] is True
    assert code_memory["detail"] == "Code-memory binary and index are present."


def test_bootstrap_status_ignores_empty_code_memory_cache_scaffolding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    binary = tmp_path / "codebase-memory-mcp"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    code_home = tmp_path / "code-memory-home"
    graph_dir = code_home / ".cache" / "codebase-memory-mcp"
    graph_dir.mkdir(parents=True)

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_HOME", str(code_home))

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["index_home"] == str(code_home)
    assert code_memory["graph_dir"] == str(graph_dir)
    assert code_memory["index_present"] is False
    assert "run an index" in code_memory["detail"]


def test_bootstrap_status_reads_code_memory_launcher_env_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    home = tmp_path / "home"
    alfred_home = tmp_path / "runtime"
    home.mkdir()
    alfred_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    (home / ".alfredrc").write_text(
        "\n".join(
            [
                f"ALFRED_HOME={alfred_home}",
                "ALFRED_CODE_MEMORY_MCP=0",
                "ALFRED_CODE_MEMORY_AUTOFETCH=0",
            ]
        ),
        encoding="utf-8",
    )

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["enabled"] is False
    assert code_memory["autofetch"] is False
    assert code_memory["index_dir"] == str(alfred_home / "state" / "code-memory")


def test_setup_config_prefers_process_env_over_runtime_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text(
        "\n".join(
            [
                "CLAUDE_BIN=/file/claude",
                "CODEX_BIN=/file/codex",
                "GH_ORG=file-org",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("CLAUDE_BIN", "/env/claude")
    monkeypatch.setenv("CODEX_BIN", "/env/codex")
    monkeypatch.setenv("GH_ORG", "env-org")
    monkeypatch.setattr(setup_mod, "selected_repos", lambda: [])

    launcher_env = setup_mod._code_memory_launcher_env()
    engines = {item["name"]: item for item in setup_mod.engine_clis()}

    assert launcher_env["CLAUDE_BIN"] == "/env/claude"
    assert launcher_env["CODEX_BIN"] == "/env/codex"
    assert launcher_env["GH_ORG"] == "env-org"
    assert engines["claude"]["path"] == "/env/claude"
    assert engines["codex"]["path"] == "/env/codex"
    assert setup_mod._repo_list_owners() == ["env-org"]


def test_gh_subprocess_env_drops_empty_path_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")

    parts = setup_mod._gh_subprocess_env()["PATH"].split(os.pathsep)

    assert "" not in parts
    assert "." not in parts
    assert str(home / ".local" / "bin") in parts


def test_selected_repos_preserves_shipped_and_bridge_fallbacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_QUEUE_REPOS", raising=False)
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "octocat/web")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "octocat/api, octocat/web")

    assert setup_mod.selected_repos() == ["octocat/api", "octocat/web"]


def test_selected_repos_prefers_primary_queue_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "octocat/web")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "acme/frontend")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "acme/api")

    assert setup_mod.selected_repos() == ["octocat/web"]


def test_bootstrap_status_matches_case_insensitive_launcher_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_AUTOFETCH", raising=False)
    (home / ".alfredrc").write_text(
        "ALFRED_CODE_MEMORY_AUTOFETCH=False\n",
        encoding="utf-8",
    )

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["autofetch"] is False
    assert "autofetch is disabled" in code_memory["detail"]


def test_bootstrap_status_falls_back_after_stale_code_memory_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    cache_bin = tmp_path / ".alfred" / "bin" / "codebase-memory-mcp"
    cache_bin.parent.mkdir(parents=True)
    cache_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    cache_bin.chmod(cache_bin.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(tmp_path / "removed-binary"))

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["binary"] == {
        "resolved": True,
        "path": str(cache_bin),
        "source": "cache",
        "configured": str(tmp_path / "removed-binary"),
    }


def test_bootstrap_status_respects_code_memory_disable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_MCP", "0")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_AUTOFETCH", "0")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "api, web")
    monkeypatch.setattr(
        setup_mod,
        "_discover_code_memory_repos",
        lambda _env: pytest.fail("disabled code memory must not crawl workspace repos"),
    )

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["enabled"] is False
    assert code_memory["autofetch"] is False
    assert code_memory["repos"] == {
        "configured": ["api", "web"],
        "configured_existing": [],
        "discovered": [],
        "selected": ["api", "web"],
        "source": "configured",
        "count": 2,
        "limit": 25,
    }
    assert code_memory["detail"] == "Code memory is disabled with ALFRED_CODE_MEMORY_MCP."


def test_capability_plane_reports_missing_optional_layers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("ALFRED_CONTEXT_COMPRESSION", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_BIN", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_AUTOFETCH", raising=False)

    payload = setup_mod.capability_status()
    by_key = {item["key"]: item for item in payload["capabilities"]}

    assert payload["summary"] == {"ready": 0, "actionable": 3, "disabled": 0, "total": 3}
    assert by_key["code_graph"]["state"] == "installable"
    assert by_key["context_compression"]["state"] == "missing"
    assert by_key["engineering_skills"]["state"] == "missing"


def test_capability_plane_reports_external_layers_without_headroom_runner_wiring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex"
    (codex_home / "skills" / "gstack").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.setenv("ALFRED_CONTEXT_COMPRESSION", "1")

    def fake_which(name: str, **_kwargs: object) -> str | None:
        return "/opt/homebrew/bin/headroom" if name == "headroom" else None

    monkeypatch.setattr(setup_mod.shutil, "which", fake_which)
    code_memory = {
        "enabled": True,
        "autofetch": True,
        "binary": {
            "resolved": True,
            "path": "/usr/local/bin/codebase-memory-mcp",
            "source": "path",
            "configured": None,
        },
        "version_pin": "v0.8.1",
        "repo": "DeusData/codebase-memory-mcp",
        "index_dir": str(tmp_path / "index"),
        "index_present": True,
        "repos": {"configured": ["api"], "count": 1},
        "detail": "Code-memory binary and index are present.",
    }

    payload = setup_mod.capability_status(code_memory)
    by_key = {item["key"]: item for item in payload["capabilities"]}

    assert payload["summary"]["ready"] == 2
    assert payload["summary"]["actionable"] == 1
    assert by_key["code_graph"]["state"] == "ready"
    assert by_key["context_compression"]["state"] == "available"
    assert by_key["engineering_skills"]["state"] == "ready"
    assert by_key["engineering_skills"]["detected"]["paths"] == [
        str(codex_home / "skills" / "gstack")
    ]


def test_capability_plane_requires_context_compression_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    monkeypatch.delenv("ALFRED_CONTEXT_COMPRESSION", raising=False)
    monkeypatch.setattr(
        setup_mod.shutil,
        "which",
        lambda name, **_kwargs: "/opt/homebrew/bin/headroom" if name == "headroom" else None,
    )

    payload = setup_mod.capability_status()
    context = {item["key"]: item for item in payload["capabilities"]}["context_compression"]

    assert context["state"] == "available"
    assert context["installed"] is True
    assert context["enabled"] is False


def test_capability_plane_reads_context_compression_from_runtime_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_CONTEXT_COMPRESSION=0\n", encoding="utf-8")
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_CONTEXT_COMPRESSION", raising=False)
    monkeypatch.setattr(
        setup_mod.shutil,
        "which",
        lambda name, **_kwargs: "/opt/homebrew/bin/headroom" if name == "headroom" else None,
    )

    payload = setup_mod.capability_status()
    context = {item["key"]: item for item in payload["capabilities"]}["context_compression"]

    assert context["state"] == "available"
    assert context["installed"] is True
    assert context["enabled"] is False


def test_capability_plane_uses_explicit_skill_homes_without_resolving_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    (codex_home / "skills" / "gstack").mkdir(parents=True)
    (claude_home / "skills").mkdir(parents=True)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setattr(
        setup_mod.Path,
        "home",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))),
    )
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)

    payload = setup_mod.capability_status()
    skills = {item["key"]: item for item in payload["capabilities"]}["engineering_skills"]

    assert skills["state"] == "ready"
    assert skills["detected"]["paths"] == [str(codex_home / "skills" / "gstack")]


def test_setup_module_cold_import_survives_without_agent_runner_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = os.environ.copy()
    env.pop("HOME", None)
    env["PYTHONPATH"] = str(ROOT / "lib")
    code = """
import builtins
import pathlib

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "agent_runner.paths":
        raise RuntimeError("agent_runner.paths import should not be needed")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
pathlib.Path.home = staticmethod(
    lambda: (_ for _ in ()).throw(RuntimeError("no home"))
)

from server.setup import capability_status

print(capability_status()["summary"]["total"])
"""

    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "3"


def test_bootstrap_status_demo_fallback_survives_unresolvable_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    codex_home.mkdir()
    claude_home.mkdir()
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_BIN", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setattr(
        setup_mod,
        "gh_auth_status",
        lambda: {"ok": True, "account": "octocat", "detail": "Signed in."},
    )
    monkeypatch.setattr(
        setup_mod,
        "engine_clis",
        lambda: [{"name": "codex", "installed": True, "path": "/usr/local/bin/codex"}],
    )
    monkeypatch.setattr(setup_mod, "selected_repos", lambda: ["octocat/web"])
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        setup_mod.os.path,
        "expanduser",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no home")),
    )
    monkeypatch.setattr(
        setup_mod.Path,
        "home",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))),
    )

    payload = setup_mod.bootstrap_status()

    assert payload["demo"] == {"present": False}
    assert payload["capability_plane"]["summary"]["total"] == 3
    assert payload["ready"] is True


def test_bootstrap_status_avoids_home_dependent_runtime_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import builtins

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    gh_bin = tmp_path / "gh"
    gh_bin.write_text(
        '#!/bin/sh\nprintf "Logged in to github.com as octocat\\n" >&2\n',
        encoding="utf-8",
    )
    gh_bin.chmod(0o755)
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_bin.chmod(0o755)

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("ALFRED_QUEUE_REPOS", "octocat/web")
    monkeypatch.setenv("GH_BIN", str(gh_bin))
    monkeypatch.setenv("CODEX_BIN", str(codex_bin))
    monkeypatch.delenv("ALFRED_CODE_MEMORY_BIN", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_MCP", raising=False)
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "acme/frontend")
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "acme/api")
    monkeypatch.setattr(
        setup_mod.Path,
        "home",
        staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no home"))),
    )

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        blocked = {"agent_runner.paths", "issue_queue", "shipped_board"}
        if name in blocked:
            raise RuntimeError(f"{name} import should not be needed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    payload = setup_mod.bootstrap_status()

    assert payload["github"]["ok"] is True
    assert payload["repos"]["selected"] == ["octocat/web"]
    assert payload["ready"] is True


def test_bootstrap_status_auto_discovers_code_memory_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "product" / "api" / "packages" / "nested" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    (workspace / "worktree").mkdir()
    (workspace / "worktree" / ".git").write_text("gitdir: ../.git/worktrees/worktree\n")
    (workspace / ".archive" / "old" / ".git").mkdir(parents=True)
    (workspace / "tools" / ".worktrees" / "pr-1" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"] == {
        "configured": [],
        "configured_existing": [],
        "discovered": ["worktree", "product/api", "tools/alfred-os"],
        "selected": ["worktree", "product/api", "tools/alfred-os"],
        "source": "auto",
        "count": 3,
        "limit": 25,
    }


def test_bootstrap_status_follows_symlinked_code_memory_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    actual = tmp_path / "actual"
    (workspace / "real" / ".git").mkdir(parents=True)
    (actual / "api" / ".git").mkdir(parents=True)
    (workspace / "api").symlink_to(actual / "api", target_is_directory=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"]["selected"] == ["api", "real"]
    assert code_memory["repos"]["discovered"] == ["api", "real"]


def test_bootstrap_status_follows_symlinked_code_memory_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    actual = tmp_path / "actual-workspace"
    workspace = tmp_path / "workspace-link"
    (actual / "api" / ".git").mkdir(parents=True)
    workspace.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"]["selected"] == ["api"]
    assert code_memory["repos"]["discovered"] == ["api"]


def test_bootstrap_status_defaults_code_memory_to_product_subdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.delenv("WORKSPACE_SUBDIR", raising=False)
    monkeypatch.delenv("ALFRED_WORKSPACE_SUBDIR", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"]["selected"] == ["api"]
    assert code_memory["repos"]["source"] == "auto"


def test_bootstrap_status_prefers_existing_configured_code_memory_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "api" / ".git").mkdir(parents=True)
    (workspace / "web" / ".git").mkdir(parents=True)
    (workspace / "ignored" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "web, missing, my repo, api")
    (workspace / "myrepo" / ".git").mkdir(parents=True)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"] == {
        "configured": ["web", "missing", "myrepo", "api"],
        "configured_existing": ["web", "myrepo", "api"],
        "discovered": [],
        "selected": ["web", "myrepo", "api"],
        "source": "configured",
        "count": 3,
        "limit": 25,
    }


def test_bootstrap_status_falls_back_when_configured_code_memory_repos_are_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "old-alfred")

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"] == {
        "configured": ["old-alfred"],
        "configured_existing": [],
        "discovered": ["api"],
        "selected": ["api"],
        "source": "auto-fallback",
        "count": 1,
        "limit": 25,
    }


def test_bootstrap_status_falls_back_when_configured_code_memory_dirs_are_not_git_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "api" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "docs")

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"] == {
        "configured": ["docs"],
        "configured_existing": [],
        "discovered": ["api"],
        "selected": ["api"],
        "source": "auto-fallback",
        "count": 1,
        "limit": 25,
    }


def test_bootstrap_status_discovers_top_level_repos_before_nested_repos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "alpha" / "extra" / ".git").mkdir(parents=True)
    (workspace / "beta" / ".git").mkdir(parents=True)
    (workspace / "gamma" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "")
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)
    monkeypatch.setenv("ALFRED_CODE_MEMORY_DISCOVERY_LIMIT", "2")

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"]["selected"] == ["beta", "gamma"]


def test_bootstrap_status_uses_workspace_subdir_fallback_for_code_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_common(monkeypatch)
    _isolate_launcher_env(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WORKSPACE_SUBDIR", "product")
    monkeypatch.delenv("ALFRED_WORKSPACE_SUBDIR", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MEMORY_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_CODE_MAP_REPOS", raising=False)

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["repos"]["selected"] == ["api"]
    assert code_memory["repos"]["source"] == "auto"
