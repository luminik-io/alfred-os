"""Focused tests for ``lib.agent_runner.process``."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace


def test_run_returns_124_on_timeout(fresh_agent_runner, monkeypatch):
    """run() catches TimeoutExpired and surfaces a returncode of 124."""
    ar = fresh_agent_runner

    def fake(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="partial")

    monkeypatch.setattr(ar.subprocess, "run", fake)
    res = ar.run(["sleep", "5"], timeout=1)
    assert res.returncode == 124
    assert "TIMEOUT" in res.stderr


def test_gh_json_returns_default_on_nonzero(fresh_agent_runner, monkeypatch):
    """gh_json swallows failures and returns the caller's default."""
    ar = fresh_agent_runner
    monkeypatch.setattr(
        ar,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    assert ar.gh_json(["gh", "x"], default=[]) == []
    assert ar.gh_json(["gh", "x"], default={"a": 1}) == {"a": 1}


def test_gh_json_returns_default_on_unparseable(fresh_agent_runner, monkeypatch):
    """gh_json returns default when stdout isn't JSON."""
    ar = fresh_agent_runner
    monkeypatch.setattr(
        ar,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    assert ar.gh_json(["gh", "x"], default=None) is None


def test_short_trims_long_text(fresh_agent_runner):
    """short() leaves short text alone and adds an ellipsis to long text."""
    ar = fresh_agent_runner
    assert ar.short("hello") == "hello"
    long = "x" * 500
    out = ar.short(long, n=100)
    assert len(out) == 103  # 100 chars + "..."
    assert out.endswith("...")


def test_claude_invoke_dry_run_short_circuits(fresh_agent_runner, monkeypatch):
    """In dry-run mode claude_invoke returns a synthetic result without subprocess."""
    ar = fresh_agent_runner
    ar.set_dry_run(True)
    try:
        called = []
        monkeypatch.setattr(
            ar.subprocess,
            "run",
            lambda *a, **kw: called.append(a) or SimpleNamespace(returncode=0),
        )
        from pathlib import Path

        res = ar.claude_invoke(
            prompt="hi",
            workdir=Path("/tmp"),
            allowed_tools="Read",
        )
        assert called == []
        assert res.success
        assert res.subtype == "success"
        assert "synthetic claude result" in res.result_text
    finally:
        ar.set_dry_run(False)
