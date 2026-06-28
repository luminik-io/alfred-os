"""Regression tests for bin/agent-launch interpreter resolution.

Pinned behaviour: only Python targets (`.py` or shebang containing
"python") may be re-execed through the venv interpreter. Shell scripts
must always run via their own shebang so the venv-python path never
tries to parse `.sh` files as Python source (regression from PR #102).
"""

from __future__ import annotations

import os
import pwd
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


def _run(
    target: Path, *, alfred_home: Path, alfred_python: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(alfred_home)
    env.pop("ALFRED_PYTHON", None)
    env.pop("ALFREDRC", None)
    if alfred_python is not None:
        env["ALFRED_PYTHON"] = alfred_python
    # Empty HOME so load_env_file does not pick up a real ~/.alfredrc.
    env["HOME"] = str(alfred_home.parent)
    return subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def alfred_home(tmp_path: Path) -> Path:
    home_root = tmp_path / "home"
    home_root.mkdir()
    alfred = home_root / ".alfred"
    (alfred / "bin").mkdir(parents=True)
    venv_bin = alfred / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    # Stub venv python that prints a marker so we know it was the one invoked.
    python_stub = venv_bin / "python"
    python_stub.write_text('#!/usr/bin/env bash\necho "venv-python" "$@"\n')
    _make_executable(python_stub)
    return alfred


def test_shell_target_runs_via_own_shebang(tmp_path: Path, alfred_home: Path) -> None:
    """Regression: PR #102's blanket exec broke .sh agents.

    A shell-script target must NOT be handed to $ALFRED_HOME/venv/bin/python.
    """
    target = tmp_path / "fleet-recap.sh"
    target.write_text('#!/usr/bin/env bash\necho "ran shell directly"\n')
    _make_executable(target)

    proc = _run(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "ran shell directly" in proc.stdout
    assert "venv-python" not in proc.stdout


def test_dotpy_target_routes_through_venv(tmp_path: Path, alfred_home: Path) -> None:
    """Python targets (by .py extension) get the venv interpreter."""
    target = tmp_path / "lucius.py"
    target.write_text("#!/usr/bin/env python3\nprint('python ran')\n")
    _make_executable(target)

    proc = _run(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("venv-python")
    assert str(target) in proc.stdout


def test_python_shebang_without_dotpy_routes_through_venv(
    tmp_path: Path, alfred_home: Path
) -> None:
    """No `.py` suffix but a python shebang still routes through venv."""
    target = tmp_path / "drake"
    target.write_text("#!/usr/bin/env python3\nprint('python ran')\n")
    _make_executable(target)

    proc = _run(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("venv-python")


def test_alfred_python_override_only_applies_to_python_targets(
    tmp_path: Path, alfred_home: Path
) -> None:
    """ALFRED_PYTHON must not be inflicted on shell targets either."""
    override_python = tmp_path / "override-python"
    override_python.write_text('#!/usr/bin/env bash\necho "override-python" "$@"\n')
    _make_executable(override_python)

    shell_target = tmp_path / "shipped-summary.sh"
    shell_target.write_text('#!/usr/bin/env bash\necho "shell-direct"\n')
    _make_executable(shell_target)

    proc = _run(shell_target, alfred_home=alfred_home, alfred_python=str(override_python))

    assert proc.returncode == 0, proc.stderr
    assert "shell-direct" in proc.stdout
    assert "override-python" not in proc.stdout


def _run_env(
    target: Path, *, alfred_home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(alfred_home)
    env.pop("ALFRED_PYTHON", None)
    env.pop("ALFREDRC", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("ALFRED_AUTO_PROMOTE", None)
    env.pop("ALFRED_AUTO_PROMOTE_KILL", None)
    env.pop("ALFRED_AUTO_PROMOTE_LLM_JUDGE", None)
    env.pop("ALFRED_QUEUE_REPOS", None)
    env.pop("ALFRED_SHIPPED_REPOS", None)
    env.pop("ALFRED_BRIDGE_REPOS", None)
    env["HOME"] = str(alfred_home.parent)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_agent_launch_loads_token_from_env_file(tmp_path: Path, alfred_home: Path) -> None:
    """The canonical store: a token in $ALFRED_HOME/.env (dotenv KEY=value,
    no `export`) must be exported into the agent's environment. This is the
    fix for the silent-401 outage where the loader and the token tool
    pointed at different files."""
    (alfred_home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    target = tmp_path / "echo-token.sh"
    target.write_text('#!/usr/bin/env bash\necho "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "TOKEN=sk-ant-oat01-fromdotenv" in proc.stdout


def test_agent_launch_env_file_does_not_clobber_real_env(tmp_path: Path, alfred_home: Path) -> None:
    """A value already in the scheduler/process environment wins over the
    same key in .env (.env is a gap-filler, not an override)."""
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


def test_agent_launch_alfredrc_wins_over_env_file(tmp_path: Path, alfred_home: Path) -> None:
    """.alfredrc is loaded first; its value must survive the later .env load
    because .env is no-clobber. Keeps the legacy rc working during migration."""
    (alfred_home.parent / ".alfredrc").write_text(
        "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromrc\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    target = tmp_path / "echo-token.sh"
    target.write_text('#!/usr/bin/env bash\necho "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "TOKEN=sk-ant-oat01-fromrc" in proc.stdout


def test_agent_launch_env_file_repo_scope_overrides_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "export ALFRED_SHIPPED_REPOS=org/old\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text("ALFRED_SHIPPED_REPOS=org/new\n", encoding="utf-8")
    target = tmp_path / "echo-repos.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_SHIPPED_REPOS:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/new" in proc.stdout


def test_agent_launch_real_env_repo_scope_wins_over_env_file(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "export ALFRED_SHIPPED_REPOS=org/old\n", encoding="utf-8"
    )
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


def test_agent_launch_env_file_code_memory_settings_override_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "export ALFRED_CODE_MEMORY_REPOS=org/old\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text("ALFRED_CODE_MEMORY_REPOS=org/new\n", encoding="utf-8")
    target = tmp_path / "echo-code-memory.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_CODE_MEMORY_REPOS:-unset}"\n')
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/new" in proc.stdout


def test_agent_launch_real_env_code_memory_setting_wins_over_env_file(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "export ALFRED_CODE_MEMORY_REPOS=org/old\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text("ALFRED_CODE_MEMORY_REPOS=org/new\n", encoding="utf-8")
    target = tmp_path / "echo-code-memory.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_CODE_MEMORY_REPOS:-unset}"\n')
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFRED_CODE_MEMORY_REPOS": "org/process"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/process" in proc.stdout


def test_agent_launch_ignores_stale_rc_code_memory_scope_for_custom_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    other_runtime = tmp_path / "other-runtime"
    home.mkdir()
    runtime.mkdir()
    other_runtime.mkdir()
    (home / ".alfredrc").write_text(
        f"ALFRED_HOME={other_runtime}\nALFRED_CODE_MEMORY_REPOS=org/stale\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-code-memory.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_CODE_MEMORY_REPOS:-unset}"\n')
    _make_executable(target)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = str(runtime)
    env.pop("ALFRED_CODE_MEMORY_REPOS", None)

    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=unset" in proc.stdout


def test_agent_launch_expands_tilde_alfred_home_before_env_file(tmp_path: Path) -> None:
    """ALFRED_HOME=~/runtime must load the runtime .env, matching setup/status."""

    home = tmp_path / "home"
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (home / ".alfredrc").write_text("ALFRED_HOME=~/runtime\n", encoding="utf-8")
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/expanded\n", encoding="utf-8")
    target = tmp_path / "echo-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME=${ALFRED_HOME:-unset}"\n'
        'echo "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("ALFRED_HOME", None)
    env.pop("ALFRED_PYTHON", None)
    env.pop("ALFRED_QUEUE_REPOS", None)
    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME={runtime}" in proc.stdout
    assert "REPOS=org/expanded" in proc.stdout


def test_agent_launch_env_file_stop_controls_override_stale_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=fales\n", encoding="utf-8"
    )
    target = tmp_path / "echo-memory-stop.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
        'echo "KILL=${ALFRED_AUTO_PROMOTE_KILL:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=fales" in proc.stdout


def test_agent_launch_env_file_stop_controls_override_commented_stale_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE=1 # old enable\nALFRED_AUTO_PROMOTE_KILL=0 # disabled\n",
        encoding="utf-8",
    )
    (alfred_home / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n", encoding="utf-8"
    )
    target = tmp_path / "echo-memory-stop.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
        'echo "KILL=${ALFRED_AUTO_PROMOTE_KILL:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=1" in proc.stdout


def test_agent_launch_env_file_stop_controls_override_stale_process_env(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n", encoding="utf-8"
    )
    target = tmp_path / "echo-memory-stop.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
        'echo "KILL=${ALFRED_AUTO_PROMOTE_KILL:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFRED_AUTO_PROMOTE": "1", "ALFRED_AUTO_PROMOTE_KILL": "0"},
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=1" in proc.stdout


def test_agent_launch_env_file_judge_stop_control_overrides_stale_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE_LLM_JUDGE=1\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text("ALFRED_AUTO_PROMOTE_LLM_JUDGE=treu\n", encoding="utf-8")
    target = tmp_path / "echo-memory-judge.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "JUDGE=${ALFRED_AUTO_PROMOTE_LLM_JUDGE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "JUDGE=treu" in proc.stdout


def test_agent_launch_preserves_existing_judge_stop_control_over_env_enable(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE_LLM_JUDGE=0\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text("ALFRED_AUTO_PROMOTE_LLM_JUDGE=1\n", encoding="utf-8")
    target = tmp_path / "echo-memory-judge.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "JUDGE=${ALFRED_AUTO_PROMOTE_LLM_JUDGE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "JUDGE=0" in proc.stdout


def test_agent_launch_preserves_existing_memory_stop_control_over_env_enable(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE=0\nALFRED_AUTO_PROMOTE_KILL=1\n", encoding="utf-8"
    )
    (alfred_home / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\n", encoding="utf-8"
    )
    target = tmp_path / "echo-memory-stop.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n'
        'echo "KILL=${ALFRED_AUTO_PROMOTE_KILL:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=1" in proc.stdout


def test_agent_launch_preserves_process_stop_controls_over_alfredrc_enable(
    tmp_path: Path, alfred_home: Path
) -> None:
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFRED_AUTO_PROMOTE=1\nALFRED_AUTO_PROMOTE_KILL=0\nALFRED_AUTO_PROMOTE_LLM_JUDGE=1\n",
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
            "ALFRED_AUTO_PROMOTE": "0",
            "ALFRED_AUTO_PROMOTE_KILL": "1",
            "ALFRED_AUTO_PROMOTE_LLM_JUDGE": "0",
        },
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout
    assert "KILL=1" in proc.stdout
    assert "JUDGE=0" in proc.stdout


def test_agent_launch_preserves_process_layout_over_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    rc_home = tmp_path / "rc-runtime"
    (alfred_home.parent / ".alfredrc").write_text(
        f"ALFRED_HOME={rc_home}\nWORKSPACE_ROOT={tmp_path / 'rc-workspace'}\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-layout.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "WORKSPACE=${WORKSPACE_ROOT:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    workspace = tmp_path / "process-workspace"
    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"WORKSPACE_ROOT": str(workspace)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={alfred_home}" in proc.stdout
    assert f"WORKSPACE={workspace}" in proc.stdout


def test_agent_launch_honors_custom_alfredrc_path(tmp_path: Path, alfred_home: Path) -> None:
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    target = tmp_path / "echo-auto-promote.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFREDRC": str(custom_rc)},
    )

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout


def test_agent_launch_follows_pointer_from_explicit_alfredrc(
    tmp_path: Path, alfred_home: Path
) -> None:
    launch_rc = tmp_path / "launch.alfredrc"
    custom_rc = tmp_path / "custom.alfredrc"
    launch_rc.write_text(
        f"ALFREDRC={custom_rc}\nALFRED_AUTO_PROMOTE=1\n",
        encoding="utf-8",
    )
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    target = tmp_path / "echo-auto-promote.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "RC=${ALFREDRC:-unset}"\n'
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFREDRC": str(launch_rc)},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"RC={custom_rc}" in proc.stdout
    assert "AUTO=0" in proc.stdout


def test_agent_launch_followed_alfredrc_retargets_runtime_env(
    tmp_path: Path,
) -> None:
    stale_home = tmp_path / "stale-runtime"
    runtime_home = tmp_path / "runtime"
    stale_home.mkdir()
    runtime_home.mkdir()
    launch_rc = tmp_path / "launch.alfredrc"
    custom_rc = tmp_path / "custom.alfredrc"
    launch_rc.write_text(
        f"ALFREDRC={custom_rc}\nALFRED_HOME={stale_home}\nALFRED_AUTO_PROMOTE=1\n",
        encoding="utf-8",
    )
    custom_rc.write_text(f"ALFRED_HOME={runtime_home}\n", encoding="utf-8")
    (stale_home / ".env").write_text("ALFRED_AUTO_PROMOTE=1\n", encoding="utf-8")
    (runtime_home / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    target = tmp_path / "echo-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=stale_home,
        extra_env={"ALFREDRC": str(launch_rc), "ALFRED_AUTO_PROMOTE": "1"},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={runtime_home}" in proc.stdout
    assert "AUTO=0" in proc.stdout


def test_agent_launch_persisted_pointer_preserves_process_runtime(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    process_home = home / ".alfred"
    pointed_home = tmp_path / "pointed-runtime"
    custom_rc = tmp_path / "custom.alfredrc"
    home.mkdir()
    process_home.mkdir()
    pointed_home.mkdir()
    (home / ".alfredrc").write_text(f"ALFREDRC={custom_rc}\n", encoding="utf-8")
    custom_rc.write_text(f"ALFRED_HOME={pointed_home}\n", encoding="utf-8")
    (process_home / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    (pointed_home / ".env").write_text("ALFRED_AUTO_PROMOTE=1\n", encoding="utf-8")
    target = tmp_path / "echo-persisted-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=process_home)

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={process_home}" in proc.stdout
    assert "AUTO=0" in proc.stdout


def test_agent_launch_direct_alfredrc_retargets_stale_process_runtime(
    tmp_path: Path,
) -> None:
    stale_home = tmp_path / "stale-runtime"
    runtime_home = tmp_path / "runtime"
    stale_home.mkdir()
    runtime_home.mkdir()
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text(f"ALFRED_HOME={runtime_home}\n", encoding="utf-8")
    (stale_home / ".env").write_text("ALFRED_AUTO_PROMOTE=1\n", encoding="utf-8")
    (runtime_home / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    target = tmp_path / "echo-direct-runtime.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME_VAR=${ALFRED_HOME:-unset}"\n'
        'echo "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=stale_home,
        extra_env={"ALFREDRC": str(custom_rc), "ALFRED_AUTO_PROMOTE": "1"},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME_VAR={runtime_home}" in proc.stdout
    assert "AUTO=0" in proc.stdout


def test_agent_launch_follows_persisted_alfredrc_pointer(tmp_path: Path, alfred_home: Path) -> None:
    custom_rc = tmp_path / "custom.alfredrc"
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    (alfred_home.parent / ".alfredrc").write_text(
        f"ALFREDRC={custom_rc} # scheduler rc\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-auto-promote.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout


def test_agent_launch_expands_home_relative_persisted_alfredrc_pointer(
    tmp_path: Path, alfred_home: Path
) -> None:
    custom_rc = alfred_home.parent / "custom.alfredrc"
    custom_rc.write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    (alfred_home.parent / ".alfredrc").write_text(
        "ALFREDRC=~/custom.alfredrc\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-auto-promote.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "AUTO=${ALFRED_AUTO_PROMOTE:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(target, alfred_home=alfred_home)

    assert proc.returncode == 0, proc.stderr
    assert "AUTO=0" in proc.stdout


def test_agent_launch_expands_user_relative_alfredrc_path(
    tmp_path: Path, alfred_home: Path
) -> None:
    user_info = pwd.getpwuid(os.getuid())
    target = tmp_path / "echo-rc.sh"
    target.write_text(
        '#!/usr/bin/env bash\necho "RC=${ALFREDRC:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    proc = _run_env(
        target,
        alfred_home=alfred_home,
        extra_env={"ALFREDRC": f"~{user_info.pw_name}/custom.alfredrc"},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"RC={Path(user_info.pw_dir) / 'custom.alfredrc'}" in proc.stdout


def test_agent_launch_empty_alfred_home_uses_default_home_for_rc_scope(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default_runtime = home / ".alfred"
    home.mkdir()
    default_runtime.mkdir()
    (home / ".alfredrc").write_text("ALFRED_QUEUE_REPOS=org/from-rc\n", encoding="utf-8")
    target = tmp_path / "echo-repos.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n')
    _make_executable(target)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = ""
    env.pop("ALFRED_QUEUE_REPOS", None)

    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/from-rc" in proc.stdout


def test_agent_launch_empty_alfred_home_loads_rc_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (home / ".alfredrc").write_text(f"ALFRED_HOME={runtime}\n", encoding="utf-8")
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/runtime\n", encoding="utf-8")
    target = tmp_path / "echo-home.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "HOME=${ALFRED_HOME:-unset}"\n'
        'echo "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = ""
    env.pop("ALFRED_QUEUE_REPOS", None)

    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"HOME={runtime}" in proc.stdout
    assert "REPOS=org/runtime" in proc.stdout


def test_agent_launch_loads_non_repo_rc_for_custom_home_but_skips_stale_repo_scope(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (home / ".alfredrc").write_text(
        "\n".join(
            [
                "WORKSPACE_ROOT=$HOME/code space",
                "CLAUDE_CODE_OAUTH_TOKEN=from-rc",
                "ALFRED_QUEUE_REPOS=org/from-rc",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (runtime / ".env").write_text("ALFRED_QUEUE_REPOS=org/from-env\n", encoding="utf-8")
    target = tmp_path / "echo-env.sh"
    target.write_text(
        "#!/usr/bin/env bash\n"
        'echo "WORKSPACE=${WORKSPACE_ROOT:-unset}"\n'
        'echo "TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n'
        'echo "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n',
        encoding="utf-8",
    )
    _make_executable(target)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = str(runtime)
    env.pop("WORKSPACE_ROOT", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("ALFRED_QUEUE_REPOS", None)

    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"WORKSPACE={home}/code space" in proc.stdout
    assert "TOKEN=from-rc" in proc.stdout
    assert "REPOS=org/from-env" in proc.stdout


def test_agent_launch_normalizes_custom_home_before_rejecting_rc_repo_scope(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (home / ".alfredrc").write_text(
        f"ALFRED_HOME={runtime}\nALFRED_QUEUE_REPOS=org/from-rc\n",
        encoding="utf-8",
    )
    target = tmp_path / "echo-repos.sh"
    target.write_text('#!/usr/bin/env bash\necho "REPOS=${ALFRED_QUEUE_REPOS:-unset}"\n')
    _make_executable(target)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["ALFRED_HOME"] = f"{runtime}/"
    env.pop("ALFRED_QUEUE_REPOS", None)

    proc = subprocess.run(
        ["bash", str(AGENT_LAUNCH), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "REPOS=org/from-rc" in proc.stdout


def test_bash_available() -> None:
    """Sanity: tests need bash on PATH."""
    assert shutil.which("bash"), "bash not on PATH; cannot run agent-launch tests"
