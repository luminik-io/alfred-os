from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("bin/alfred", "bin/alfred-serve.py")


def _script_path_order(
    script_path: Path,
    runtime_home: Path,
    *,
    alfred_home: bool = True,
    seed_duplicates: bool = False,
) -> list[str]:
    repo_lib = str(script_path.resolve().parents[1] / "lib")
    runtime_lib = str(runtime_home / "lib")
    env = os.environ.copy()
    if alfred_home:
        env["ALFRED_HOME"] = str(runtime_home)
    else:
        env.pop("ALFRED_HOME", None)
    code = f"""
import json
import runpy
import sys

repo_lib = {repo_lib!r}
runtime_lib = {runtime_lib!r}
sys.path = [entry for entry in sys.path if entry not in {{repo_lib, runtime_lib}}]
if {seed_duplicates!r}:
    sys.path[:0] = [repo_lib, runtime_lib, repo_lib, runtime_lib]
runpy.run_path({str(script_path)!r})
print(json.dumps([entry for entry in sys.path if entry in {{repo_lib, runtime_lib}}]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        env=env,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("script", SCRIPTS)
def test_source_scripts_prefer_checkout_lib_over_deployed_lib(
    tmp_path: Path,
    script: str,
) -> None:
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    script_path = ROOT / script

    assert _script_path_order(script_path, runtime_lib.parent) == [
        str(ROOT / "lib"),
        str(runtime_lib),
    ]


@pytest.mark.parametrize("script", SCRIPTS)
def test_source_scripts_keep_checkout_lib_when_alfred_home_is_unset(
    tmp_path: Path,
    script: str,
) -> None:
    script_path = ROOT / script

    assert _script_path_order(script_path, tmp_path / "missing-runtime", alfred_home=False) == [
        str(ROOT / "lib"),
    ]


@pytest.mark.parametrize("script", SCRIPTS)
def test_source_scripts_keep_deployed_lib_when_checkout_lib_is_absent(
    tmp_path: Path,
    script: str,
) -> None:
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    copied_script = checkout / script
    copied_script.parent.mkdir(parents=True)
    copied_script.write_text((ROOT / script).read_text(encoding="utf-8"), encoding="utf-8")

    assert _script_path_order(copied_script, runtime_lib.parent) == [str(runtime_lib)]


@pytest.mark.parametrize("script", SCRIPTS)
def test_source_scripts_remove_duplicate_lib_entries(
    tmp_path: Path,
    script: str,
) -> None:
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    script_path = ROOT / script

    assert _script_path_order(
        script_path,
        runtime_lib.parent,
        seed_duplicates=True,
    ) == [
        str(ROOT / "lib"),
        str(runtime_lib),
    ]
