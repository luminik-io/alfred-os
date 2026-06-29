from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script_path_order(script: str, runtime_home: Path) -> list[str]:
    repo_lib = str(ROOT / "lib")
    runtime_lib = str(runtime_home / "lib")
    env = os.environ.copy()
    env["ALFRED_HOME"] = str(runtime_home)
    code = f"""
import json
import runpy
import sys
from pathlib import Path

repo_lib = {repo_lib!r}
runtime_lib = {runtime_lib!r}
sys.path = [entry for entry in sys.path if entry not in {{repo_lib, runtime_lib}}]
runpy.run_path(str(Path({str(ROOT)!r}) / {script!r}))
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


def test_source_alfred_prefers_checkout_lib_over_deployed_lib(tmp_path: Path) -> None:
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)

    assert _script_path_order("bin/alfred", runtime_lib.parent) == [
        str(ROOT / "lib"),
        str(runtime_lib),
    ]


def test_source_serve_prefers_checkout_lib_over_deployed_lib(tmp_path: Path) -> None:
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)

    assert _script_path_order("bin/alfred-serve.py", runtime_lib.parent) == [
        str(ROOT / "lib"),
        str(runtime_lib),
    ]
