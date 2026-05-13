"""Tests for the ``bin/batman.py`` runner shell.

The heavy bundle and parser primitives live in ``lib/batman.py``. These
tests cover runner-only wiring that should stay offline and deterministic:
configured repo scoping for GitHub issue search.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "bin" / "batman.py"


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod in ("batman", "batman_runner", "slack_format"):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    yield


def _load_runner():
    spec = importlib.util.spec_from_file_location("batman_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["batman_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_list_large_features_returns_no_work_when_scan_repos_unset(monkeypatch):
    runner = _load_runner()
    calls: list[list[str]] = []

    def fake_gh_json(cmd, *, default):
        calls.append(cmd)
        return default

    monkeypatch.delenv("BATMAN_SCAN_REPOS", raising=False)
    monkeypatch.setattr(runner, "gh_json", fake_gh_json)

    assert runner._list_large_features() == []
    assert calls == []


def test_list_large_features_scopes_search_to_configured_repos(monkeypatch):
    runner = _load_runner()
    runner.GH_REPO_TO_LOCAL.update({"myorg-backend": "backend"})
    calls: list[list[str]] = []

    def fake_gh_json(cmd, *, default):
        calls.append(cmd)
        return [
            {
                "number": 1,
                "title": "eligible",
                "url": "https://github.com/myorg/myorg-backend/issues/1",
                "labels": [{"name": "agent:large-feature"}],
                "createdAt": "2026-05-09T10:00:00Z",
                "body": "",
            },
            {
                "number": 2,
                "title": "claimed",
                "url": "https://github.com/myorg/frontend/issues/2",
                "labels": [{"name": "agent:in-flight"}],
                "createdAt": "2026-05-09T10:00:00Z",
                "body": "",
            },
        ]

    monkeypatch.setenv("BATMAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setattr(runner, "gh_json", fake_gh_json)

    rows = runner._list_large_features()

    assert [row["number"] for row in rows] == [1]
    cmd = calls[0]
    assert "--owner" not in cmd
    assert cmd.count("--repo") == 2
    assert "myorg/myorg-backend" in cmd
    assert "myorg/frontend" in cmd
