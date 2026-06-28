"""Setup-status probes used by the desktop onboarding flow."""

from __future__ import annotations

import stat
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
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
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
    index_dir.mkdir()
    (index_dir / "graph.db").write_text("ok", encoding="utf-8")
    workspace = tmp_path / "workspace"
    (workspace / "api").mkdir(parents=True)
    (workspace / "web").mkdir(parents=True)

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
    monkeypatch.setattr(setup_mod.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_MCP", "0")
    monkeypatch.setenv("ALFRED_CODE_MEMORY_AUTOFETCH", "0")

    code_memory = setup_mod.bootstrap_status()["code_memory"]

    assert code_memory["enabled"] is False
    assert code_memory["autofetch"] is False
    assert code_memory["detail"] == "Code memory is disabled with ALFRED_CODE_MEMORY_MCP."


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
        "discovered": ["product/api", "tools/alfred-os", "worktree"],
        "selected": ["product/api", "tools/alfred-os", "worktree"],
        "source": "auto",
        "count": 3,
        "limit": 25,
    }


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
