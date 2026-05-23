"""Focused tests for ``lib.agent_runner.notify``."""

from __future__ import annotations

import io
import json
import urllib.request


def test_slack_post_empty_text_returns_false(fresh_agent_runner):
    """slack_post returns False for empty/whitespace text without hitting the network."""
    ar = fresh_agent_runner
    assert ar.slack_post("") is False
    assert ar.slack_post("   ") is False


def test_slack_post_unknown_severity_coerces_to_info(
    fresh_agent_runner, monkeypatch
):
    """Unknown severity falls back to info (no glyph prefix added)."""
    ar = fresh_agent_runner
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    posted = {}

    def fake_urlopen(req, timeout):
        posted["data"] = req.data
        return _resp_ctx()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ar.slack_post("hello", severity="bogus") is True
    payload = json.loads(posted["data"].decode())
    assert payload["text"] == "hello"
    assert "🚨" not in payload["text"]
    assert "⚠️" not in payload["text"]


def test_slack_post_alert_adds_glyph_and_here_mention(
    fresh_agent_runner, monkeypatch
):
    """alert severity adds a 🚨 prefix and a <!here> mention."""
    ar = fresh_agent_runner
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    posted = {}

    def fake_urlopen(req, timeout):
        posted["data"] = req.data
        return _resp_ctx()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert ar.slack_post("backend is down", severity="alert") is True
    text = json.loads(posted["data"].decode())["text"]
    assert text.startswith("🚨 ")
    assert "<!here>" in text


def test_slack_post_dry_run_skips_network(fresh_agent_runner, monkeypatch):
    """Dry-run never POSTs; the call still reports True (at-least-once)."""
    ar = fresh_agent_runner

    def fake_urlopen(req, timeout):
        raise AssertionError("should not be called in dry-run")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ar.set_dry_run(True)
    try:
        assert ar.slack_post("hello", severity="info") is True
    finally:
        ar.set_dry_run(False)


class _resp_ctx:
    def __enter__(self):
        return io.BytesIO(b"ok")

    def __exit__(self, *args):
        return False
