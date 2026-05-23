"""
Sentry connector — reference implementation.

Polls the Sentry REST API for unresolved issues at or above a configured
severity threshold and emits one ``IssueDraft`` per Sentry issue.

API surface used
----------------
* Endpoint: ``https://sentry.io/api/0/projects/{org}/{project}/issues/``
  with ``query=is:unresolved`` plus level filter.
* Auth: ``Authorization: Bearer <SENTRY_AUTH_TOKEN>``. Token comes from
  the environment only (``SENTRY_AUTH_TOKEN`` default).
* Pagination: cursor-based. This reference impl pulls a single page
  (``per_page=50``). High-volume orgs that need to drain a backlog
  should run the connector more frequently rather than crank up
  per-poll cost — Alfred is pull-mode.

Severity mapping
----------------
Sentry levels (``fatal``, ``error``, ``warning``, ``info``, ``debug``)
collapse to Alfred's three tiers:

    fatal              -> blocker
    error              -> warning
    warning            -> warning
    info / debug       -> info

A ``min_severity`` filter drops issues below the threshold before
emitting drafts, so a connector configured at ``warning`` will never
file ``info``-level noise.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode

from . import HttpClient, IssueDraft, Severity, UrllibHttpClient
from ._state import load_state, save_state

logger = logging.getLogger(__name__)

# Severity ordering for the ``min_severity`` filter. Higher index = louder.
_SEVERITY_RANK: dict[Severity, int] = {"info": 0, "warning": 1, "blocker": 2}

_SENTRY_LEVEL_TO_SEVERITY: dict[str, Severity] = {
    "debug": "info",
    "info": "info",
    "warning": "warning",
    "error": "warning",
    "fatal": "blocker",
}


@dataclass
class SentryConnector:
    """Pull Sentry issues into ``IssueDraft``.

    Parameters
    ----------
    name:
        Connector name. Defaults to ``"sentry"``.
    api_key_env:
        Env var holding the Sentry auth token. Defaults to
        ``SENTRY_AUTH_TOKEN``.
    organization:
        Sentry organization slug. Required.
    project:
        Sentry project slug. Required.
    min_severity:
        Drop issues whose mapped Alfred severity is below this tier.
        Default ``warning`` (``info``/``debug`` are dropped).
    base_url:
        Override the API base. Defaults to ``https://sentry.io/api/0``.
        Operators on self-hosted Sentry override this.
    default_labels / default_repo / http / page_size:
        See ``LinearConnector``.
    """

    name: str = "sentry"
    api_key_env: str = "SENTRY_AUTH_TOKEN"
    organization: str = ""
    project: str = ""
    min_severity: Severity = "warning"
    base_url: str = "https://sentry.io/api/0"
    default_labels: list[str] = field(default_factory=lambda: ["source:sentry", "type:bug"])
    default_repo: str | None = None
    http: HttpClient = field(default_factory=UrllibHttpClient)
    page_size: int = 50

    def poll(self, since: datetime | None) -> list[IssueDraft]:
        token = os.environ.get(self.api_key_env, "").strip()
        if not token:
            logger.warning(
                "%s: %s is unset; skipping poll (no inline key allowed)",
                self.name,
                self.api_key_env,
            )
            return []
        if not self.organization or not self.project:
            logger.warning("%s: organization+project are required", self.name)
            return []

        url = self._build_url(since)
        try:
            resp = self.http.get_json(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
        except Exception as e:
            logger.warning("%s: API call failed: %s", self.name, e)
            return []

        if not isinstance(resp, list):
            logger.warning("%s: expected list response, got %s", self.name, type(resp).__name__)
            return []

        threshold = _SEVERITY_RANK[self.min_severity]
        drafts: list[IssueDraft] = []
        for node in resp:
            if not isinstance(node, dict):
                continue
            try:
                draft = self._to_draft(node)
            except Exception:
                logger.exception("%s: failed to map node %r", self.name, node.get("id"))
                continue
            if _SEVERITY_RANK[draft.severity] < threshold:
                continue
            drafts.append(draft)
        logger.info(
            "%s: polled %d nodes (%d drafts after min_severity=%s)",
            self.name,
            len(resp),
            len(drafts),
            self.min_severity,
        )
        return drafts

    def mark_seen(self, draft: IssueDraft) -> None:
        state = load_state(self.name)
        ids = list(state["seen_ids"])
        if draft.source_id not in ids:
            ids.append(draft.source_id)
        last = state["last_poll_at"]
        last_dt = None
        if isinstance(last, str):
            try:
                last_dt = datetime.fromisoformat(last)
            except ValueError:
                last_dt = None
        save_state(self.name, last_poll_at=last_dt, seen_ids=ids)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _build_url(self, since: datetime | None) -> str:
        org = quote(self.organization, safe="")
        proj = quote(self.project, safe="")
        # Sentry query syntax supports ``age:-Nd`` and ``firstSeen:>...``.
        # Use ``lastSeen`` for incremental polling; first poll has no
        # cursor and pulls the most recent ``page_size`` unresolved.
        query_parts = ["is:unresolved"]
        if since is not None:
            query_parts.append(f"lastSeen:>{since.isoformat()}")
        params = {
            "query": " ".join(query_parts),
            "limit": str(self.page_size),
        }
        return f"{self.base_url}/projects/{org}/{proj}/issues/?{urlencode(params, safe=':>-')}"

    @staticmethod
    def _level_to_severity(level: str | None) -> Severity:
        if not level:
            return "info"
        return _SENTRY_LEVEL_TO_SEVERITY.get(level.lower(), "info")

    def _to_draft(self, node: dict[str, Any]) -> IssueDraft:
        short_id = str(node.get("shortId") or node.get("id") or "")
        title = str(node.get("title") or "").strip() or "(untitled Sentry issue)"
        culprit = str(node.get("culprit") or "").strip()
        permalink = str(node.get("permalink") or node.get("permalinkUrl") or "")
        level = self._level_to_severity(node.get("level"))
        count = node.get("count") or "?"
        users = node.get("userCount") if isinstance(node.get("userCount"), int) else "?"

        body_parts = [
            f"**Sentry**: `{short_id}`  ·  **events**: {count}  ·  **users**: {users}",
            "",
        ]
        if culprit:
            body_parts.extend([f"**Culprit**: `{culprit}`", ""])
        body_parts.append(title)
        body = "\n".join(body_parts)

        return IssueDraft(
            source=self.name,
            source_id=short_id,
            title=title,
            body=body,
            labels=[],
            severity=level,
            target_repo=None,
            source_url=permalink,
        )


__all__ = ["SentryConnector"]
