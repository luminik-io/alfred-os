"""NDJSON wire protocol for alfred-claude-proxy.

The protocol is intentionally minimal: one JSON object per line, no
framing other than ``\\n``, no JSON-RPC ceremony. Each connection carries
exactly one request from the client and a stream of response events from
the server, terminated by a single ``proxy.terminal`` (for ``invoke``) or
a single response object (for ``health`` and ``probe``).

This module owns:

* Dataclasses for every message type the wire carries.
* :func:`serialize` and :func:`parse_line` round-trip helpers.
* :func:`iter_lines` -- a small ``bytes`` -> ``list[bytes]`` chunker the
  server uses to peel complete NDJSON lines off the socket read buffer.

It owns NO networking or subprocess logic; that lives in
:mod:`claude_proxy.server` and :mod:`claude_proxy.client`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# --------------------------------------------------------------------------
# Request shapes (client -> server)
# --------------------------------------------------------------------------


@dataclass
class InvokeRequest:
    """Spawn ``claude -p ...`` and stream its NDJSON output back.

    The proxy uses the same defaults the ``agent_runner.process`` module
    applies when calling ``claude`` directly, so a caller that already had
    a working direct-subprocess invocation can swap in the proxy without
    re-tuning flags.

    Attributes:
        prompt: full ``-p`` argument.
        workdir: working directory the child ``claude`` is spawned in.
        allowed_tools: comma-joined tool gate, forwarded to
            ``--allowedTools``.
        session_id: opaque correlation id the proxy logs and echoes in
            error events; not forwarded to ``claude``.
        claude_args: extra argv tail appended verbatim after the standard
            flags. The default applies ``--permission-mode bypassPermissions``,
            matching the direct-subprocess path.
        timeout_seconds: wall-clock ceiling. ``0`` disables the timeout.
        max_turns: optional explicit ``--max-turns``. ``None`` means the
            proxy lets the upstream caller's hidden default apply.
        model: optional ``--model`` flag forwarded verbatim.
        resume_session: optional ``--resume`` session id forwarded verbatim.
    """

    prompt: str
    workdir: str
    allowed_tools: str
    session_id: str
    claude_args: list[str] = field(
        default_factory=lambda: ["--permission-mode", "bypassPermissions"]
    )
    timeout_seconds: int = 2400
    max_turns: int | None = None
    model: str | None = None
    resume_session: str | None = None
    type: Literal["invoke"] = "invoke"


@dataclass
class HealthRequest:
    """Liveness probe. The server replies immediately with :class:`HealthOk`."""

    type: Literal["health"] = "health"


@dataclass
class ProbeRequest:
    """End-to-end check: spawn a tiny ``claude -p "say ok"`` and report.

    Used by operator tooling to confirm the proxy can actually reach the
    keychain and authenticate, not just that the socket is alive.
    """

    type: Literal["probe"] = "probe"


# --------------------------------------------------------------------------
# Response shapes (server -> client)
# --------------------------------------------------------------------------


@dataclass
class ProxyAccepted:
    """First event on an ``invoke`` connection: the child claude has started."""

    claude_bin: str
    pid: int
    type: Literal["proxy.accepted"] = "proxy.accepted"


@dataclass
class ProxyTerminal:
    """Last event on an ``invoke`` connection: the child exited.

    ``exit_code`` is ``-1`` when the child was killed by the proxy (client
    disconnect, shutdown) and could not be reaped normally.
    """

    exit_code: int
    duration_ms: int
    type: Literal["proxy.terminal"] = "proxy.terminal"


@dataclass
class ProxyError:
    """Emitted in place of ``proxy.terminal`` when the proxy itself failed.

    Examples: the ``claude`` binary could not be exec'd, the workdir does
    not exist, JSON parsing of the request failed. ``claude``'s own
    stream-JSON errors pass through untouched as upstream events.
    """

    reason: str
    detail: str = ""
    type: Literal["proxy.error"] = "proxy.error"


@dataclass
class HealthOk:
    """Response to :class:`HealthRequest`."""

    claude_bin: str
    uptime_seconds: int
    pid: int
    type: Literal["health.ok"] = "health.ok"


@dataclass
class ProbeOk:
    """Successful response to :class:`ProbeRequest`."""

    duration_ms: int
    type: Literal["probe.ok"] = "probe.ok"


@dataclass
class ProbeFail:
    """Failed response to :class:`ProbeRequest`."""

    reason: str
    stderr_tail: str = ""
    type: Literal["probe.fail"] = "probe.fail"


# Union of every request and response type. Used by :func:`parse_line` to
# pick a dataclass; ordering is significant only insofar as the ``type``
# discriminator must be unique across the union (and it is).
_REQUEST_TYPES: dict[str, type] = {
    "invoke": InvokeRequest,
    "health": HealthRequest,
    "probe": ProbeRequest,
}

_RESPONSE_TYPES: dict[str, type] = {
    "proxy.accepted": ProxyAccepted,
    "proxy.terminal": ProxyTerminal,
    "proxy.error": ProxyError,
    "health.ok": HealthOk,
    "probe.ok": ProbeOk,
    "probe.fail": ProbeFail,
}


class ProtocolError(ValueError):
    """Raised when an NDJSON line cannot be mapped to a known message."""


def serialize(obj: Any) -> bytes:
    """Render a dataclass instance to a single NDJSON line (``b"...\\n"``).

    Accepts any dataclass or plain dict. The trailing newline is included
    so callers can ``writer.write(serialize(...))`` without remembering.
    Compact separators keep the wire small for high-volume stream-json
    pass-through.
    """
    if hasattr(obj, "__dataclass_fields__"):
        payload = asdict(obj)
    elif isinstance(obj, dict):
        payload = obj
    else:
        raise TypeError(f"cannot serialize {type(obj).__name__}")
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return line.encode("utf-8") + b"\n"


def parse_request(line: bytes | str) -> InvokeRequest | HealthRequest | ProbeRequest:
    """Parse a client-side NDJSON line into a typed request dataclass."""
    obj = _parse_to_dict(line)
    kind = obj.get("type")
    # _REQUEST_TYPES is keyed by str; obj.get("type") returns Any | None.
    # Guard the type narrowing explicitly so mypy and runtime agree.
    if not isinstance(kind, str):
        raise ProtocolError(f"unknown request type {kind!r}")
    cls = _REQUEST_TYPES.get(kind)
    if cls is None:
        raise ProtocolError(f"unknown request type {kind!r}")
    return _from_dict(cls, obj)


def parse_response(line: bytes | str) -> Any:
    """Parse a server-side NDJSON line.

    Returns a typed proxy response dataclass for the proxy's own control
    events; returns the raw ``dict`` untouched for everything else (so
    upstream ``claude`` stream-JSON events pass through unmodified).
    """
    obj = _parse_to_dict(line)
    kind = obj.get("type", "")
    cls = _RESPONSE_TYPES.get(kind)
    if cls is None:
        return obj
    return _from_dict(cls, obj)


def parse_line(line: bytes | str) -> Any:
    """Best-effort parse: tries response first, then request, then raw dict.

    Tests for the round-trip property use this to avoid caring whether a
    given fixture is a request or a response.
    """
    obj = _parse_to_dict(line)
    kind = obj.get("type", "")
    if kind in _RESPONSE_TYPES:
        return _from_dict(_RESPONSE_TYPES[kind], obj)
    if kind in _REQUEST_TYPES:
        return _from_dict(_REQUEST_TYPES[kind], obj)
    return obj


def iter_lines(buf: bytes) -> tuple[list[bytes], bytes]:
    """Peel complete NDJSON lines off ``buf``.

    Returns ``(complete_lines, remainder)`` where ``complete_lines`` is the
    list of newline-terminated payloads (without the trailing ``\\n``) and
    ``remainder`` is the still-incomplete trailing bytes. Useful when reads
    from a socket may straddle line boundaries.
    """
    lines: list[bytes] = []
    while True:
        idx = buf.find(b"\n")
        if idx < 0:
            return lines, buf
        lines.append(buf[:idx])
        buf = buf[idx + 1 :]


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


def _parse_to_dict(line: bytes | str) -> dict[str, Any]:
    text = line.decode("utf-8") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        raise ProtocolError("empty NDJSON line")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError(f"expected JSON object, got {type(obj).__name__}")
    return obj


def _from_dict(cls: type, obj: dict[str, Any]) -> Any:
    """Construct ``cls`` from ``obj``, ignoring unknown keys.

    Forward compatibility: an older client receiving a response from a
    newer server with extra fields will silently drop them rather than
    crash. The reverse (newer client, older server) means the new fields
    fall back to their dataclass defaults.
    """
    known = set(cls.__dataclass_fields__.keys())  # type: ignore[attr-defined]
    filtered = {k: v for k, v in obj.items() if k in known}
    return cls(**filtered)
