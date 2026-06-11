"""Tests for the ``alfred assign`` wrapper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_assign_wrapper_loads_checkout_lib_when_runtime_lib_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "runtime"))
    checkout_lib = str(REPO / "lib")
    original_sys_path = list(sys.path)
    original_modules = {
        module: sys.modules.get(module)
        for module in ("alfred_assign", "issue_assignment", "issue_queue")
    }
    for module in ("alfred_assign", "issue_assignment", "issue_queue"):
        sys.modules.pop(module, None)
    try:
        sys.path[:] = [path for path in sys.path if path != checkout_lib]
        spec = importlib.util.spec_from_file_location(
            "alfred_assign",
            REPO / "bin" / "alfred-assign.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["alfred_assign"] = module
        spec.loader.exec_module(module)

        assert checkout_lib in sys.path[:2]
        assert module.parse_issue_ref("acme/widgets#42") == ("acme/widgets", 42)
    finally:
        sys.path[:] = original_sys_path
        for name, module in original_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
