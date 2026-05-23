"""Integration tests for the claude-proxy daemon.

Uses a tiny Python "fake claude" binary written into each test's tmpdir.
The fake echoes a few stream-JSON lines, optionally sleeps, and exits with
a configurable code -- enough to exercise every path through the server
without depending on the real ``claude`` CLI.

These tests drive asyncio synchronously via ``asyncio.run`` rather than
relying on a third-party plugin like pytest-asyncio; the project's
constraint is stdlib only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time as time_mod
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.claude_proxy.protocol import InvokeRequest, serialize  # noqa: E402
from lib.claude_proxy.server import ServerConfig, _ProxyServer  # noqa: E402

# --------------------------------------------------------------------------
# Fake-claude builders
# --------------------------------------------------------------------------

_FAKE_OK = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys
    for ev in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "result", "success": True, "num_turns": 1, "cost_usd": 0.0},
    ]:
        sys.stdout.write(json.dumps(ev) + "\\n")
        sys.stdout.flush()
    sys.exit(0)
    """
)

_FAKE_FAIL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys
    sys.stderr.write("simulated auth failure\\n")
    sys.exit(1)
    """
)

_FAKE_SLEEP = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, sys, time
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init"}) + "\\n")
    sys.stdout.flush()
    time.sleep(10)
    sys.stdout.write(json.dumps({"type": "result", "success": True}) + "\\n")
    """
)


