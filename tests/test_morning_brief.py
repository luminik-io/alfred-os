from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "agent-morning-brief.py"


def load_module(monkeypatch, tmp_path):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("GH_ORG", "acme")
    for name in list(sys.modules):
        if name == "agent_runner" or name.startswith("agent_runner"):
            del sys.modules[name]
    sys.path.insert(0, str(ROOT / "lib"))
    try:
        spec = importlib.util.spec_from_file_location("agent_morning_brief_test", BIN)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_main_idles_before_preflight_when_no_scope(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ALFRED_MORNING_BRIEF_AGENTS", raising=False)
    monkeypatch.delenv("ALFRED_MORNING_BRIEF_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    mod = load_module(monkeypatch, tmp_path)

    def fail_preflight(_spec):
        raise AssertionError("preflight should not run without repo scope")

    monkeypatch.setattr(mod, "preflight", fail_preflight)

    assert mod.main() == 0
    assert "no agents/repos configured" in capsys.readouterr().out
