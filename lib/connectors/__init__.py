"""
alfred-os: input connectors.

Connectors are pull-mode adapters that translate non-GitHub signals
(Linear tickets, Sentry issues, future sources) into GitHub issues
labeled ``agent:implement`` so the existing engineering fleet can pick
them up through its normal claim/run/release lifecycle.

Public surface
--------------
- ``Connector`` Protocol — what every source adapter must implement.
- ``IssueDraft`` dataclass — the normalized record a connector emits.
- ``HttpClient`` Protocol — injection seam for tests; default impl uses
  ``urllib.request`` from the stdlib so connectors stay dep-free.
- ``UrllibHttpClient`` — the default stdlib HTTP client.

Design rules
------------
1. Pull-mode only. Operator polls each connector on a schedule. Webhook
   push-mode is deferred to v2 (see ``docs/CONNECTORS.md``).
2. Dedup is per-connector. Each connector owns a seen-cache under
   ``$ALFRED_HOME/state/connectors/<name>.json``. The runner calls
   ``mark_seen`` only after a successful ``gh issue create``.
3. API keys come from the process environment only. No file storage,
   no operator-readable secrets on disk. Connectors document which env
   vars they read.
4. Zero new third-party deps. Every connector goes through stdlib
   ``urllib.request`` (or another stdlib transport) wrapped behind the
   ``HttpClient`` Protocol so tests can inject a fake.
5. Open-Closed: a new source is a new file under ``lib/connectors/``
   that implements the ``Connector`` Protocol. The runner does not
   change.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

# Severity levels accepted on an IssueDraft. The runner maps these onto
# Alfred's existing severity-routing labels:
#
#   info      -> connector:info        (silent, daily digest only)
#   warning   -> connector:warn        (Slack warn channel)
#   blocker   -> connector:blocker     (Slack alert channel)
#
# Keep the vocabulary deliberately small; new tiers must be a deliberate
# product decision, not a connector-author choice.
Severity = Literal["info", "warning", "blocker"]


@dataclass
class IssueDraft:
    """Normalized cross-source issue record.

    Connectors emit ``IssueDraft`` instances. The ``ConnectorRunner``
    converts each draft into a single ``gh issue create`` call against
    the connector's configured target repo.

    Attributes
    ----------
    source:
        Connector ``name`` (e.g. ``"linear"``, ``"sentry"``). Stamped
        onto the seen-cache and the rendered footer.
    source_id:
        Stable identifier from the upstream system. Dedup key. Must be
        idempotent across polls (Linear issue identifier, Sentry issue
        short-id, etc).
    title:
        GitHub issue title. Connectors should keep it under 200 chars;
        the runner truncates aggressively if longer.
    body:
        Markdown body. The runner appends a source-link footer; do not
        include one in the draft body.
    labels:
        Connector-specific labels to add on top of the connector's
        ``default_labels``. The runner always also adds ``agent:implement``.
    severity:
        ``info`` / ``warning`` / ``blocker``. Drives the connector
        severity label only; the agent fleet's Slack routing is keyed
        off the agent's own severity outputs, not the connector's.
    target_repo:
        Bare repo slug (e.g. ``"backend"``) or full ``org/repo``. When
        ``None`` the runner uses the connector's configured default repo.
    source_url:
        URL pointing back to the upstream record. Rendered in the body
        footer so the agent fleet (and humans) can trace context.
    """

    source: str
    source_id: str
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    severity: Severity = "info"
    target_repo: str | None = None
    source_url: str = ""


@runtime_checkable
class HttpClient(Protocol):
    """Minimal HTTP client surface used by every connector.

    A Protocol so tests can inject a fake without subclassing. The
    default implementation is ``UrllibHttpClient`` below; tests pass a
    fake that returns canned JSON.
    """

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object: ...

    def post_json(
        self,
        url: str,
        *,
        body: object,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object: ...


@runtime_checkable
class Connector(Protocol):
    """A pull-mode adapter from an upstream system to ``IssueDraft``.

    Connectors are stateless instances; per-connector state lives on
    disk under ``$ALFRED_HOME/state/connectors/<name>.json``.
    """

    name: str
    default_labels: list[str]
    default_repo: str | None

    def poll(self, since: datetime | None) -> list[IssueDraft]:
        """Return drafts newer than ``since`` (None = first run).

        Must be idempotent. The runner separately filters out drafts
        whose ``source_id`` is already in the seen-cache, so a
        connector may return duplicates without harm; doing the cheap
        filter upstream just saves bandwidth.
        """
        ...

    def mark_seen(self, draft: IssueDraft) -> None:
        """Persist ``draft.source_id`` as already-filed.

        Called by the runner only after ``gh issue create`` succeeds
        (or after a successful dry-run narration).
        """
        ...


# ---------------------------------------------------------------------------
# Default HTTP client. stdlib-only by policy.
# ---------------------------------------------------------------------------


class UrllibHttpClient:
    """Default ``HttpClient`` backed by ``urllib.request``.

    Returns parsed JSON. Raises ``HttpError`` on any non-2xx response so
    callers can decide how loud to be (connectors generally log + skip,
    so one transient API blip never blocks a whole sync).
    """

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object:
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        return self._send(req, timeout=timeout)

    def post_json(
        self,
        url: str,
        *,
        body: object,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> object:
        data = json.dumps(body).encode("utf-8")
        merged: dict[str, str] = {"Content-Type": "application/json"}
        if headers:
            merged.update(headers)
        req = urllib.request.Request(url, data=data, headers=merged, method="POST")
        return self._send(req, timeout=timeout)

    @staticmethod
    def _send(req: urllib.request.Request, *, timeout: float) -> object:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = ""
            with contextlib.suppress(Exception):
                body = e.read().decode("utf-8", errors="replace")
            raise HttpError(e.code, body) from e
        except urllib.error.URLError as e:
            raise HttpError(0, str(e.reason)) from e
        if not 200 <= status < 300:
            raise HttpError(status, payload)
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise HttpError(status, f"non-JSON body: {payload[:200]}") from e


class HttpError(RuntimeError):
    """Raised by ``UrllibHttpClient`` on HTTP errors. Carries status + body."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


__all__ = [
    "Connector",
    "HttpClient",
    "HttpError",
    "IssueDraft",
    "Severity",
    "UrllibHttpClient",
]
