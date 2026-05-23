"""Focused tests for ``lib.agent_runner.orchestrator``."""

from __future__ import annotations

import pytest


def test_preflight_passes_when_env_and_bins_exist(fresh_agent_runner, monkeypatch):
    """preflight returns silently when env vars + bins are present."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_HOME", "/tmp/alfred")
    monkeypatch.setenv("WORKSPACE_ROOT", "/tmp/workspace")
    monkeypatch.setattr(
        ar.orchestrator, "shutil", _shutil_always_present(), raising=False
    )

    # Use the real shutil.which by patching what preflight imports lazily.
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/" + name)

    spec = ar.PreflightSpec(agent="test", bins=["claude"])
    ar.preflight(spec)  # raises on miss


def test_preflight_reports_missing_env(fresh_agent_runner, monkeypatch):
    """preflight raises PreflightFailed when a required env var is unset."""
    ar = fresh_agent_runner
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    spec = ar.PreflightSpec(agent="test", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)


def test_get_tier_from_labels_defaults_to_sonnet(fresh_agent_runner):
    """No llm-tier label means sonnet; first matching label wins."""
    ar = fresh_agent_runner
    assert ar.get_tier_from_labels([]) == "sonnet"
    assert ar.get_tier_from_labels([{"name": "llm-tier:opus"}]) == "opus"
    assert (
        ar.get_tier_from_labels(
            [{"name": "other"}, {"name": "llm-tier:haiku"}]
        )
        == "haiku"
    )


def test_route_llm_unknown_tier_falls_back(fresh_agent_runner, monkeypatch):
    """Unknown tiers route to sonnet via claude_invoke."""
    ar = fresh_agent_runner
    captured = {}

    def fake_claude(prompt, *, model=None, **kw):
        captured["model"] = model
        captured["prompt"] = prompt
        return ar.dry_run_claude_result(prompt)

    monkeypatch.setattr(ar, "claude_invoke", fake_claude)
    ar.route_llm("nonsense", "hi")
    # Unknown tier resolves to TIER_TO_MODEL.get(..., TIER_TO_MODEL["sonnet"])
    # which is the literal string "sonnet" in our map.
    assert captured["model"] == "sonnet"


class _shutil_always_present:
    """Pretend every binary is on PATH; used for the happy-path preflight test."""

    @staticmethod
    def which(name: str) -> str:
        return "/usr/bin/" + name
