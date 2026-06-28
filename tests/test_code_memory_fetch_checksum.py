#!/usr/bin/env python3
"""``bin/code-memory-mcp`` sha256-verifies the auto-fetched release before it is
ever extracted or executed.

The launcher fetches a prebuilt codebase-memory-mcp tarball from a GitHub
release and runs the binary inside it. To close the supply-chain gap (a
compromised upstream account or an overridden repo/version env could otherwise
install a malicious binary), the download is checked against a sha256 pinned
from upstream's published checksums.txt before extraction. These tests drive
the verify path directly through the script's internal ``__verify-checksum``
hook, with no network access: a matching digest passes, and every failure mode
(wrong digest, no pin, missing file) fails closed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "code-memory-mcp"

# Pinned digest the script ships for darwin-arm64, copied from upstream
# checksums.txt for the pinned version. The test recreates a file with exactly
# this digest so the match path is exercised without any download.
DARWIN_ARM64_SHA = "fbd047509852021b5446a11141bcb0a3d1dcaebf6e5112460960f29f052c1c58"


def _verify(file_path: Path, expected: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "__verify-checksum", str(file_path), expected],
        capture_output=True,
        text=True,
    )


def _asset(tmp_path: Path) -> Path:
    """A stand-in release tarball with known bytes (so we know its digest)."""
    blob = tmp_path / "asset.tar.gz"
    blob.write_bytes(b"alfred-code-memory-pinned-asset")
    return blob


def _launcher_env(tmp_path: Path, **updates: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
    }
    env.update(updates)
    return env


def test_verify_passes_on_matching_digest(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest()
    res = _verify(blob, actual)
    assert res.returncode == 0, res.stderr


def test_verify_is_case_insensitive(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest().upper()
    res = _verify(blob, actual)
    assert res.returncode == 0, res.stderr


def test_verify_fails_closed_on_mismatch(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    res = _verify(blob, "deadbeef" * 8)
    assert res.returncode != 0
    assert "MISMATCH" in res.stderr


def test_verify_fails_closed_on_empty_expected(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    res = _verify(blob, "")
    assert res.returncode != 0
    assert "refusing unverified binary" in res.stderr


def test_verify_fails_closed_on_missing_file(tmp_path: Path) -> None:
    res = _verify(tmp_path / "does-not-exist.tar.gz", DARWIN_ARM64_SHA)
    assert res.returncode != 0
    assert "missing" in res.stderr


def test_pinned_tag_resolves_to_published_digest(tmp_path: Path) -> None:
    """Passing a bare platform tag resolves to the pinned digest. A file that
    does NOT have that digest must fail closed, proving the pin is wired in
    (not silently treated as 'no pin = skip')."""
    blob = _asset(tmp_path)
    res = _verify(blob, "darwin-arm64")
    assert res.returncode != 0
    assert "MISMATCH" in res.stderr


def test_pinned_digest_overridable_via_env(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest()
    env = dict(os.environ, ALFRED_CODE_MEMORY_SHA256_DARWIN_ARM64=actual)
    res = subprocess.run(
        ["bash", str(SCRIPT), "__verify-checksum", str(blob), "darwin-arm64"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stderr


def test_fetch_path_has_bounded_curl_timeouts() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "--connect-timeout" in script
    assert "CODE_MEMORY_CONNECT_TIMEOUT_S" in script
    assert "--max-time" in script
    assert "CODE_MEMORY_FETCH_TIMEOUT_S" in script


def test_fetch_timeout_knobs_are_derived_after_env_files_load() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    load_pos = script.index('load_env_file "$ALFRED_HOME/.env"')
    fetch_timeout_pos = script.index("CODE_MEMORY_FETCH_TIMEOUT_S=")
    connect_timeout_pos = script.index("CODE_MEMORY_CONNECT_TIMEOUT_S=")
    assert load_pos < fetch_timeout_pos
    assert load_pos < connect_timeout_pos


def test_scope_repos_auto_discovers_git_repos_when_unconfigured(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "product" / "api" / "packages" / "nested" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    (workspace / "worktree").mkdir()
    (workspace / "worktree" / ".git").write_text("gitdir: ../.git/worktrees/worktree\n")
    (workspace / ".archive" / "old" / ".git").mkdir(parents=True)
    (workspace / "tools" / ".worktrees" / "pr-1" / ".git").mkdir(parents=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [Path(line).relative_to(workspace).as_posix() for line in res.stdout.splitlines()]
    assert repos == ["worktree", "product/api", "tools/alfred-os"]


def test_scope_repos_defaults_to_product_subdir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [
        Path(line).relative_to(workspace / "product").as_posix() for line in res.stdout.splitlines()
    ]
    assert repos == ["api"]


def test_scope_repos_follows_symlinked_workspace_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual-workspace"
    workspace = tmp_path / "workspace-link"
    (actual / "api" / ".git").mkdir(parents=True)
    workspace.symlink_to(actual, target_is_directory=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [Path(line).relative_to(workspace).as_posix() for line in res.stdout.splitlines()]
    assert repos == ["api"]


def test_scope_repos_follows_symlinked_repo_dirs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    actual = tmp_path / "actual"
    (workspace / "real" / ".git").mkdir(parents=True)
    (actual / "api" / ".git").mkdir(parents=True)
    (workspace / "api").symlink_to(actual / "api", target_is_directory=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [Path(line).relative_to(workspace).as_posix() for line in res.stdout.splitlines()]
    assert repos == ["api", "real"]


def test_scope_repos_prefers_configured_scope(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "api" / ".git").mkdir(parents=True)
    (workspace / "web" / ".git").mkdir(parents=True)
    (workspace / "ignored" / ".git").mkdir(parents=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
        ALFRED_CODE_MEMORY_REPOS="web, missing, api",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [Path(line).relative_to(workspace).as_posix() for line in res.stdout.splitlines()]
    assert repos == ["web", "api"]


def test_scope_repos_discovers_top_level_repos_before_nested_repos(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "alpha" / "extra" / ".git").mkdir(parents=True)
    (workspace / "beta" / ".git").mkdir(parents=True)
    (workspace / "gamma" / ".git").mkdir(parents=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
        ALFRED_CODE_MEMORY_DISCOVERY_LIMIT="2",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [Path(line).relative_to(workspace).as_posix() for line in res.stdout.splitlines()]
    assert repos == ["beta", "gamma"]


def test_scope_repos_uses_workspace_subdir_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "product" / "api" / ".git").mkdir(parents=True)
    (workspace / "tools" / "alfred-os" / ".git").mkdir(parents=True)
    env = _launcher_env(
        tmp_path,
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="product",
        ALFRED_CODE_MEMORY_REPOS="",
        ALFRED_CODE_MAP_REPOS="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "__scope-repos"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    repos = [
        Path(line).relative_to(workspace / "product").as_posix() for line in res.stdout.splitlines()
    ]
    assert repos == ["api"]


def test_index_invokes_upstream_cli_index_repository(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "api"
    (repo / ".git").mkdir(parents=True)
    code_home = tmp_path / "code-memory-home"
    log = tmp_path / "upstream.log"
    fake_bin = tmp_path / "codebase-memory-mcp"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'printf "HOME=%s\\n" "$HOME" >> "$CODE_MEMORY_TEST_LOG"\n'
        'printf "ARG1=%s\\nARG2=%s\\nARG3=%s\\n" "$1" "$2" "$3" >> "$CODE_MEMORY_TEST_LOG"\n',
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    env = _launcher_env(
        tmp_path,
        ALFRED_CODE_MEMORY_BIN=str(fake_bin),
        ALFRED_CODE_MEMORY_AUTOFETCH="0",
        ALFRED_CODE_MEMORY_HOME=str(code_home),
        ALFRED_CODE_MEMORY_REPOS="api",
        ALFRED_CODE_MAP_REPOS="",
        CODE_MEMORY_TEST_LOG=str(log),
        WORKSPACE_ROOT=str(workspace),
        WORKSPACE_SUBDIR="",
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "index"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    text = log.read_text(encoding="utf-8")
    assert f"HOME={code_home}" in text
    assert "ARG1=cli" in text
    assert "ARG2=index_repository" in text
    assert f'"repo_path":"{repo}"' in text


def test_serve_runs_upstream_stdio_server_with_code_memory_home(tmp_path: Path) -> None:
    code_home = tmp_path / "code-memory-home"
    log = tmp_path / "serve.log"
    fake_bin = tmp_path / "codebase-memory-mcp"
    fake_bin.write_text(
        "#!/bin/sh\n"
        'printf "HOME=%s\\n" "$HOME" >> "$CODE_MEMORY_TEST_LOG"\n'
        'printf "ARGS=%s\\n" "$*" >> "$CODE_MEMORY_TEST_LOG"\n',
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    env = _launcher_env(
        tmp_path,
        ALFRED_CODE_MEMORY_BIN=str(fake_bin),
        ALFRED_CODE_MEMORY_AUTOFETCH="0",
        ALFRED_CODE_MEMORY_HOME=str(code_home),
        CODE_MEMORY_TEST_LOG=str(log),
    )

    res = subprocess.run(
        ["bash", str(SCRIPT), "serve", "--probe"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    text = log.read_text(encoding="utf-8")
    assert f"HOME={code_home}" in text
    assert "ARGS=--probe" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
