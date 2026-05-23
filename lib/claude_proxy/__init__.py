"""alfred-claude-proxy: localhost NDJSON-over-unix-socket bridge to ``claude``.

On macOS, when ``claude`` is invoked from a process spawned by launchd, it
cannot read the OAuth token from the operator's keychain: launchd runs in
a different security context than the Aqua login session that created the
keychain item, so the per-application ACL on the credential blocks the
read.

This package solves that with a long-running proxy daemon launched into
the Aqua session (``LimitLoadToSessionType=Aqua`` in its plist). Agent
processes (themselves spawned by launchd, in non-Aqua sessions) connect to
the proxy over a unix domain socket, send an ``invoke`` request, and the
proxy spawns ``claude`` itself. The child ``claude`` inherits the proxy's
Aqua-session security context and therefore its keychain access.

Public submodules:

* :mod:`claude_proxy.protocol` -- request / response dataclasses + NDJSON
  parsing.
* :mod:`claude_proxy.server` -- the async daemon (``bin/claude-proxy.py``
  thin-wraps :func:`claude_proxy.server.run_server`).
* :mod:`claude_proxy.client` -- helper used by ``agent_runner`` to talk to
  the proxy from non-Aqua processes, with a clean fallback path when the
  socket is unset or unreachable.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_SOCKET_REL_PATH",
    "ENV_SOCKET",
]

#: Environment variable agents consult to decide whether to route through
#: the proxy. Unset (or pointing at a missing socket) means: invoke
#: ``claude`` directly via subprocess. This keeps the proxy opt-in.
ENV_SOCKET: str = "ALFRED_CLAUDE_PROXY_SOCKET"

#: Recommended socket path relative to ``$ALFRED_HOME``. The daemon binds
#: here by default; the example launchd plist sets ``ENV_SOCKET`` to the
#: same absolute path for client discovery.
DEFAULT_SOCKET_REL_PATH: str = "run/claude-proxy.sock"
