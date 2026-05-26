"""Focused tests for ``lib.agent_runner.process``."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace


def test_run_returns_124_on_timeout(fresh_agent_runner):
    """run() catches TimeoutExpired and surfaces a returncode of 124."""
    ar = fresh_agent_runner

    res = ar.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
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


def test_claude_invoke_streaming_delegates_to_claude_invoke(fresh_agent_runner, monkeypatch):
    """After v0.4.1 the streaming wrapper no longer routes through a
    proxy daemon; it should forward straight to ``claude_invoke`` with
    the historical kwargs preserved so existing agent callers keep
    working unchanged. Also verifies the wrapper does not read
    ``ALFRED_CLAUDE_PROXY_SOCKET`` any more.
    """
    ar = fresh_agent_runner
    from pathlib import Path

    monkeypatch.setenv("ALFRED_CLAUDE_PROXY_SOCKET", "/tmp/socket-that-should-be-ignored")

    captured: dict[str, object] = {}

    def fake_claude_invoke(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return ar.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="s-1",
            result_text="ok",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    monkeypatch.setattr(ar, "claude_invoke", fake_claude_invoke)

    res = ar.claude_invoke_streaming(
        prompt="hello",
        workdir=Path("/tmp"),
        allowed_tools="Read,Bash",
        agent="testagent",
        firing_id="20260524-123456-aaaa",
        timeout=42,
        max_turns=None,
        resume_session=None,
        model=None,
    )

    assert res.success
    # The wrapper should have called claude_invoke once with the same
    # transport-relevant kwargs. ``agent`` / ``firing_id`` are part of
    # the public signature but unused at the wrapper level today, so
    # they should NOT be forwarded as kwargs to ``claude_invoke`` (which
    # does not accept them).
    assert captured["prompt"] == "hello"
    assert captured["allowed_tools"] == "Read,Bash"
    assert captured["timeout"] == 42
    assert "agent" not in captured
    assert "firing_id" not in captured


def test_claude_invoke_timeout_returns_error_timeout(fresh_agent_runner, monkeypatch):
    ar = fresh_agent_runner
    from pathlib import Path

    import agent_runner.process as proc

    monkeypatch.setattr(
        proc,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            a[0],
            124,
            stdout="partial output",
            stderr="TIMEOUT after 5s",
        ),
    )

    out = ar.claude_invoke(
        prompt="hi",
        workdir=Path("/tmp"),
        allowed_tools="Read",
        timeout=5,
    )

    assert out.success is False
    assert out.subtype == "error_timeout"
    assert out.stop_reason == "aborted"
    assert "5s" in (out.error_message or "")
