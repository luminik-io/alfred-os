"""Shared fixtures for the focused unit tests under ``tests/unit/agent_runner/``.

The package was carved out of a monolith and many constants are
captured at import time from ``ALFRED_HOME`` / ``WORKSPACE_ROOT``. We
re-import a fresh ``agent_runner`` under a tmp ``ALFRED_HOME`` for
every test so on-disk state never leaks between cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[3] / "lib"


@pytest.fixture()
def fresh_agent_runner(tmp_path, monkeypatch):
    """Return a freshly-imported ``agent_runner`` rooted at a tmp ALFRED_HOME."""
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    # Force a clean import so module-level constants pick up the env vars.
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    sys.path.insert(0, str(_LIB))
    import agent_runner

    return agent_runner
