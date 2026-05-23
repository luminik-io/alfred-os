"""Tests for ``lib/connectors/sentry.py`` with a fake HTTP client.

Cover the level-to-severity map, the ``min_severity`` filter, env-key
gating, URL composition, and ``mark_seen`` persistence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("SENTRY_AUTH_TOKEN", "sntrys_test_token")
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    for mod in list(sys.modules):
        if mod.startswith("connectors"):
            del sys.modules[mod]
    yield


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.last_url: str | None = None
        self.last_headers: dict | None = None

    def get_json(self, url, *, headers=None, timeout=30.0):
        self.last_url = url
        self.last_headers = dict(headers or {})
        return self.response

    def post_json(self, url, *, body, headers=None, timeout=30.0):
        raise AssertionError("SentryConnector should not POST")


def _sample_issues():
    return [
        {
            "id": "1",
            "shortId": "EXAMPLE-WEB-1",
            "title": "TypeError: undefined is not a function",
            "culprit": "checkout.js in handleSubmit",
            "permalink": "https://sentry.io/organizations/example-org/issues/1/",
            "level": "fatal",
            "count": "42",
            "userCount": 7,
        },
        {
            "id": "2",
            "shortId": "EXAMPLE-WEB-2",
            "title": "Slow query: SELECT * FROM users",
            "culprit": "db/queries.py",
            "permalink": "https://sentry.io/organizations/example-org/issues/2/",
            "level": "warning",
            "count": "3",
            "userCount": 1,
        },
        {
            "id": "3",
            "shortId": "EXAMPLE-WEB-3",
            "title": "Verbose log entry",
            "culprit": "",
            "permalink": "https://sentry.io/organizations/example-org/issues/3/",
            "level": "info",
            "count": "1",
            "userCount": 0,
        },
    ]


def test_poll_emits_drafts_above_min_severity():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    conn = SentryConnector(
        organization="example-org",
        project="example-web",
        min_severity="warning",
        default_repo="org/example-web",
        http=http,
    )
    drafts = conn.poll(since=None)
    # Only the fatal and warning issues survive; info is dropped.
    ids = {d.source_id for d in drafts}
    assert ids == {"EXAMPLE-WEB-1", "EXAMPLE-WEB-2"}
    fatal = next(d for d in drafts if d.source_id == "EXAMPLE-WEB-1")
    assert fatal.severity == "blocker"
    warn = next(d for d in drafts if d.source_id == "EXAMPLE-WEB-2")
    assert warn.severity == "warning"


def test_poll_min_severity_blocker_drops_warning_and_info():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    drafts = SentryConnector(
        organization="example-org",
        project="example-web",
        min_severity="blocker",
        http=http,
    ).poll(since=None)
    assert [d.source_id for d in drafts] == ["EXAMPLE-WEB-1"]


def test_poll_sends_bearer_auth():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    SentryConnector(
        organization="example-org",
        project="example-web",
        http=http,
    ).poll(since=None)
    assert http.last_headers["Authorization"] == "Bearer sntrys_test_token"


def test_poll_skips_when_token_missing(monkeypatch):
    from connectors.sentry import SentryConnector

    monkeypatch.delenv("SENTRY_AUTH_TOKEN", raising=False)
    http = FakeHttp(_sample_issues())
    drafts = SentryConnector(
        organization="example-org",
        project="example-web",
        http=http,
    ).poll(since=None)
    assert drafts == []
    assert http.last_url is None


def test_poll_skips_when_org_or_project_missing():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    assert SentryConnector(organization="", project="example-web", http=http).poll(None) == []
    assert SentryConnector(organization="example-org", project="", http=http).poll(None) == []
    assert http.last_url is None


def test_url_includes_org_project_and_query():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    SentryConnector(
        organization="example-org",
        project="example-web",
        http=http,
    ).poll(since=None)
    assert http.last_url is not None
    assert "/projects/example-org/example-web/issues/" in http.last_url
    assert "is%3Aunresolved" in http.last_url or "is:unresolved" in http.last_url


def test_body_includes_event_and_user_count():
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    drafts = SentryConnector(
        organization="example-org",
        project="example-web",
        http=http,
    ).poll(since=None)
    fatal = next(d for d in drafts if d.source_id == "EXAMPLE-WEB-1")
    assert "events" in fatal.body
    assert "42" in fatal.body
    assert "checkout.js" in fatal.body


def test_mark_seen_persists_id():
    from connectors._state import load_state
    from connectors.sentry import SentryConnector

    http = FakeHttp(_sample_issues())
    conn = SentryConnector(
        organization="example-org",
        project="example-web",
        http=http,
    )
    drafts = conn.poll(since=None)
    conn.mark_seen(drafts[0])
    state = load_state("sentry")
    assert drafts[0].source_id in state["seen_ids"]
