"""Unit tests for the claude-proxy NDJSON wire protocol."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.claude_proxy.protocol import (  # noqa: E402
    HealthOk,
    HealthRequest,
    InvokeRequest,
    ProbeFail,
    ProbeOk,
    ProbeRequest,
    ProtocolError,
    ProxyAccepted,
    ProxyError,
    ProxyTerminal,
    iter_lines,
    parse_line,
    parse_request,
    parse_response,
    serialize,
)


@pytest.mark.parametrize(
    "obj",
    [
        InvokeRequest(
            prompt="hello",
            workdir="/tmp",
            allowed_tools="Read,Edit",
            session_id="abc123",
        ),
        HealthRequest(),
        ProbeRequest(),
        ProxyAccepted(claude_bin="/usr/local/bin/claude", pid=42),
        ProxyTerminal(exit_code=0, duration_ms=1500),
        ProxyError(reason="bad-workdir", detail="no such dir"),
        HealthOk(claude_bin="/usr/local/bin/claude", uptime_seconds=10, pid=1234),
        ProbeOk(duration_ms=900),
        ProbeFail(reason="claude-exit-1", stderr_tail="auth failed"),
    ],
)
def test_serialize_round_trip(obj: object) -> None:
    """Every dataclass should survive a serialize -> parse round trip."""
    line = serialize(obj)
    assert line.endswith(b"\n")
    parsed = parse_line(line)
    assert parsed == obj


def test_serialize_emits_compact_json() -> None:
    """Wire format should not waste bytes on whitespace."""
    line = serialize(HealthRequest())
    assert b" " not in line.rstrip(b"\n")


def test_parse_request_rejects_unknown_type() -> None:
    line = json.dumps({"type": "garbage"})
    with pytest.raises(ProtocolError):
        parse_request(line)


def test_parse_request_rejects_invalid_json() -> None:
    with pytest.raises(ProtocolError):
        parse_request(b"{not json")


def test_parse_request_rejects_non_object() -> None:
    with pytest.raises(ProtocolError):
        parse_request(b"[]")


def test_parse_request_rejects_empty_line() -> None:
    with pytest.raises(ProtocolError):
        parse_request(b"   \n")


def test_parse_response_passes_through_unknown_types() -> None:
    """Upstream claude stream-JSON events have types we don't model."""
    raw = json.dumps({"type": "system", "subtype": "init", "session_id": "s"})
    parsed = parse_response(raw)
    assert isinstance(parsed, dict)
    assert parsed["type"] == "system"
    assert parsed["session_id"] == "s"


def test_parse_response_typed_for_proxy_events() -> None:
    raw = json.dumps({"type": "proxy.accepted", "claude_bin": "/x", "pid": 7})
    parsed = parse_response(raw)
    assert isinstance(parsed, ProxyAccepted)
    assert parsed.pid == 7


def test_parse_drops_unknown_fields_for_forward_compat() -> None:
    """A newer server can add fields without breaking older clients."""
    raw = json.dumps(
        {
            "type": "proxy.terminal",
            "exit_code": 0,
            "duration_ms": 1,
            "future_field": "ignored",
        }
    )
    parsed = parse_response(raw)
    assert isinstance(parsed, ProxyTerminal)
    assert parsed.exit_code == 0


def test_iter_lines_splits_on_newline() -> None:
    buf = b'{"type":"health"}\n{"type":"probe"}\npartial'
    complete, rest = iter_lines(buf)
    assert complete == [b'{"type":"health"}', b'{"type":"probe"}']
    assert rest == b"partial"


def test_iter_lines_returns_no_lines_when_buffer_empty() -> None:
    complete, rest = iter_lines(b"")
    assert complete == []
    assert rest == b""


def test_iter_lines_returns_no_lines_when_no_newline() -> None:
    complete, rest = iter_lines(b'{"type":"health"}')
    assert complete == []
    assert rest == b'{"type":"health"}'


def test_invoke_request_default_claude_args() -> None:
    """The default should match the direct-subprocess invocation's flags."""
    req = InvokeRequest(prompt="p", workdir="/tmp", allowed_tools="Read", session_id="s")
    assert "--permission-mode" in req.claude_args
    assert "bypassPermissions" in req.claude_args


def test_invoke_request_preserves_extra_args() -> None:
    req = InvokeRequest(
        prompt="p",
        workdir="/tmp",
        allowed_tools="Read",
        session_id="s",
        claude_args=["--debug"],
    )
    parsed = parse_request(serialize(req))
    assert isinstance(parsed, InvokeRequest)
    assert parsed.claude_args == ["--debug"]


def test_serialize_rejects_non_dataclass() -> None:
    with pytest.raises(TypeError):
        serialize(42)
