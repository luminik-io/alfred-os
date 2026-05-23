"""Tests for the claude-proxy client helpers + fallback behaviour."""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.claude_proxy import ENV_SOCKET  # noqa: E402
from lib.claude_proxy.client import (  # noqa: E402
    ProxyUnavailable,
    health_check,
    invoke_collected,
    proxy_available,
    proxy_socket_path,
)
from lib.claude_proxy.protocol import InvokeRequest  # noqa: E402
from lib.claude_proxy.server import ServerConfig, _ProxyServer  # noqa: E402


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """Short-path tmpdir suitable for AF_UNIX sockets on macOS."""
    path = Path(tempfile.mkdtemp(prefix="acp-cli-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def env_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the proxy env var so each test sets it explicitly."""
    monkeypatch.delenv(ENV_SOCKET, raising=False)


# --------------------------------------------------------------------------
# Predicate tests (no server needed)
# --------------------------------------------------------------------------


def test_proxy_available_returns_false_when_unset(env_clean: None) -> None:
    assert proxy_socket_path() is None
    assert proxy_available() is False


def test_proxy_available_returns_false_when_socket_missing(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    """Env var set, but the path doesn't exist -> not available."""
    monkeypatch.setenv(ENV_SOCKET, str(short_tmp / "no-such.sock"))
    assert proxy_socket_path() is None
    assert proxy_available() is False


def test_proxy_available_returns_false_for_non_socket_file(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    """Env var points at a regular file (not a socket) -> not available."""
    regular = short_tmp / "regular.file"
    regular.write_text("not a socket")
    monkeypatch.setenv(ENV_SOCKET, str(regular))
    assert proxy_socket_path() is None


# --------------------------------------------------------------------------
# Fallback raising
# --------------------------------------------------------------------------


def test_invoke_collected_raises_when_unset(env_clean: None) -> None:
    request = InvokeRequest(
        prompt="x", workdir="/tmp", allowed_tools="Read", session_id="s"
    )
    with pytest.raises(ProxyUnavailable):
        invoke_collected(request)


def test_invoke_collected_raises_when_socket_missing(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    monkeypatch.setenv(ENV_SOCKET, str(short_tmp / "missing.sock"))
    request = InvokeRequest(
        prompt="x", workdir="/tmp", allowed_tools="Read", session_id="s"
    )
    with pytest.raises(ProxyUnavailable):
        invoke_collected(request)


def test_health_check_raises_when_unset(env_clean: None) -> None:
    with pytest.raises(ProxyUnavailable):
        health_check()


# --------------------------------------------------------------------------
# End-to-end against a real (in-process) server
# --------------------------------------------------------------------------

_FAKE_OK = (
    "#!/usr/bin/env python3\n"
    "import json, sys\n"
    "sys.stdout.write(json.dumps({'type':'system','subtype':'init'}) + '\\n')\n"
    "sys.stdout.write(json.dumps({'type':'result','success':True}) + '\\n')\n"
    "sys.stdout.flush()\n"
)


def _spawn_server(short_tmp: Path) -> tuple[_ProxyServer, ServerConfig, asyncio.AbstractEventLoop]:
    """Start a proxy server on its own loop in the current thread."""
    fake = short_tmp / "claude"
    fake.write_text(_FAKE_OK)
    fake.chmod(0o755)
    config = ServerConfig(
        socket_path=short_tmp / "claude-proxy.sock",
        claude_bin=str(fake),
        audit_log_path=None,
    )
    server = _ProxyServer(config)

    # We use a dedicated background thread to run the server's event loop.
    # The synchronous client helper uses asyncio.run internally; nesting
    # loops in one thread isn't allowed.
    import threading

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    stop = threading.Event()

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        try:
            loop.run_until_complete(_wait_for_stop(stop))
        finally:
            loop.run_until_complete(server.shutdown())
            loop.close()

    async def _wait_for_stop(stop_evt: object) -> None:
        while not stop_evt.is_set():  # type: ignore[attr-defined]
            await asyncio.sleep(0.05)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    ready.wait(timeout=5.0)

    # Stash stop+thread so tests can clean up.
    config.audit_log_path = None  # already None, makes intent obvious
    server._test_stop = stop  # type: ignore[attr-defined]
    server._test_thread = thread  # type: ignore[attr-defined]
    return server, config, loop


def _stop_server(server: _ProxyServer) -> None:
    server._test_stop.set()  # type: ignore[attr-defined]
    server._test_thread.join(timeout=5.0)  # type: ignore[attr-defined]


def test_invoke_collected_streams_against_running_server(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    server, config, _loop = _spawn_server(short_tmp)
    try:
        monkeypatch.setenv(ENV_SOCKET, str(config.socket_path))
        request = InvokeRequest(
            prompt="hi",
            workdir=str(short_tmp),
            allowed_tools="Read",
            session_id="cli-1",
            claude_args=[],
            timeout_seconds=30,
        )
        result = invoke_collected(request)
        types = [e["type"] for e in result.events]
        assert types[0] == "proxy.accepted"
        assert types[-1] == "proxy.terminal"
        assert "system" in types
        assert "result" in types
        assert result.exit_code == 0
    finally:
        _stop_server(server)


def test_health_check_against_running_server(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    server, config, _loop = _spawn_server(short_tmp)
    try:
        monkeypatch.setenv(ENV_SOCKET, str(config.socket_path))
        response = health_check()
        assert response["type"] == "health.ok"
        assert response["claude_bin"] == config.claude_bin
    finally:
        _stop_server(server)


def test_invoke_collected_handles_server_crash_after_connect(
    env_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    short_tmp: Path,
) -> None:
    """Client must surface a proxy.error rather than hang on EOF."""
    server, config, _loop = _spawn_server(short_tmp)
    monkeypatch.setenv(ENV_SOCKET, str(config.socket_path))

    # Kill the server before we send anything.
    _stop_server(server)

    request = InvokeRequest(
        prompt="hi",
        workdir=str(short_tmp),
        allowed_tools="Read",
        session_id="crashy",
        claude_args=[],
        timeout_seconds=5,
    )
    with pytest.raises(ProxyUnavailable):
        invoke_collected(request)
