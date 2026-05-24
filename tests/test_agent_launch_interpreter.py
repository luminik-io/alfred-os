"""Regression tests for bin/agent-launch interpreter resolution.

Pinned behaviour: only Python targets (`.py` or shebang containing
"python") may be re-execed through the venv interpreter. Shell scripts
must always run via their own shebang so the venv-python path never
tries to parse `.sh` files as Python source (regression from PR #102).
"""

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


def _run(
    target: Path, *, alfred_home: Path, alfred_python: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(alfred_home)
    env.pop("ALFRED_PYTHON", None)
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


def test_bash_available() -> None:
    """Sanity: tests need bash on PATH."""
    assert shutil.which("bash"), "bash not on PATH; cannot run agent-launch tests"
