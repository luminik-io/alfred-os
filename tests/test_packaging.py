"""Packaging contract tests."""

from __future__ import annotations

import subprocess
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
