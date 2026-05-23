"""Async unix-socket daemon that brokers ``claude -p`` invocations.

The daemon's reason for being is documented in ``docs/MACOS_KEYCHAIN.md``:
on macOS, the keychain ACL is bound to the requesting process's session,
not its uid, so a ``claude`` invoked from a non-Aqua launchd-spawned
process cannot read the OAuth credential -- even though the operator is
the same user. The proxy runs in the Aqua session and spawns ``claude`` on
behalf of those agent processes, which fixes the read.

What this module owns:

* The asyncio server lifecycle: bind, accept, shutdown, signal handling.
* Per-connection dispatch on the request ``type`` discriminator.
* Spawning ``claude``, line-buffered stdout pass-through, child cleanup.
* Stale-socket detection + unlink-and-rebind on startup.
* Peer-cred check rejecting connections from other uids.

What it does NOT own:

* The wire protocol (lives in :mod:`claude_proxy.protocol`).
* Client-side fallback to direct subprocess (lives in
  :mod:`claude_proxy.client`).
* Any business logic about prompts, agents, or transcripts; those belong
  to the calling agent process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import DEFAULT_SOCKET_REL_PATH
from .protocol import (
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
    parse_request,
    serialize,
)

_log = logging.getLogger("claude_proxy.server")

# How long we give a child claude to exit after SIGTERM before escalating
# to SIGKILL. Five seconds matches the upstream's ``signal.SIGTERM`` grace
# in other parts of the codebase and is enough for it to flush its final
# stream-JSON line.
_CHILD_SIGTERM_GRACE_S: float = 5.0


@dataclass
class ServerConfig:
    """Static configuration for one daemon process.

    Attributes:
        socket_path: absolute path the unix socket binds to.
        claude_bin: resolved path to the ``claude`` executable.
        audit_log_path: optional JSONL file the daemon appends one event
            per invocation to. ``None`` disables the audit log.
        graceful_shutdown_seconds: on SIGTERM, wait at most this many
            seconds for in-flight invokes to complete before tearing them
            down. ``0`` means: terminate children immediately.
        allowed_uid: only accept connections whose peer uid matches.
            Defaults to the proxy's own uid; cross-uid clients are
            rejected.
    """

    socket_path: Path
    claude_bin: str
    audit_log_path: Path | None = None
    graceful_shutdown_seconds: int = 0
    allowed_uid: int = field(default_factory=os.getuid)


class _ProxyServer:
    """One daemon instance. Lives for the lifetime of the process."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.started_at: float = time.monotonic()
        self._server: asyncio.AbstractServer | None = None
        self._active_children: set[asyncio.subprocess.Process] = set()
        self._shutting_down: bool = False

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bind the socket and start accepting connections.

        Stale-socket handling: if the path already exists but ``connect``
        to it fails (because the previous daemon crashed without
        unlinking), we unlink and rebind. If the path exists AND another
        listener answers, we refuse to start.
        """
        self._prepare_socket_path()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        sock.bind(str(self.config.socket_path))
        os.chmod(self.config.socket_path, 0o600)
        sock.listen(64)

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            sock=sock,
        )
        _log.info(
            "claude-proxy listening on %s (claude_bin=%s)",
            self.config.socket_path,
            self.config.claude_bin,
        )

    def _prepare_socket_path(self) -> None:
        """Ensure parent dir exists and remove any stale socket file."""
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config.socket_path.exists():
            return
        # Path exists. Probe it. If a live listener answers, bail out
        # rather than yanking the socket out from under it.
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.config.socket_path))
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            # Stale -- safe to unlink.
            with contextlib.suppress(FileNotFoundError):
                self.config.socket_path.unlink()
            return
        finally:
            probe.close()
        raise RuntimeError(
            f"another claude-proxy appears to be listening on {self.config.socket_path}"
        )

    async def serve_forever(self) -> None:
        """Run until cancelled (typically by a SIGTERM-driven shutdown)."""
        assert self._server is not None, "call start() first"
        async with self._server:
            await self._server.serve_forever()

    async def shutdown(self) -> None:
        """Stop accepting, then kill / wait on outstanding child claudes."""
        self._shutting_down = True
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

        grace = self.config.graceful_shutdown_seconds
        if grace > 0 and self._active_children:
            _log.info(
                "waiting up to %ds for %d in-flight invocations",
                grace,
                len(self._active_children),
            )
            deadline = time.monotonic() + grace
            while self._active_children and time.monotonic() < deadline:
                await asyncio.sleep(0.1)

        for child in list(self._active_children):
            await _terminate_child(child)

        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()

    # --- connection dispatch -----------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """One connection = one request + its response stream."""
        peername = "unix"
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None and not _peer_uid_allowed(sock, self.config.allowed_uid):
                _log.warning("rejecting connection from disallowed peer uid")
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return

            line = await reader.readline()
            if not line:
                return

            try:
                request = parse_request(line)
            except ProtocolError as e:
                writer.write(serialize(ProxyError(reason="bad-request", detail=str(e))))
                await writer.drain()
                return

            if isinstance(request, HealthRequest):
                await self._handle_health(writer)
            elif isinstance(request, ProbeRequest):
                await self._handle_probe(writer)
            elif isinstance(request, InvokeRequest):
                await self._handle_invoke(request, reader, writer)
            else:  # pragma: no cover -- parse_request can't return anything else
                writer.write(
                    serialize(ProxyError(reason="unsupported", detail=str(request)))
                )
                await writer.drain()
        except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
            raise
        except Exception as e:  # pragma: no cover -- last-ditch safety net
            _log.exception("unhandled error on connection from %s", peername)
            with contextlib.suppress(Exception):
                writer.write(serialize(ProxyError(reason="server-error", detail=repr(e))))
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    # --- health ------------------------------------------------------------

    async def _handle_health(self, writer: asyncio.StreamWriter) -> None:
        """Always-fast liveness reply.

        Health checks must not queue behind in-flight invocations -- they
        share the asyncio event loop, but each invoke awaits on the child
        process's stdout, never blocking the loop. So a separate task is
        unnecessary; we just write and flush.
        """
        response = HealthOk(
            claude_bin=self.config.claude_bin,
            uptime_seconds=int(time.monotonic() - self.started_at),
            pid=os.getpid(),
        )
        writer.write(serialize(response))
        await writer.drain()

    # --- probe -------------------------------------------------------------

    async def _handle_probe(self, writer: asyncio.StreamWriter) -> None:
        """End-to-end check: spawn a tiny ``claude -p "say ok"`` invocation."""
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                self.config.claude_bin,
                "-p",
                "say ok",
                "--output-format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            writer.write(
                serialize(
                    ProbeFail(reason="claude-bin-missing", stderr_tail=str(e))
                )
            )
            await writer.drain()
            return

        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            await _terminate_child(proc)
            writer.write(serialize(ProbeFail(reason="timeout")))
            await writer.drain()
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        if proc.returncode == 0:
            writer.write(serialize(ProbeOk(duration_ms=duration_ms)))
        else:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-512:]
            writer.write(
                serialize(
                    ProbeFail(
                        reason=f"claude-exit-{proc.returncode}",
                        stderr_tail=tail,
                    )
                )
            )
        await writer.drain()

    # --- invoke ------------------------------------------------------------

    async def _handle_invoke(
        self,
        request: InvokeRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Spawn claude and pump its stream-JSON output to the client."""
        workdir = Path(request.workdir)
        if not workdir.is_dir():
            writer.write(
                serialize(
                    ProxyError(
                        reason="bad-workdir",
                        detail=f"{workdir} is not a directory",
                    )
                )
            )
            await writer.drain()
            return

        cmd = self._build_claude_argv(request)
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
            )
        except FileNotFoundError as e:
            writer.write(
                serialize(
                    ProxyError(reason="claude-bin-missing", detail=str(e))
                )
            )
            await writer.drain()
            return

        self._active_children.add(proc)
        try:
            writer.write(
                serialize(ProxyAccepted(claude_bin=self.config.claude_bin, pid=proc.pid))
            )
            await writer.drain()

            await self._pump_invocation(request, proc, reader, writer, start)
        finally:
            self._active_children.discard(proc)
            self._audit(request, proc, start)

    def _build_claude_argv(self, request: InvokeRequest) -> list[str]:
        """Compose the ``claude -p ...`` argv for an ``invoke`` request."""
        argv: list[str] = [
            self.config.claude_bin,
            "-p",
            request.prompt,
            "--allowedTools",
            request.allowed_tools,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if request.max_turns is not None:
            argv.extend(["--max-turns", str(request.max_turns)])
        if request.model:
            argv.extend(["--model", request.model])
        if request.resume_session:
            argv.extend(["--resume", request.resume_session])
        argv.extend(request.claude_args)
        return argv

    async def _pump_invocation(
        self,
        request: InvokeRequest,
        proc: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start_monotonic: float,
    ) -> None:
        """Forward child stdout to client; tear down on disconnect or timeout."""
        assert proc.stdout is not None
        forward_task = asyncio.create_task(_forward_lines(proc.stdout, writer))
        disconnect_task = asyncio.create_task(_wait_for_disconnect(reader))
        timeout = request.timeout_seconds if request.timeout_seconds > 0 else None

        try:
            done, pending = await asyncio.wait(
                {forward_task, disconnect_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            await _terminate_child(proc)
            raise

        if not done:
            # Timed out before either side finished.
            await _terminate_child(proc)
            for task in pending:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            duration_ms = int((time.monotonic() - start_monotonic) * 1000)
            writer.write(
                serialize(
                    ProxyError(
                        reason="timeout",
                        detail=f"exceeded {request.timeout_seconds}s",
                    )
                )
            )
            writer.write(serialize(ProxyTerminal(exit_code=-1, duration_ms=duration_ms)))
            with contextlib.suppress(Exception):
                await writer.drain()
            return

        if disconnect_task in done and forward_task not in done:
            # Client disconnected mid-invocation. Kill the child; no point
            # finishing its work, and we cannot stream the bytes anywhere.
            await _terminate_child(proc)
            forward_task.cancel()
            with contextlib.suppress(BaseException):
                await forward_task
            return

        # Forward task finished -> child closed stdout, so it is exiting.
        for task in pending:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=_CHILD_SIGTERM_GRACE_S)
        except TimeoutError:
            await _terminate_child(proc)
            exit_code = proc.returncode if proc.returncode is not None else -1

        duration_ms = int((time.monotonic() - start_monotonic) * 1000)
        writer.write(
            serialize(ProxyTerminal(exit_code=exit_code, duration_ms=duration_ms))
        )
        with contextlib.suppress(Exception):
            await writer.drain()

    # --- audit log ---------------------------------------------------------

    def _audit(
        self,
        request: InvokeRequest,
        proc: asyncio.subprocess.Process,
        start_monotonic: float,
    ) -> None:
        """Append one summary line per invocation to the audit log.

        Best-effort: any I/O error is swallowed so a full disk can't take
        the daemon down. Audit is for postmortems, not control flow.
        """
        path = self.config.audit_log_path
        if path is None:
            return
        record: dict[str, Any] = {
            "ts": int(time.time()),
            "session_id": request.session_id,
            "workdir": request.workdir,
            "allowed_tools": request.allowed_tools,
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "duration_ms": int((time.monotonic() - start_monotonic) * 1000),
            "claude_bin": self.config.claude_bin,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:  # pragma: no cover -- log-then-swallow
            _log.warning("audit log write failed: %s", e)


# --------------------------------------------------------------------------
# Helpers (module-level so tests can target them directly)
# --------------------------------------------------------------------------


async def _forward_lines(
    src: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Stream NDJSON lines from ``src`` to ``writer`` until EOF.

    We pass child output through unchanged so the wire and the bytes the
    operator would have seen from a direct ``claude --output-format
    stream-json`` invocation stay identical. The client doesn't have to
    care which transport delivered the bytes.
    """
    while True:
        line = await src.readline()
        if not line:
            return
        writer.write(line if line.endswith(b"\n") else line + b"\n")
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return


async def _wait_for_disconnect(reader: asyncio.StreamReader) -> None:
    """Resolve when the client half-closes its side of the socket.

    The proxy expects exactly one request per connection. Anything else
    the client sends after that is treated as a heartbeat we drain, but
    an EOF read means: hang up.
    """
    while True:
        data = await reader.read(4096)
        if not data:
            return


async def _terminate_child(proc: asyncio.subprocess.Process) -> None:
    """Best-effort SIGTERM-then-SIGKILL teardown of a spawned child."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_CHILD_SIGTERM_GRACE_S)
        return
    except TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _peer_uid_allowed(sock: socket.socket, allowed_uid: int) -> bool:
    """Return True if the peer's uid is the same as ``allowed_uid``.

    Cross-platform: Linux uses ``SO_PEERCRED``, macOS / BSD use
    ``LOCAL_PEERCRED`` via ``getsockopt`` (the kernel exposes it on a
    different option name). When neither call works we fail closed.
    """
    try:
        if sys.platform == "linux":
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", data)
            return uid == allowed_uid
        # macOS path: ``getpeereid`` is a libc call, not a getsockopt.
        # Use ctypes; the import is local to avoid the cost when never
        # exercised on platforms that don't need it.
        import ctypes

        libc = ctypes.CDLL(None)
        uid_t = ctypes.c_uint32()
        gid_t = ctypes.c_uint32()
        rc = libc.getpeereid(
            sock.fileno(),
            ctypes.byref(uid_t),
            ctypes.byref(gid_t),
        )
        if rc != 0:
            return False
        return uid_t.value == allowed_uid
    except OSError:
        return False


# --------------------------------------------------------------------------
# Entry point used by bin/claude-proxy.py
# --------------------------------------------------------------------------


def resolve_socket_path(alfred_home: Path | None = None) -> Path:
    """Return ``$ALFRED_HOME/run/claude-proxy.sock`` (overridable via env)."""
    if alfred_home is None:
        alfred_home = Path(
            os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
        )
    return alfred_home / DEFAULT_SOCKET_REL_PATH


def resolve_audit_log_path(alfred_home: Path | None = None) -> Path:
    """Return ``$ALFRED_HOME/state/claude-proxy/log.jsonl``."""
    if alfred_home is None:
        alfred_home = Path(
            os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
        )
    return alfred_home / "state" / "claude-proxy" / "log.jsonl"


async def run_server(
    *,
    socket_path: Path | None = None,
    claude_bin: str | None = None,
    audit_log_path: Path | None = None,
    graceful_shutdown_seconds: int = 0,
) -> int:
    """Async entry point. Returns process exit code.

    Wires SIGTERM / SIGINT to a clean :meth:`_ProxyServer.shutdown`.
    Designed to be ``asyncio.run``-driven from ``bin/claude-proxy.py``.
    """
    if socket_path is None:
        socket_path = resolve_socket_path()
    if claude_bin is None:
        claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    if audit_log_path is None and os.environ.get("ALFRED_CLAUDE_PROXY_AUDIT", "1") != "0":
        audit_log_path = resolve_audit_log_path()

    config = ServerConfig(
        socket_path=Path(socket_path),
        claude_bin=claude_bin,
        audit_log_path=audit_log_path,
        graceful_shutdown_seconds=graceful_shutdown_seconds,
    )
    server = _ProxyServer(config)
    await server.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    serve_task = asyncio.create_task(server.serve_forever())
    stop_task = asyncio.create_task(stop_event.wait())
    _done, pending = await asyncio.wait(
        {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    await server.shutdown()
    return 0
