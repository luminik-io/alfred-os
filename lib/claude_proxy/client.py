"""Client helpers for routing ``claude`` invocations through the proxy.

The single thing this module exports that the rest of the codebase cares
about is :func:`invoke_via_proxy`, a drop-in replacement for the part of
``agent_runner.process.claude_invoke_streaming`` that actually shells out.
The integration in ``process.py`` checks the proxy env var, calls this if
set, and falls back to direct subprocess otherwise.

Design notes:

* The client must work without asyncio in the calling code. Most callers
  in alfred-os are synchronous. We wrap a small asyncio loop internally.
* The fallback path must be cheap to trigger: a missing socket, an unset
  env var, a refused connection, and a connection that closes before
  emitting ``proxy.accepted`` all count as "proxy unavailable" and let
  the caller route to direct subprocess. We never raise from these.
* On success, we surface the raw stream-JSON lines back to the caller as
  parsed dicts via :func:`stream_events`, exactly as the operator would
  have seen them from ``claude -p ... --output-format stream-json``.

What this module does NOT own:

* Parsing the final ``result`` event into a ``ClaudeResult`` -- that is
  done by ``agent_runner.result``, which we don't import to avoid a
  circular dependency.
* Authentication / authorization. The unix socket file permissions and
  the server's peer-uid check are the whole security story.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ENV_SOCKET
from .protocol import InvokeRequest, ProtocolError, parse_response, serialize

_log = logging.getLogger("claude_proxy.client")


class ProxyUnavailable(Exception):
    """The proxy could not be reached; caller should fall back."""


@dataclass
class StreamResult:
    """Outcome of a streamed invocation collected by :func:`invoke_collected`.

    ``events`` is the list of every NDJSON object emitted on the wire,
    including the proxy's own ``proxy.accepted`` and ``proxy.terminal``
    bookends. ``exit_code`` reflects the child's exit; ``-1`` is the
    proxy's sentinel for "killed before reap".
    """

    events: list[dict[str, Any]]
    exit_code: int
    duration_ms: int


def proxy_socket_path() -> Path | None:
    """Read ``$ALFRED_CLAUDE_PROXY_SOCKET`` and validate the path.

    Returns ``None`` when the env var is unset, empty, or points at a
    path that is not an existing socket. This is the canonical predicate
    callers use to decide whether to bother talking to the proxy.
    """
    raw = os.environ.get(ENV_SOCKET, "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        if not path.exists():
            return None
        if not path.is_socket():
            return None
    except OSError:
        return None
    return path


def proxy_available() -> bool:
    """Cheap probe: env var set and socket file present.

    Does NOT touch the network. Use this for routing decisions; reserve
    ``invoke_*`` for actual work, which has its own connect-fail handling.
    """
    return proxy_socket_path() is not None


# --------------------------------------------------------------------------
# Synchronous public surface
# --------------------------------------------------------------------------


def invoke_collected(
    request: InvokeRequest,
    *,
    socket_path: Path | None = None,
    connect_timeout: float = 5.0,
) -> StreamResult:
    """Synchronously invoke via the proxy; return every event as a list.

    Raises :class:`ProxyUnavailable` if the proxy cannot be reached at all
    (so the caller can transparently fall back to direct subprocess).
    Other failures (timeout, child error) surface as events in the result;
    they are part of the protocol, not Python exceptions.
    """
    path = socket_path or proxy_socket_path()
    if path is None:
        raise ProxyUnavailable(f"{ENV_SOCKET} not set or socket missing")

    return asyncio.run(_invoke_async(request, path, connect_timeout))


def stream_events(
    request: InvokeRequest,
    *,
    socket_path: Path | None = None,
    connect_timeout: float = 5.0,
) -> Iterator[dict[str, Any]]:
    """Synchronous generator yielding one parsed event at a time.

    Used by callers that want to act on stream-json events as they
    arrive (e.g. write a transcript line-by-line) rather than buffering
    the whole invocation in memory.
    """
    path = socket_path or proxy_socket_path()
    if path is None:
        raise ProxyUnavailable(f"{ENV_SOCKET} not set or socket missing")

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for event in _stream_async(request, path, connect_timeout):
                await queue.put(event)
        finally:
            await queue.put(None)

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(_producer())
        while True:
            event = loop.run_until_complete(queue.get())
            if event is None:
                break
            yield event
        loop.run_until_complete(task)
    finally:
        loop.close()


# --------------------------------------------------------------------------
# Async implementation
# --------------------------------------------------------------------------


async def _invoke_async(
    request: InvokeRequest,
    socket_path: Path,
    connect_timeout: float,
) -> StreamResult:
    events: list[dict[str, Any]] = []
    exit_code = -1
    duration_ms = 0
    async for event in _stream_async(request, socket_path, connect_timeout):
        events.append(event)
        if event.get("type") == "proxy.terminal":
            exit_code = int(event.get("exit_code", -1))
            duration_ms = int(event.get("duration_ms", 0))
    return StreamResult(events=events, exit_code=exit_code, duration_ms=duration_ms)


async def _stream_async(
    request: InvokeRequest,
    socket_path: Path,
    connect_timeout: float,
) -> AsyncIterator[dict[str, Any]]:
    """Connect, send the request, yield each parsed response event."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path)),
            timeout=connect_timeout,
        )
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise ProxyUnavailable(f"could not connect to {socket_path}: {e}") from e
    except TimeoutError as e:
        raise ProxyUnavailable(f"connect to {socket_path} timed out") from e

    try:
        writer.write(serialize(request))
        await writer.drain()

        terminal_seen = False
        while True:
            line = await reader.readline()
            if not line:
                if not terminal_seen:
                    # Server closed without a terminal event. Surface a
                    # synthetic one so callers always see the bookend.
                    yield {
                        "type": "proxy.error",
                        "reason": "unexpected-eof",
                        "detail": "server closed before terminal event",
                    }
                return
            try:
                parsed = parse_response(line)
            except ProtocolError as e:
                _log.warning("dropping unparseable line from proxy: %s", e)
                continue

            if hasattr(parsed, "__dataclass_fields__"):
                event_dict: dict[str, Any] = {
                    k: getattr(parsed, k) for k in parsed.__dataclass_fields__
                }
            else:
                event_dict = parsed if isinstance(parsed, dict) else {"raw": parsed}

            yield event_dict
            if event_dict.get("type") == "proxy.terminal":
                terminal_seen = True
                return
            if event_dict.get("type") == "proxy.error":
                # Errors are terminal too -- the proxy will not send anything
                # after this.
                return
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


# --------------------------------------------------------------------------
# Lightweight health helper for operator tooling
# --------------------------------------------------------------------------


def health_check(socket_path: Path | None = None, timeout: float = 2.0) -> dict[str, Any]:
    """Send a single ``health`` request and return the parsed response.

    Synchronous, stdlib-only, no asyncio. Suitable for diagnostic scripts
    and the alfred-os ``doctor`` flow.
    """
    path = socket_path or proxy_socket_path()
    if path is None:
        raise ProxyUnavailable(f"{ENV_SOCKET} not set or socket missing")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall(b'{"type":"health"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise ProxyUnavailable(str(e)) from e
    finally:
        sock.close()
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise ProxyUnavailable("empty health response")
    return json.loads(line.decode("utf-8"))


