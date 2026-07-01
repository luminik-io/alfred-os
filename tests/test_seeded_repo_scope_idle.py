"""Seeded repo-scoped runners stay quiet until onboarding saves repos."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
LIB = ROOT / "lib"


def load_runner(script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "acme")
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    spec = importlib.util.spec_from_file_location(script.replace("-", "_"), BIN / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(spec.name, None)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("script", "env_key"),
    [
        ("lucius.py", "ALFRED_LUCIUS_REPOS"),
        ("drake.py", "ALFRED_DRAKE_REPOS"),
        ("bane.py", "ALFRED_BANE_REPOS"),
        ("automerge.py", "ALFRED_AUTOMERGE_REPOS"),
        ("rasalghul.py", "ALFRED_RASALGHUL_REPOS"),
        ("nightwing.py", "ALFRED_NIGHTWING_REPOS"),
        ("robin.py", "ALFRED_ROBIN_REPOS"),
        ("code-map-refresh.py", "ALFRED_CODE_MAP_REPOS"),
    ],
)
def test_seeded_repo_scoped_runners_idle_before_preflight(
    script: str,
    env_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv(env_key, raising=False)
    if script == "code-map-refresh.py":
        monkeypatch.delenv("ALFRED_CODE_MAP_BACKEND_REPO", raising=False)
        monkeypatch.delenv("ALFRED_CODE_MAP_SIDECAR_REPO", raising=False)
    runner = load_runner(script, tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "with_lock", lambda _agent: None)
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "doctor_requested", lambda: False)
    if hasattr(runner, "is_dry_run"):
        monkeypatch.setattr(runner, "is_dry_run", lambda: False)

    def fail_preflight(_spec):
        raise AssertionError("preflight should not run without repo scope")

    monkeypatch.setattr(runner, "preflight", fail_preflight)

    assert runner.main() == 0
    assert "no repos configured" in capsys.readouterr().out
