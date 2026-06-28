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

    monkeypatch.setenv("ALFRED_CODE_MEMORY_BIN", str(binary))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_INDEX_DIR", str(index_dir))
    monkeypatch.setenv("ALFRED_CODE_MEMORY_REPOS", "api, web, api")

    payload = setup_mod.bootstrap_status()

    code_memory = payload["code_memory"]
    assert code_memory["binary"] == {
        "resolved": True,
        "path": str(binary),
        "source": "env",
        "configured": str(binary),
    }
    assert code_memory["index_present"] is True
    assert code_memory["repos"] == {"configured": ["api", "web"], "count": 2}
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


def test_capability_plane_reports_ready_external_layers(
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

    assert payload["summary"]["ready"] == 3
    assert by_key["code_graph"]["state"] == "ready"
    assert by_key["context_compression"]["state"] == "ready"
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
