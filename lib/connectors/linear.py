"""
Linear connector — reference implementation.

Polls the Linear GraphQL API for issues matching the configured filter
(typically: "Ready for Engineering" state in a given team) and emits
one ``IssueDraft`` per ticket.

API surface used
----------------
* Endpoint: ``https://api.linear.app/graphql``
* Auth: ``Authorization: <LINEAR_API_KEY>`` header. The key is read from
  the environment only (``LINEAR_API_KEY`` by default; configurable).
* Query: a small ``issues(filter: {...})`` shape with ``updatedAt`` cursor.

Filter contract
---------------
The operator-provided ``filter`` dict is a subset of Linear's filter
shape, scoped to the cases this reference impl supports:

    {
      "team_key": "ENG",         # required — Linear team key
      "state": "Ready",          # optional — state name to match
      "label": "agent-ready"     # optional — Linear label name
    }

A polled ticket becomes an ``IssueDraft`` with:

    title   = ticket.title
    body    = ticket.description (rendered as-is; Linear uses Markdown)
    labels  = connector default labels (+ severity from runner)
    severity = mapped from ticket priority (1 -> blocker, 2 -> warning,
                                            else -> info)
    source_url = ticket.url

Roughly ~150 lines on purpose — anything more belongs in a richer
``linear_advanced.py`` written by an operator who needs cycles, parents,
or per-project routing.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import HttpClient, IssueDraft, Severity, UrllibHttpClient
from ._state import load_state, save_state

logger = logging.getLogger(__name__)

LINEAR_ENDPOINT = "https://api.linear.app/graphql"

# GraphQL query. Hand-rolled string (no graphql-client dep). Variables
# are limited to ``filter`` and ``orderBy`` so the operator-provided
# filter dict is the only customization knob.
_QUERY = """
query AlfredConnectorPoll($filter: IssueFilter, $first: Int!) {
  issues(filter: $filter, first: $first, orderBy: updatedAt) {
    nodes {
      id
      identifier
      title
      description
      url
      priority
      updatedAt
      state { name type }
      labels { nodes { name } }
    }
  }
}
""".strip()


@dataclass
class LinearConnector:
    """Pull Linear issues into ``IssueDraft``.

    Parameters
    ----------
    name:
        Connector name. Defaults to ``"linear"``; override only if you
        run multiple Linear workspaces and need distinct seen-caches.
    api_key_env:
        Env var to read the Linear API key from. Defaults to
        ``LINEAR_API_KEY``. Never accepts an inline value.
    filter:
        Operator filter — see module docstring.
    default_labels:
        GitHub labels added to every issue this connector files (on top
        of the runner's ``agent:implement`` + ``connector`` defaults).
    default_repo:
        ``org/repo`` or bare slug where issues are filed when a draft
        does not override ``target_repo``.
    http:
        ``HttpClient`` Protocol impl. Defaults to ``UrllibHttpClient``;
        tests inject a fake.
    page_size:
        Linear page size. 50 is the API default; raise carefully.
    """

    name: str = "linear"
    api_key_env: str = "LINEAR_API_KEY"
    filter: dict[str, Any] = field(default_factory=dict)
    default_labels: list[str] = field(default_factory=lambda: ["source:linear"])
    default_repo: str | None = None
    http: HttpClient = field(default_factory=UrllibHttpClient)
    page_size: int = 50

    def poll(self, since: datetime | None) -> list[IssueDraft]:
        api_key = os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            logger.warning(
                "%s: %s is unset; skipping poll (no inline key allowed)",
                self.name,
                self.api_key_env,
            )
            return []

        gql_filter = self._build_filter(since)
        payload = {
            "query": _QUERY,
            "variables": {"filter": gql_filter, "first": self.page_size},
        }
        try:
            resp = self.http.post_json(
                LINEAR_ENDPOINT,
                body=payload,
                headers={"Authorization": api_key},
                timeout=30.0,
            )
        except Exception as e:
            logger.warning("%s: API call failed: %s", self.name, e)
            return []

        if not isinstance(resp, dict):
            logger.warning("%s: unexpected response shape: %r", self.name, type(resp).__name__)
            return []
        if resp.get("errors"):
            logger.warning("%s: GraphQL errors: %s", self.name, resp["errors"])
            return []

        nodes = (((resp.get("data") or {}).get("issues") or {}).get("nodes")) or []
        drafts: list[IssueDraft] = []
        for node in nodes:
            try:
                drafts.append(self._to_draft(node))
            except Exception:
                logger.exception("%s: failed to map node %r", self.name, node.get("id"))
        logger.info("%s: polled %d issues (%d drafts)", self.name, len(nodes), len(drafts))
        return drafts

    def mark_seen(self, draft: IssueDraft) -> None:
        # Runner owns the canonical seen-cache; this is a per-connector
        # belt-and-suspenders write so a buggy runner cannot re-file.
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
    def _build_filter(self, since: datetime | None) -> dict[str, Any]:
        gql: dict[str, Any] = {}
        team_key = self.filter.get("team_key")
        if team_key:
            gql["team"] = {"key": {"eq": team_key}}
        state_name = self.filter.get("state")
        if state_name:
            gql["state"] = {"name": {"eq": state_name}}
        label_name = self.filter.get("label")
        if label_name:
            gql["labels"] = {"some": {"name": {"eq": label_name}}}
        if since is not None:
            gql["updatedAt"] = {"gt": since.isoformat()}
        return gql

    @staticmethod
    def _priority_to_severity(priority: int | None) -> Severity:
        # Linear priorities: 0 (no), 1 (urgent), 2 (high), 3 (medium), 4 (low).
        if priority == 1:
            return "blocker"
        if priority == 2:
            return "warning"
        return "info"

    def _to_draft(self, node: dict[str, Any]) -> IssueDraft:
        identifier = str(node.get("identifier") or node.get("id") or "")
        title = str(node.get("title") or "").strip() or "(untitled Linear issue)"
        body = str(node.get("description") or "").strip()
        url = str(node.get("url") or "")
        priority = node.get("priority")
        labels_meta = ((node.get("labels") or {}).get("nodes")) or []
        label_names = [
            f"linear:{lbl['name'].lower().replace(' ', '-')}"
            for lbl in labels_meta
            if isinstance(lbl, dict) and lbl.get("name")
        ]
        return IssueDraft(
            source=self.name,
            source_id=identifier,
            title=title,
            body=body or "(no description provided in Linear)",
            labels=label_names,
            severity=self._priority_to_severity(priority),
            target_repo=None,
            source_url=url,
        )


__all__ = ["LinearConnector"]
