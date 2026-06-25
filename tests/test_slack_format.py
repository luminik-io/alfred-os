"""Tests for ``lib/slack_format.py``, Block Kit threading helpers.

We don't hit the real Slack API; we monkeypatch ``_api_post`` and
``_resolve_bot_token`` so the tests stay deterministic and offline.

The contract being verified:

- Without a bot token, the helpers return None / False (silent skip).
- With a token + canned API responses, ``firing_thread_root`` returns a
  ``ThreadHandle`` carrying channel + ts.
- The header text is built from ``codename_with_role`` so role wiring
  flows through to the Slack post.
- Severity drives the attachment colour stripe (green / yellow / red).
- The duplicate-render guard from PR #141: top-level ``text`` is a
  generic notification preview and the attachment must NOT also carry
  ``text`` (only ``fallback`` + ``blocks``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod == "slack_format":
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def test_firing_thread_root_returns_none_without_bot_token(monkeypatch):
    import slack_format as sf

    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: None)
    handle = sf.firing_thread_root(
        codename="lucius",
        firing_id="2026-05-09-1432-aa",
        summary_one_liner="firing started",
    )
    assert handle is None


def test_firing_thread_root_posts_block_kit_with_role_when_set(monkeypatch):
    import slack_format as sf

    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Single-repo feature engineer")
    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: "xoxb-fake")
    monkeypatch.setattr(sf, "_get_permalink", lambda *a, **kw: None)

    captured: dict = {}

    def fake_api_post(method, payload, *, token):
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True, "ts": "1700000000.000100", "channel": "C0123"}

    monkeypatch.setattr(sf, "_api_post", fake_api_post)
    handle = sf.firing_thread_root(
        codename="lucius",
        firing_id="2026-05-09-1432-aa",
        summary_one_liner="firing started",
    )
    assert handle is not None
    assert handle.channel == "C0123"
    assert handle.ts == "1700000000.000100"

    # Header carries the role-suffixed codename.
    blocks = captured["payload"]["attachments"][0]["blocks"]
    header_text = blocks[0]["text"]["text"]
    assert "lucius (Single-repo feature engineer)" in header_text
    assert "firing started" in header_text


def test_firing_thread_root_severity_drives_colour(monkeypatch):
    import slack_format as sf

    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: "xoxb-fake")
    monkeypatch.setattr(sf, "_get_permalink", lambda *a, **kw: None)

    captured: dict = {}

    def fake_api_post(method, payload, *, token):
        captured["payload"] = payload
        return {"ok": True, "ts": "1.0", "channel": "C0"}

    monkeypatch.setattr(sf, "_api_post", fake_api_post)
    sf.firing_thread_root(
        codename="lucius",
        firing_id="x",
        summary_one_liner="oops",
        severity="alert",
    )
    assert captured["payload"]["attachments"][0]["color"] == sf.SEVERITY_COLOUR["alert"]


def test_firing_thread_root_no_duplicate_text_in_attachment(monkeypatch):
    """PR #141-equivalent guard: the attachment must NOT carry a ``text``
    field that mirrors the top-level ``text``, otherwise Slack renders
    the body twice in the channel."""
    import slack_format as sf

    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: "xoxb-fake")
    monkeypatch.setattr(sf, "_get_permalink", lambda *a, **kw: None)

    captured: dict = {}

    def fake_api_post(method, payload, *, token):
        captured["payload"] = payload
        return {"ok": True, "ts": "1.0", "channel": "C0"}

    monkeypatch.setattr(sf, "_api_post", fake_api_post)
    sf.firing_thread_root(
        codename="lucius",
        firing_id="x",
        summary_one_liner="post body",
    )
    payload = captured["payload"]
    # Top-level text is the generic preview, not the post body.
    assert payload["text"] == "Alfred · lucius firing"
    # Attachment carries the body inside blocks, NOT in a top-level
    # attachment[].text field.
    assert "text" not in payload["attachments"][0]


def test_firing_thread_reply_returns_false_without_handle():
    import slack_format as sf

    assert sf.firing_thread_reply(None, text="anything") is False


def test_firing_thread_reply_posts_to_thread_ts(monkeypatch):
    import slack_format as sf

    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: "xoxb-fake")

    captured: dict = {}

    def fake_api_post(method, payload, *, token):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(sf, "_api_post", fake_api_post)
    handle = sf.ThreadHandle(channel="C0", ts="1700.0001")
    ok = sf.firing_thread_reply(handle, text="worktree created", severity="info")
    assert ok is True
    assert captured["payload"]["channel"] == "C0"
    assert captured["payload"]["thread_ts"] == "1700.0001"
    # Reply attachment: also no top-level text duplicate.
    assert "text" not in captured["payload"]["attachments"][0]


def test_firing_thread_close_summarises_outcome_duration_firing_id(monkeypatch):
    import slack_format as sf

    monkeypatch.setattr(sf, "_resolve_bot_token", lambda: "xoxb-fake")

    captured: dict = {}

    def fake_api_post(method, payload, *, token):
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(sf, "_api_post", fake_api_post)
    handle = sf.ThreadHandle(channel="C0", ts="1700.0001")
    sf.firing_thread_close(
        handle,
        codename="lucius",
        firing_id="2026-05-09-aa",
        outcome="pr-opened",
        duration_seconds=125.4,
    )
    body = captured["payload"]["attachments"][0]["blocks"][0]["text"]["text"]
    assert "lucius" in body
    assert "pr-opened" in body
    assert "2m 5s" in body
    assert "2026-05-09-aa" in body


def test_home_channel_resolution(monkeypatch):
    import slack_format as sf

    monkeypatch.delenv("SLACK_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("BATMAN_APPROVAL_CHANNEL", raising=False)
    assert sf._home_channel() == "alfred"
    monkeypatch.setenv("SLACK_HOME_CHANNEL", "#fleet-ops")
    assert sf._home_channel() == "fleet-ops"
    # Caller-supplied wins.
    assert sf._home_channel("custom") == "custom"


def test_home_channel_batman_approval_alias_wins_over_slack_home(monkeypatch):
    """BATMAN_APPROVAL_CHANNEL is the historical alias from the alfred
    Batman approval flow. It must continue to route firing threads so
    a fleet that wired plan posts to a non-default channel keeps that
    routing."""
    import slack_format as sf

    monkeypatch.setenv("SLACK_HOME_CHANNEL", "fleet-ops")
    monkeypatch.setenv("BATMAN_APPROVAL_CHANNEL", "#fleet-approvals")
    assert sf._home_channel() == "fleet-approvals"


def test_truncate_aggressive_with_marker():
    import slack_format as sf

    short = sf._truncate("abc", 10)
    assert short == "abc"
    long = sf._truncate("a" * 200, 50)
    assert long.endswith("...[truncated]")
    assert len(long) == 50


def test_github_links_render_as_slack_mrkdwn():
    import slack_format as sf

    assert (
        sf.github_issue_link("luminik-io/alfred-os", 113)
        == "<https://github.com/luminik-io/alfred-os/issues/113|luminik-io/alfred-os#113>"
    )
    assert (
        sf.github_url_link("https://github.com/luminik-io/alfred-os/pull/139")
        == "<https://github.com/luminik-io/alfred-os/pull/139|luminik-io/alfred-os#139>"
    )


# --------------------------------------------------------------------------
# Persisted roster theme honored in the Slack header label
# --------------------------------------------------------------------------


def _persist_theme(tmp_path, **payload):
    """Write a roster-theme state file under the isolated ALFRED_HOME."""
    from roster_theme_store import RosterThemeStore
    from agent_runner.paths import STATE_ROOT

    RosterThemeStore.from_state_root(STATE_ROOT).save(**payload)


def test_themed_label_default_matches_codename_with_role(monkeypatch):
    import slack_format as sf
    from agent_runner.metadata import codename_with_role

    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Single-repo feature engineer")
    # No theme persisted: behavior is identical to the shipped helper.
    assert sf._themed_codename_label("lucius") == codename_with_role("lucius")
    assert sf._themed_codename_label("lucius") == "lucius (Single-repo feature engineer)"


def test_themed_label_preset_is_unchanged(tmp_path, monkeypatch):
    import slack_format as sf
    from agent_runner.metadata import codename_with_role

    monkeypatch.setenv("ALFRED_BATMAN_ROLE", "Fleet lead")
    _persist_theme(tmp_path, theme="justice-league")
    # A preset carries no custom names, so the Slack path renders as shipped.
    assert sf._themed_codename_label("batman") == codename_with_role("batman")


def test_themed_label_custom_name_and_role_applied(tmp_path, monkeypatch):
    import slack_format as sf

    monkeypatch.setenv("ALFRED_BATMAN_ROLE", "Fleet lead")
    _persist_theme(
        tmp_path,
        theme="custom",
        custom_names={"batman": "Sherlock"},
        custom_roles={"batman": "Lead detective"},
    )
    assert sf._themed_codename_label("batman") == "Sherlock (Lead detective)"


def test_themed_label_custom_name_falls_back_to_env_role(tmp_path, monkeypatch):
    import slack_format as sf

    monkeypatch.setenv("ALFRED_BATMAN_ROLE", "Fleet lead")
    # Custom name set, but no custom role: the env role still shows.
    _persist_theme(tmp_path, theme="custom", custom_names={"batman": "Sherlock"})
    assert sf._themed_codename_label("batman") == "Sherlock (Fleet lead)"


def test_themed_label_custom_without_name_keeps_shipped_behavior(tmp_path, monkeypatch):
    import slack_format as sf
    from agent_runner.metadata import codename_with_role

    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Engineer")
    # A custom theme that did not name THIS agent renders the shipped codename.
    _persist_theme(tmp_path, theme="custom", custom_names={"batman": "Sherlock"})
    assert sf._themed_codename_label("lucius") == codename_with_role("lucius")
