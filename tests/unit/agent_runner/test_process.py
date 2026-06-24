"""Focused tests for ``lib.agent_runner.process``."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
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


def test_claude_invoke_streaming_writes_transcript(fresh_agent_runner, monkeypatch):
    """Streaming Claude writes stream-json lines and parses the final result."""
    ar = fresh_agent_runner
    from pathlib import Path

    import agent_runner.process as proc

    monkeypatch.setenv("ALFRED_CLAUDE_PROXY_SOCKET", "/tmp/socket-that-should-be-ignored")

    captured: dict[str, object] = {}
    assistant = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    }
    final = {
        "type": "result",
        "subtype": "success",
        "result": "ok",
        "num_turns": 1,
        "total_cost_usd": 0.01,
        "session_id": "s-1",
        "stop_reason": "end_turn",
    }
    stream = json.dumps(assistant) + "\n" + json.dumps(final) + "\n"

    class FakeProc:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = io.StringIO(stream)
            self.stderr = io.StringIO("")

        def wait(self, timeout: int) -> int:
            captured["timeout"] = timeout
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(proc.subprocess, "Popen", fake_popen)

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
    assert res.result_text == "ok"
    cmd = captured["cmd"]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--allowedTools" in cmd
    assert cmd[cmd.index("--allowedTools") + 1].startswith("Read,Bash")
    assert captured["timeout"] == 42
    assert captured["kwargs"]["cwd"] == "/tmp"
    transcript = ar.transcript_path("testagent", "20260524-123456-aaaa")
    assert transcript.read_text(encoding="utf-8") == stream


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


# --------------------------------------------------------------------------
# Reliability wiring in process.py
# --------------------------------------------------------------------------


def test_stream_step_for_loopcheck_tool_use(fresh_agent_runner):
    ar = fresh_agent_runner
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
            },
        }
    )
    step = ar.process._stream_step_for_loopcheck(line)
    assert step is not None
    action, preview = step
    assert action == "Bash"
    assert "ls" in preview


def test_stream_step_for_loopcheck_tool_result(fresh_agent_runner):
    ar = fresh_agent_runner
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": [{"type": "text", "text": "boom"}]}]
            },
        }
    )
    step = ar.process._stream_step_for_loopcheck(line)
    assert step == ("tool_result", "boom")


def test_stream_step_for_loopcheck_ignores_non_tool_lines(fresh_agent_runner):
    ar = fresh_agent_runner
    assert ar.process._stream_step_for_loopcheck('{"type":"system","subtype":"init"}') is None
    assert ar.process._stream_step_for_loopcheck("not json") is None
    assert ar.process._stream_step_for_loopcheck("") is None


def test_dispatch_short_circuits_when_breaker_open(fresh_agent_runner, monkeypatch):
    """When the engine breaker is already open, dispatch returns a
    breaker-open result without invoking the engine."""
    ar = fresh_agent_runner

    # Trip the claude breaker first.
    cb = ar.CircuitBreaker("claude", threshold=1, cooldown_seconds=600)
    cb.record_transient_failure()
    assert cb.is_open() is True

    # Make the per-instance breaker inside dispatch use the same low settings
    # by setting the env defaults (threshold 1, long cooldown).
    monkeypatch.setenv("ALFRED_BREAKER_THRESHOLD", "1")
    monkeypatch.setenv("ALFRED_BREAKER_COOLDOWN_SECONDS", "600")

    def fake_claude(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("claude must not be called while breaker is open")

    def fake_codex(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("codex must not be called")

    out, engine_used = ar.invoke_agent_engine(
        "hi",
        engine="claude",
        agent="batman",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        claude_fn=fake_claude,
        codex_fn=fake_codex,
    )
    assert out.success is False
    assert out.raw.get("breaker_open") is True
    assert engine_used == "claude"