def _write_fake(short_tmp: Path, body: str, name: str = "claude") -> Path:
    fake = short_tmp / name
    fake.write_text(body)
    fake.chmod(0o755)
    return fake


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """Provide a short temp dir.

    macOS caps ``AF_UNIX`` paths at ~104 bytes. pytest's stock ``short_tmp``
    fixture, nested inside per-test dirs under the user-cache root, blows
    past that. We use ``/tmp/`` (or whatever ``$TMPDIR`` short-circuits
    to) and yield a hand-rolled dir we clean up ourselves.
    """
    base = "/tmp"
    path = Path(tempfile.mkdtemp(prefix="acp-", dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _make_config(short_tmp: Path, body: str = _FAKE_OK) -> ServerConfig:
    fake = _write_fake(short_tmp, body)
    return ServerConfig(
        socket_path=short_tmp / "claude-proxy.sock",
        claude_bin=str(fake),
        audit_log_path=short_tmp / "audit.jsonl",
    )


# --------------------------------------------------------------------------
# Async helpers
# --------------------------------------------------------------------------


async def _request_and_collect(
    socket_path: Path, request: dict | InvokeRequest
) -> list[dict]:
    """Connect, send one line, collect every NDJSON response."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        if isinstance(request, InvokeRequest):
            writer.write(serialize(request))
        else:
            writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        events: list[dict] = []
        while True:
            line = await reader.readline()
            if not line:
                return events
            events.append(json.loads(line.decode("utf-8")))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _with_server(
    config: ServerConfig,
    body: Callable[[_ProxyServer, ServerConfig], Awaitable[None]],
) -> None:
    """Start a server, run ``body``, always shut down."""
    server = _ProxyServer(config)
    await server.start()
    try:
        await body(server, config)
    finally:
        await server.shutdown()


def _run(coro: Awaitable[None]) -> None:
    asyncio.run(coro)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------


def test_invoke_forwards_stream_json_lines(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="t1",
            claude_args=[],
            timeout_seconds=30,
        )
        events = await _request_and_collect(cfg.socket_path, request)
        types = [e["type"] for e in events]
        assert types[0] == "proxy.accepted"
        assert types[-1] == "proxy.terminal"
        assert "system" in types
        assert "result" in types
        assert events[-1]["exit_code"] == 0

    _run(_with_server(config, body))


def test_health_responds_with_metadata(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        events = await _request_and_collect(cfg.socket_path, {"type": "health"})
        assert len(events) == 1
        assert events[0]["type"] == "health.ok"
        assert events[0]["claude_bin"] == cfg.claude_bin

    _run(_with_server(config, body))


def test_health_during_active_invoke(short_tmp: Path) -> None:
    """A health check must not queue behind an in-flight invoke."""
    config = _make_config(short_tmp, _FAKE_SLEEP)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="t2",
            claude_args=[],
            timeout_seconds=60,
        )
        invoke_task = asyncio.create_task(
            _request_and_collect(cfg.socket_path, request)
        )

        # Let the invocation reach the proxy.accepted point.
        await asyncio.sleep(0.5)

        health_events = await asyncio.wait_for(
            _request_and_collect(cfg.socket_path, {"type": "health"}),
            timeout=2.0,
        )
        assert health_events[0]["type"] == "health.ok"

        invoke_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await invoke_task

    _run(_with_server(config, body))


def test_probe_ok_with_zero_exit_fake(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        events = await _request_and_collect(cfg.socket_path, {"type": "probe"})
        assert events[0]["type"] == "probe.ok"

    _run(_with_server(config, body))


def test_probe_fail_surfaces_stderr(short_tmp: Path) -> None:
    config = _make_config(short_tmp, _FAKE_FAIL)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        events = await _request_and_collect(cfg.socket_path, {"type": "probe"})
        assert events[0]["type"] == "probe.fail"
        assert "auth failure" in events[0].get("stderr_tail", "")

    _run(_with_server(config, body))


def test_bad_workdir_emits_proxy_error(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp / "does-not-exist"),
            allowed_tools="Read",
            session_id="t3",
            claude_args=[],
            timeout_seconds=5,
        )
        events = await _request_and_collect(cfg.socket_path, request)
        assert events[0]["type"] == "proxy.error"
        assert events[0]["reason"] == "bad-workdir"

    _run(_with_server(config, body))


def test_invalid_json_request_emits_proxy_error(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        reader, writer = await asyncio.open_unix_connection(str(cfg.socket_path))
        try:
            writer.write(b"not json at all\n")
            await writer.drain()
            line = await reader.readline()
            assert line
            obj = json.loads(line.decode("utf-8"))
            assert obj["type"] == "proxy.error"
            assert obj["reason"] == "bad-request"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    _run(_with_server(config, body))


def test_client_disconnect_kills_child(short_tmp: Path) -> None:
    """Closing the socket mid-invocation must SIGTERM the child claude."""
    config = _make_config(short_tmp, _FAKE_SLEEP)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="t4",
            claude_args=[],
            timeout_seconds=60,
        )
        reader, writer = await asyncio.open_unix_connection(str(cfg.socket_path))
        writer.write(serialize(request))
        await writer.drain()

        line = await reader.readline()
        accepted = json.loads(line.decode("utf-8"))
        assert accepted["type"] == "proxy.accepted"
        child_pid = accepted["pid"]

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

        # Poll until the child is gone (or fail after a generous deadline).
        deadline = time_mod.monotonic() + 8.0
        while time_mod.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"child pid {child_pid} still alive after disconnect")

    _run(_with_server(config, body))


def test_concurrent_invocations(short_tmp: Path) -> None:
    """Three invokes in flight at once must all complete cleanly."""
    config = _make_config(short_tmp, _FAKE_OK)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        async def one_invoke(i: int) -> list[dict]:
            request = InvokeRequest(
                prompt=f"prompt-{i}",
                workdir=str(short_tmp),
                allowed_tools="Read",
                session_id=f"s{i}",
                claude_args=[],
                timeout_seconds=30,
            )
            return await _request_and_collect(cfg.socket_path, request)

        results = await asyncio.gather(one_invoke(0), one_invoke(1), one_invoke(2))
        for events in results:
            assert events[0]["type"] == "proxy.accepted"
            assert events[-1]["type"] == "proxy.terminal"
            assert events[-1]["exit_code"] == 0

    _run(_with_server(config, body))


def test_audit_log_records_invocations(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="audit-1",
            claude_args=[],
            timeout_seconds=30,
        )
        await _request_and_collect(cfg.socket_path, request)
        assert cfg.audit_log_path is not None
        assert cfg.audit_log_path.exists()
        lines = cfg.audit_log_path.read_text().splitlines()
        assert lines
        record = json.loads(lines[-1])
        assert record["session_id"] == "audit-1"
        assert record["exit_code"] == 0

    _run(_with_server(config, body))


def test_stale_socket_is_replaced_on_start(short_tmp: Path) -> None:
    """A leftover socket file from a crashed daemon must not block startup."""

    async def body() -> None:
        socket_path = short_tmp / "claude-proxy.sock"
        socket_path.touch()
        config = ServerConfig(socket_path=socket_path, claude_bin="/bin/true")
        server = _ProxyServer(config)
        await server.start()
        try:
            assert socket_path.is_socket()
        finally:
            await server.shutdown()

    asyncio.run(body())


def test_live_socket_blocks_second_bind(short_tmp: Path) -> None:
    config = _make_config(short_tmp)

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        config_b = ServerConfig(
            socket_path=cfg.socket_path, claude_bin=cfg.claude_bin
        )
        server_b = _ProxyServer(config_b)
        with pytest.raises(RuntimeError, match="another claude-proxy"):
            await server_b.start()

    _run(_with_server(config, body))


def test_claude_bin_missing_emits_proxy_error(short_tmp: Path) -> None:
    """Wrong path to claude must surface as proxy.error, not crash."""
    config = ServerConfig(
        socket_path=short_tmp / "claude-proxy.sock",
        claude_bin=str(short_tmp / "no-such-binary"),
        audit_log_path=None,
    )

    async def body(_server: _ProxyServer, cfg: ServerConfig) -> None:
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="t5",
            claude_args=[],
            timeout_seconds=5,
        )
        events = await _request_and_collect(cfg.socket_path, request)
        assert any(
            e.get("type") == "proxy.error" and e.get("reason") == "claude-bin-missing"
            for e in events
        )

    _run(_with_server(config, body))
