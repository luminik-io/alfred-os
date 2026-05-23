"""Tests for ``lib/connectors/linear.py`` with a fake HTTP client.

These tests never touch the network. The ``FakeHttp`` instance captures
the URL/body/headers and returns a canned GraphQL payload, which is
enough to exercise the filter builder, priority -> severity mapping,
and label rendering.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("LINEAR_API_KEY", "lin_test_key")
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    for mod in list(sys.modules):
        if mod.startswith("connectors"):
            del sys.modules[mod]
    yield


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.last_url: str | None = None
        self.last_body = None
        self.last_headers: dict | None = None

    def get_json(self, url, *, headers=None, timeout=30.0):
        raise AssertionError("LinearConnector should not GET")

    def post_json(self, url, *, body, headers=None, timeout=30.0):
        self.last_url = url
        self.last_body = body
        self.last_headers = dict(headers or {})
        return self.response


def _sample_response():
    return {
        "data": {
            "issues": {
                "nodes": [
                    {
                        "id": "uuid-1",
                        "identifier": "ENG-101",
                        "title": "Add billing webhook handler",
                        "description": "We need to handle invoice.paid events.",
                        "url": "https://linear.app/example/issue/ENG-101",
                        "priority": 1,
                        "updatedAt": "2026-05-23T12:00:00Z",
                        "state": {"name": "Ready", "type": "unstarted"},
                        "labels": {"nodes": [{"name": "backend"}, {"name": "Quick Win"}]},
                    },
                    {
                        "id": "uuid-2",
                        "identifier": "ENG-102",
                        "title": "Tweak copy on pricing page",
                        "description": "",
                        "url": "https://linear.app/example/issue/ENG-102",
                        "priority": 4,
                        "updatedAt": "2026-05-23T12:05:00Z",
                        "state": {"name": "Ready", "type": "unstarted"},
                        "labels": {"nodes": []},
                    },
                ]
            }
        }
    }


def test_poll_emits_drafts_and_maps_priority_to_severity():
    from connectors.linear import LinearConnector

    http = FakeHttp(_sample_response())
    conn = LinearConnector(
        filter={"team_key": "ENG", "state": "Ready"},
        default_repo="org/backend",
        default_labels=["source:linear"],
        http=http,
    )

    drafts = conn.poll(since=None)
    assert len(drafts) == 2

    blocker = next(d for d in drafts if d.source_id == "ENG-101")
    assert blocker.severity == "blocker"  # priority 1 -> blocker
    assert blocker.source_url == "https://linear.app/example/issue/ENG-101"
    assert "Add billing webhook handler" in blocker.title
    assert "linear:backend" in blocker.labels
    assert "linear:quick-win" in blocker.labels

    info = next(d for d in drafts if d.source_id == "ENG-102")
    assert info.severity == "info"  # priority 4 -> info
    assert info.body  # empty description gets a placeholder


def test_poll_sends_authorization_header_from_env(monkeypatch):
    from connectors.linear import LinearConnector

    monkeypatch.setenv("LINEAR_API_KEY", "lin_secret_42")
    http = FakeHttp(_sample_response())
    LinearConnector(http=http).poll(since=None)
    assert http.last_headers["Authorization"] == "lin_secret_42"


def test_poll_skips_when_env_key_missing(monkeypatch):
    from connectors.linear import LinearConnector

    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    http = FakeHttp(_sample_response())
    drafts = LinearConnector(http=http).poll(since=None)
    assert drafts == []
    # And no HTTP call was attempted.
    assert http.last_url is None


def test_poll_filter_includes_since_when_provided():
    from datetime import UTC, datetime

    from connectors.linear import LinearConnector

    http = FakeHttp(_sample_response())
    since = datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC)
    LinearConnector(filter={"team_key": "ENG"}, http=http).poll(since=since)

    gql_filter = http.last_body["variables"]["filter"]
    assert gql_filter["team"] == {"key": {"eq": "ENG"}}
    assert gql_filter["updatedAt"] == {"gt": since.isoformat()}


def test_poll_handles_graphql_errors_gracefully():
    from connectors.linear import LinearConnector

    http = FakeHttp({"errors": [{"message": "rate limited"}]})
    drafts = LinearConnector(http=http).poll(since=None)
    assert drafts == []


def test_mark_seen_persists_id(tmp_path):
    from connectors._state import load_state
    from connectors.linear import LinearConnector

    http = FakeHttp(_sample_response())
    conn = LinearConnector(http=http)
    drafts = conn.poll(since=None)
    conn.mark_seen(drafts[0])
    state = load_state("linear")
    assert drafts[0].source_id in state["seen_ids"]
