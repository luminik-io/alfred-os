"""Regression tests for bin/agent-launch interpreter and env resolution."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_LAUNCH = REPO_ROOT / "bin" / "agent-launch"


def _make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def alfred_home(tmp_path: Path) -> Path:
    home_root = tmp_path / "home"
    home_root.mkdir()
    alfred = home_root / ".alfred"
    (alfred / "bin").mkdir(parents=True)
    venv_bin = alfred / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_stub = venv_bin / "python"
    python_stub.write_text('#!/usr/bin/env bash\necho "venv-python" "$@"\n')
    _make_executable(python_stub)
    return alfred


def _run_env(
    target: Path,
    *,
    alfred_home: Path | None = None,
    home: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in (
        "ALFRED_PYTHON",
        "ALFREDRC",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ALFRED_AUTO_PROMOTE",
        "ALFRED_AUTO_PROMOTE_KILL",
        "ALFRED_AUTO_PROMOTE_LLM_JUDGE",
        "ALFRED_QUEUE_REPOS",
        "ALFRED_SHIPPED_REPOS",
        "ALFRED_BRIDGE_REPOS",
        "ALFRED_CODE_MEMORY_REPOS",
        "WORKSPACE_ROOT",
    ):
        env.pop(key, None)
    if home is not None:
        env["HOME"] = str(home)
    elif alfred_home is not None:
        env["HOME"] = str(alfred_home.parent)
    if alfred_home is not None:
        env["ALFRED_HOME"] = str(alfred_home)
    else:
        env.pop("ALFRED_HOME", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shell_target_runs_via_own_shebang(tmp_path: Path, alfred_home: Path) -> None:
    target = tmp_path / "fleet-recap.sh"
    target.write_text('#!/usr/bin/env bash\necho "ran shell directly"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "ran shell directly" in proc.stdout
    assert "venv-python" not in proc.stdout


def test_dotpy_target_routes_through_venv(tmp_path: Path, alfred_home: Path) -> None:
    target = tmp_path / "lucius.py"
    target.write_text("#!/usr/bin/env python3\nprint('python ran')\n")
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("venv-python")
    assert str(target) in proc.stdout


def test_python_shebang_without_dotpy_routes_through_venv(
    tmp_path: Path, alfred_home: Path
) -> None:
    target = tmp_path / "drake"
    target.write_text("#!/usr/bin/env python3\nprint('python ran')\n")
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("venv-python")


def test_alfred_python_override_only_applies_to_python_targets(
    tmp_path: Path, alfred_home: Path
) -> None:
    override_python = tmp_path / "override-python"
    override_python.write_text('#!/usr/bin/env bash\necho "override-python" "$@"\n')
    _make_executable(override_python)

    shell_target = tmp_path / "shipped-summary.sh"
    shell_target.write_text('#!/usr/bin/env bash\necho "shell-direct"\n')
    _make_executable(shell_target)

    proc = _run_env(
        shell_target,
        alfred_home=alfred_home,
        extra_env={"ALFRED_PYTHON": str(override_python)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "shell-direct" in proc.stdout
    assert "override-python" not in proc.stdout


def test_agent_launch_loads_token_from_env_file(tmp_path: Path, alfred_home: Path) -> None:
    (alfred_home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    target = tmp_path / "echo-token.sh"
    target.write_text('#!/usr/bin/env bash\necho "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "TOKEN=sk-ant-oat01-fromdotenv" in proc.stdout


def test_agent_launch_env_file_does_not_clobber_real_env(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    target = tmp_path / "echo-token.sh"
    target.write_text('#!/usr/bin/env bash\necho "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n')
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fromenv"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "TOKEN=sk-ant-oat01-fromenv" in proc.stdout


def test_agent_launch_ignores_home_alfredrc(tmp_path: Path, alfred_home: Path) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromrc\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    target = tmp_path / "echo-token.sh"
    target.write_text('#!/usr/bin/env bash\necho "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "TOKEN=sk-ant-oat01-fromdotenv" in proc.stdout
    assert "fromrc" not in proc.stdout


def test_agent_launch_ignores_alfredrc_environment_pointer(
    tmp_path: Path, alfred_home: Path
) -> None:
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    target = tmp_path / "echo-auto-promote.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "RC=${ALFREDRC:-unset}"\n'
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFREDRC": str(custom_rc)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "RC=unset" in proc.stdout
    assert "AUTO=unset" in proc.stdout


def test_env_file_setup_managed_repo_scope_overrides_stale_process_env(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text("ALFRED_SHIPPED_REPOS=org/new\n", encoding="utf-8")
    target = tmp_path / "echo-repos.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_SHIPPED_REPOS:-unset}"\n')
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFRED_SHIPPED_REPOS": "org/process"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/process" in proc.stdout


def test_env_file_code_memory_settings_load_when_process_absent(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text("ALFRED_CODE_MEMORY_REPOS=org/new\n", encoding="utf-8")
    target = tmp_path / "echo-code-memory.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_CODE_MEMORY_REPOS:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/new" in proc.stdout


def test_env_file_stop_controls_override_stale_process_env(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=0\n"
        "ALFRED_AUTO_PROMOTE_KILL=1\n"
        "ALFRED_AUTO_PROMOTE_LLM_JUDGE=treu\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-memory-stop.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
        'echo "KILL=${ALFRED_AUTO_PROMOTE_KILL:-unset}"\n'
        'echo "JUDGE=${ALFRED_AUTO_PROMOTE_LLM_JUDGE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={
            "ALFRED_AUTO_PROMOTE": "1",
            "ALFRED_AUTO_PROMOTE_KILL": "0",
            "ALFRED_AUTO_PROMOTE_LLM_JUDGE": "1",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=1" in proc.stdout
    assert "JUDGE=treu" in proc.stdout


def test_agent_launch_expands_process_alfred_home_before_env_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/expanded\n", encoding="utf-8")
    target = tmp_path / "echo-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        home=home,
        extra_env={"ALFRED_HOME": "~/runtime"},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={runtime}" in proc.stdout
    assert "REPOS=org/expanded" in proc.stdout


def test_agent_launch_empty_alfred_home_uses_default_runtime_env(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default_runtime = home / ".alfred"
    default_runtime.mkdir(parents=True)
    (default_runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/default\n", encoding="utf-8")
    target = tmp_path / "echo-default-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, home=home, extra_env={"ALFRED_HOME": ""})

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={default_runtime}" in proc.stdout
    assert "REPOS=org/default" in proc.stdout


def test_agent_launch_preserves_process_layout_over_env_file(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text("WORKSPACE_ROOT=/stale\n", encoding="utf-8")
    workspace = tmp_path / "process-workspace"
    target = tmp_path / "echo-layout.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "WORKSPACE=${WORKSPACE_ROOT:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"WORKSPACE_ROOT": str(workspace)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={alfred_home}" in proc.stdout
    assert f"WORKSPACE={workspace}" in proc.stdout


def test_bash_available() -> None:
    assert shutil.which("bash"), "bash not on PATH; cannot run agent-launch tests"
