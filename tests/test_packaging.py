"""Packaging contract tests."""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def test_wheel_maps_lib_modules_to_top_level_imports():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["only-include"] == ["lib"]
    assert wheel["sources"] == ["lib"]


def test_wheel_smoke_import_and_console_script(tmp_path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))

    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
        assert "agent_runner.py" in names
        assert "alfred_os_cli.py" in names
        entry_points = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        assert "alfred-os = alfred_os_cli:main" in zf.read(entry_points).decode()


def test_operator_cli_exposes_claude_wrapper():
    result = subprocess.run(
        [sys.executable, "bin/alfred", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "claude" in result.stdout
    assert "manage Claude Code account routing" in result.stdout


def test_operator_cli_owns_claude_probe():
    result = subprocess.run(
        [sys.executable, "bin/alfred", "claude", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "status,primary,secondary,swap,probe" in result.stdout
    removed_helper = "hermes" + "-claude"
    assert not Path("bin", removed_helper).exists()
