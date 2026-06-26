"""Conversational, streamed Slack answers (``lib/slack_converse``).

Drives the full converse path against a fake Slack client and a fake turn
runner so no network and no live model are touched. Covers the four behaviors
the listener depends on:

* intent routing: a ``conversation`` turn is answered as-is, a ``build`` turn
  keeps its prose and appends an optional issue offer (and omits the offer when
  the bridge is disabled);
* streaming: ``chat.update`` calls are throttled while text grows and the final
  reconciled answer always lands;
* thread context: prior thread messages are gathered, bounded, role-tagged, and
  the triggering message is excluded;
* gating: converse stays inert unless enabled, has an engine, and the channel is
  on the allowlist.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import slack_converse as sc  # noqa: E402
from compose_converse import (  # noqa: E402
    INTENT_BUILD,
    INTENT_CONVERSATION,
    ConverseMessage,
    ConverseReadiness,
    ConverseTurn,
)
from spec_helper import IssueDraft  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic monotonic clock the throttle reads instead of wall time."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSlackClient:
    """Records every Slack call; mints a ts for each posted message."""

    def __init__(self, replies: dict | None = None) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self._replies = replies
        self._ts_seq = 0

    def chat_postMessage(self, **kwargs: object) -> dict:
        self._ts_seq += 1
        self.posts.append(dict(kwargs))
        return {"ok": True, "ts": f"100.{self._ts_seq}"}

    def chat_update(self, **kwargs: object) -> dict:
        self.updates.append(dict(kwargs))
        return {"ok": True}

    def conversations_replies(self, **kwargs: object) -> dict:
        if self._replies is None:
            raise AssertionError("conversations_replies should not be called")
        return self._replies


def _turn(reply: str, intent: str, *, title: str = "") -> ConverseTurn:
    return ConverseTurn(
        reply=reply,
        draft=IssueDraft(title=title),
        readiness=ConverseReadiness(score=0, ready=False),
        done=False,
        intent=intent,
    )


def _enabled_config(**overrides: object) -> sc.SlackConverseConfig:
    base: dict[str, object] = {
        "enabled": True,
        "engine": "claude",
        "channels": frozenset(),
        "throttle": 1.2,
        "thread_context": 12,
    }
    base.update(overrides)
    return sc.SlackConverseConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_disabled_config_never_engages() -> None:
    config = sc.SlackConverseConfig(enabled=False, engine="claude")
    assert config.engages("C123") is False


def test_enabled_without_engine_never_engages() -> None:
    config = sc.SlackConverseConfig(enabled=True, engine="")
    assert config.engages("C123") is False


def test_empty_allowlist_engages_every_channel() -> None:
    config = _enabled_config(channels=frozenset())
    assert config.engages("C123") is True
    assert config.engages("Cwhatever") is True


def test_allowlist_scopes_to_listed_channels() -> None:
    config = _enabled_config(channels=frozenset({"CALLOWED"}))
    assert config.engages("CALLOWED") is True
    assert config.engages("COTHER") is False


def test_from_env_parses_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sc.ENV_ENABLED, "true")
    monkeypatch.setenv(sc.ENV_ENGINE, "claude")
    monkeypatch.setenv(sc.ENV_CHANNELS, "C1, C2  C3")
    monkeypatch.setenv(sc.ENV_THREAD_CONTEXT, "5")
    monkeypatch.setenv(sc.ENV_THROTTLE, "2.5")
    config = sc.SlackConverseConfig.from_env()
    assert config.enabled is True
    assert config.engine == "claude"
    assert config.channels == frozenset({"C1", "C2", "C3"})
    assert config.thread_context == 5
    assert config.throttle == 2.5
    assert config.engages("C2") is True
    assert config.engages("C9") is False


def test_from_env_falls_back_to_compose_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sc.ENV_ENGINE, raising=False)
    monkeypatch.setenv(sc.ENV_ENABLED, "1")
    monkeypatch.setenv(sc.ENV_FALLBACK_ENGINE, "codex")
    config = sc.SlackConverseConfig.from_env()
    assert config.engine == "codex"
    assert config.engages("Cx") is True


# ---------------------------------------------------------------------------
# Intent routing / reply rendering
# ---------------------------------------------------------------------------


def test_conversation_turn_is_returned_as_is() -> None:
    turn = _turn("I am Alfred, your build assistant.", INTENT_CONVERSATION)
    reply = sc.render_converse_reply(turn, bridge_enabled=True)
    assert reply.intent == INTENT_CONVERSATION
    assert reply.offered_issue is False
    assert reply.text == "I am Alfred, your build assistant."


def test_build_turn_appends_issue_offer_when_bridge_enabled() -> None:
    turn = _turn("Here is how I would scope it.", INTENT_BUILD, title="Add dark mode")
    reply = sc.render_converse_reply(turn, bridge_enabled=True)
    assert reply.intent == INTENT_BUILD
    assert reply.offered_issue is True
    assert "Here is how I would scope it." in reply.text
    assert "Add dark mode" in reply.text
    assert "ship it" in reply.text
    assert "Nothing is filed and no code runs until you approve." in reply.text


def test_build_turn_omits_offer_when_bridge_disabled() -> None:
    turn = _turn("Here is the plan.", INTENT_BUILD, title="Add dark mode")
    reply = sc.render_converse_reply(turn, bridge_enabled=False)
    assert reply.intent == INTENT_BUILD
    assert reply.offered_issue is False
    assert reply.text == "Here is the plan."
    assert "ship it" not in reply.text


# ---------------------------------------------------------------------------
# Thread context gathering (bounded, role-tagged, best-effort)
# ---------------------------------------------------------------------------


def test_thread_context_is_bounded_and_role_tagged() -> None:
    messages = [
        {"ts": f"1.{i}", "user": "U1" if i % 2 == 0 else "UBOT", "text": f"msg {i}"}
        for i in range(20)
    ]
    # Tag the odd ones as the bot so role-tagging is exercised.
    for i, m in enumerate(messages):
        if i % 2 == 1:
            m["user"] = "UBOT"
    client = FakeSlackClient(replies={"ok": True, "messages": messages})
    out = sc.gather_thread_context(client, channel="C1", root_ts="1.0", bot_user_id="UBOT", limit=5)
    assert len(out) == 5  # bounded to limit
    # Chronological order preserved (most recent five).
    assert [m.content for m in out] == ["msg 15", "msg 16", "msg 17", "msg 18", "msg 19"]
    # UBOT maps to assistant, everyone else to user.
    assert out[0].role == "assistant"  # index 15 -> odd -> bot
    assert out[1].role == "user"  # index 16 -> even -> user


def test_thread_context_only_own_bot_is_assistant() -> None:
    # A third-party bot (bot_id set, but a different user) must NOT be tagged
    # assistant; only Alfred's own user id maps to the assistant role.
    messages = [
        {"ts": "1.1", "user": "U1", "text": "a human question"},
        {"ts": "1.2", "user": "UOTHERBOT", "bot_id": "B999", "text": "a third-party bot post"},
        {"ts": "1.3", "user": "UBOT", "bot_id": "BALFRED", "text": "Alfred's own answer"},
    ]
    client = FakeSlackClient(replies={"ok": True, "messages": messages})
    out = sc.gather_thread_context(client, channel="C1", root_ts="1.0", bot_user_id="UBOT")
    assert [(m.role, m.content) for m in out] == [
        ("user", "a human question"),
        ("user", "a third-party bot post"),
        ("assistant", "Alfred's own answer"),
    ]


def test_thread_context_pages_to_newest_turns() -> None:
    # conversations_replies returns oldest-first and paginates. A long thread
    # must surface the NEWEST ``limit`` turns, not the oldest chunk.
    page_one = [{"ts": f"1.{i}", "user": "U1", "text": f"msg {i}"} for i in range(200)]
    page_two = [{"ts": f"2.{i}", "user": "U1", "text": f"msg {200 + i}"} for i in range(50)]

    class PagingClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def conversations_replies(self, **kwargs: object) -> dict:
            self.calls.append(dict(kwargs))
            if not kwargs.get("cursor"):
                return {
                    "ok": True,
                    "messages": page_one,
                    "response_metadata": {"next_cursor": "PAGE2"},
                }
            return {"ok": True, "messages": page_two}

    client = PagingClient()
    out = sc.gather_thread_context(client, channel="C1", root_ts="1.0", limit=3)
    assert len(client.calls) == 2  # paged forward once
    assert client.calls[1]["cursor"] == "PAGE2"
    # The most recent three turns, in chronological order.
    assert [m.content for m in out] == ["msg 247", "msg 248", "msg 249"]


def test_thread_context_paging_is_bounded() -> None:
    # A pathological thread that always returns another cursor must still stop
    # at the scan cap rather than paging forever.
    page = [{"ts": f"p.{i}", "user": "U1", "text": f"m{i}"} for i in range(sc.THREAD_PAGE_SIZE)]

    class EndlessClient:
        def __init__(self) -> None:
            self.calls = 0

        def conversations_replies(self, **kwargs: object) -> dict:
            self.calls += 1
            return {
                "ok": True,
                "messages": page,
                "response_metadata": {"next_cursor": "MORE"},
            }

    client = EndlessClient()
    out = sc.gather_thread_context(client, channel="C1", root_ts="1.0", limit=4)
    assert len(out) == 4
    # Bounded: never pages past the scan cap.
    assert client.calls <= (sc.THREAD_SCAN_CAP // sc.THREAD_PAGE_SIZE) + 1


def test_thread_context_excludes_triggering_message() -> None:
    messages = [
        {"ts": "1.1", "user": "U1", "text": "earlier"},
        {"ts": "1.2", "user": "U1", "text": "the trigger"},
    ]
    client = FakeSlackClient(replies={"ok": True, "messages": messages})
    out = sc.gather_thread_context(client, channel="C1", root_ts="1.1", limit=12, exclude_ts="1.2")
    assert [m.content for m in out] == ["earlier"]


def test_thread_context_degrades_when_replies_missing() -> None:
    class NoReplies:
        def chat_postMessage(self, **kwargs: object) -> dict:
            return {"ok": True, "ts": "1.0"}

        def chat_update(self, **kwargs: object) -> dict:
            return {"ok": True}

    assert sc.gather_thread_context(NoReplies(), channel="C1", root_ts="1.0") == []


def test_thread_context_degrades_on_not_ok_response() -> None:
    client = FakeSlackClient(replies={"ok": False, "error": "ratelimited"})
    assert sc.gather_thread_context(client, channel="C1", root_ts="1.0") == []


def test_thread_context_zero_limit_returns_empty() -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": [{"ts": "1.1", "text": "x"}]})
    assert sc.gather_thread_context(client, channel="C1", root_ts="1.0", limit=0) == []


# ---------------------------------------------------------------------------
# Streaming poster throttle
# ---------------------------------------------------------------------------


def test_stream_updates_are_throttled() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=1.0, now=clock)
    assert poster.start() is True
    assert poster.message_ts == "100.1"

    # First update too soon (clock has not advanced past throttle): skipped.
    poster.update("partial one")
    assert client.updates == []

    # Advance past the throttle window: now it writes.
    clock.advance(1.5)
    poster.update("partial two")
    assert len(client.updates) == 1
    assert client.updates[0]["text"] == "partial two"

    # Immediately again, still inside the window: skipped.
    poster.update("partial three")
    assert len(client.updates) == 1


def test_finalize_always_writes_ignoring_throttle() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=10.0, now=clock)
    poster.start()
    # No clock advance at all; finalize must still land.
    poster.finalize("the reconciled answer")
    assert len(client.updates) == 1
    assert client.updates[0]["text"] == "the reconciled answer"


# ---------------------------------------------------------------------------
# Reactive 429 / Retry-After backoff
# ---------------------------------------------------------------------------


class _RateLimitResponse:
    """Mimics the slack_sdk response attached to a 429 SlackApiError."""

    def __init__(self, retry_after: object, status: int = 429) -> None:
        self.status_code = status
        self.headers = {"Retry-After": retry_after}


class _RateLimited(Exception):
    def __init__(self, retry_after: object = "1", status: int = 429) -> None:
        super().__init__("ratelimited")
        self.response = _RateLimitResponse(retry_after, status)


class FakeSleep:
    """Records each honored Retry-After wait instead of sleeping."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


class RateLimitedUpdates(FakeSlackClient):
    """chat_update raises a 429 the first ``fails`` times, then succeeds."""

    def __init__(self, fails: int, retry_after: object = "1") -> None:
        super().__init__()
        self._fails = fails
        self._retry_after = retry_after

    def chat_update(self, **kwargs: object) -> dict:
        if self._fails > 0:
            self._fails -= 1
            raise _RateLimited(self._retry_after)
        return super().chat_update(**kwargs)


def test_finalize_retries_on_rate_limit_then_lands() -> None:
    clock = FakeClock()
    sleep = FakeSleep()
    client = RateLimitedUpdates(fails=2, retry_after="2")
    poster = sc.SlackStreamPoster(
        client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock, sleep=sleep
    )
    poster.start()
    poster.finalize("the reconciled answer")
    # It honored Retry-After twice, then the final write landed in full.
    assert sleep.waits == [2.0, 2.0]
    assert client.updates and client.updates[-1]["text"] == "the reconciled answer"


def test_update_drops_on_rate_limit_without_retrying() -> None:
    clock = FakeClock()
    sleep = FakeSleep()
    client = RateLimitedUpdates(fails=5)
    poster = sc.SlackStreamPoster(
        client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock, sleep=sleep
    )
    poster.start()
    poster.update("a streaming partial")
    # A streaming partial that 429s is simply dropped; no wait, no write.
    assert sleep.waits == []
    assert client.updates == []


def test_retry_after_is_clamped_to_a_ceiling() -> None:
    clock = FakeClock()
    sleep = FakeSleep()
    client = RateLimitedUpdates(fails=1, retry_after="99999")
    poster = sc.SlackStreamPoster(
        client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock, sleep=sleep
    )
    poster.start()
    poster.finalize("answer")
    # A hostile Retry-After cannot wedge the poster for minutes.
    assert sleep.waits == [sc.MAX_RETRY_AFTER_SECONDS]


def test_non_rate_limit_error_is_not_retried() -> None:
    clock = FakeClock()
    sleep = FakeSleep()

    class Boom(FakeSlackClient):
        def chat_update(self, **kwargs: object) -> dict:
            raise RuntimeError("network blip")

    poster = sc.SlackStreamPoster(
        Boom(), channel="C1", thread_ts="1.0", throttle=0.0, now=clock, sleep=sleep
    )
    poster.start()
    poster.finalize("answer")
    # A non-429 transport error is swallowed, never retried.
    assert sleep.waits == []


def test_start_retries_placeholder_on_rate_limit() -> None:
    sleep = FakeSleep()

    class RateLimitedPost(FakeSlackClient):
        def __init__(self) -> None:
            super().__init__()
            self._fails = 2

        def chat_postMessage(self, **kwargs: object) -> dict:
            if self._fails > 0:
                self._fails -= 1
                raise _RateLimited("1")
            return super().chat_postMessage(**kwargs)

    poster = sc.SlackStreamPoster(
        RateLimitedPost(), channel="C1", thread_ts="1.0", throttle=0.0, now=FakeClock(), sleep=sleep
    )
    # The placeholder must survive a transient 429 so the turn is not silent.
    assert poster.start() is True
    assert sleep.waits == [1.0, 1.0]


def test_update_skips_identical_text() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock)
    poster.start()
    poster.update("same")
    poster.update("same")
    assert len(client.updates) == 1


def test_long_partial_stream_text_is_trimmed() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock)
    poster.start()
    poster.update("x" * (sc.MAX_STREAM_CHARS + 500))
    assert len(client.updates) == 1
    # A streamed partial stays small so a fast stream is cheap to re-post.
    assert len(client.updates[0]["text"]) <= sc.MAX_STREAM_CHARS


def test_finalize_lands_full_answer_above_stream_cap() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock)
    poster.start()
    long_answer = "y" * (sc.MAX_STREAM_CHARS * 3)
    poster.finalize(long_answer)
    assert len(client.updates) == 1
    # The reconciled answer is NOT clipped to the small streaming cap; it lands
    # in full, bounded only by Slack's message-body limit.
    assert len(client.updates[0]["text"]) > sc.MAX_STREAM_CHARS
    assert client.updates[0]["text"] == long_answer


def test_finalize_caps_at_slack_message_limit() -> None:
    clock = FakeClock()
    client = FakeSlackClient()
    poster = sc.SlackStreamPoster(client, channel="C1", thread_ts="1.0", throttle=0.0, now=clock)
    poster.start()
    poster.finalize("z" * (sc.MAX_MESSAGE_CHARS + 5000))
    assert len(client.updates) == 1
    # Still bounded by Slack's body limit so an update never fails for length.
    assert len(client.updates[0]["text"]) <= sc.MAX_MESSAGE_CHARS


# ---------------------------------------------------------------------------
# Full orchestration: classify + stream + render
# ---------------------------------------------------------------------------


def _fake_build_turn(turn: ConverseTurn, transcript_text: str):
    """Build a fake turn runner that writes a transcript then returns ``turn``."""

    def _run(*, messages, engine, timeout, firing_id, workdir):
        # The runner tees assistant text; emulate that by writing the transcript
        # the streamer tails.
        return turn

    return _run


def test_run_slack_converse_conversation_path(tmp_path: Path) -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": []})
    turn = _turn("I help you turn ideas into tracked issues.", INTENT_CONVERSATION)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    outcome = sc.run_slack_converse(
        client=client,
        config=_enabled_config(),
        channel="C1",
        thread_ts="1.0",
        user_message="<@UBOT> who are you?",
        bot_user_id="UBOT",
        exclude_ts="1.0",
        bridge_enabled=True,
        build_turn=_fake_build_turn(turn, ""),
        transcript_for=lambda fid: transcript,
        extract_tokens=lambda p: [],
        now=FakeClock(),
    )
    assert outcome.handled is True
    assert outcome.intent == INTENT_CONVERSATION
    assert outcome.offered_issue is False
    # Placeholder posted, then a final reconciled update with the answer.
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == sc.PLACEHOLDER
    assert client.updates[-1]["text"] == "I help you turn ideas into tracked issues."


def test_run_slack_converse_build_path_offers_issue(tmp_path: Path) -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": []})
    turn = _turn("Sounds like a feature.", INTENT_BUILD, title="Dark mode toggle")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    outcome = sc.run_slack_converse(
        client=client,
        config=_enabled_config(),
        channel="C1",
        thread_ts="1.0",
        user_message="add a dark mode toggle to settings",
        bridge_enabled=True,
        build_turn=_fake_build_turn(turn, ""),
        transcript_for=lambda fid: transcript,
        extract_tokens=lambda p: [],
        now=FakeClock(),
    )
    assert outcome.handled is True
    assert outcome.intent == INTENT_BUILD
    assert outcome.offered_issue is True
    assert "ship it" in client.updates[-1]["text"]


def test_run_slack_converse_streams_partial_tokens(tmp_path: Path) -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": []})
    turn = _turn("Final answer.", INTENT_CONVERSATION)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    clock = FakeClock()

    # Make the runner block briefly so the stream loop tails at least once, and
    # advance the clock so the throttled update fires.
    calls = {"n": 0}

    def _slow_run(*, messages, engine, timeout, firing_id, workdir):
        import time as _time

        _time.sleep(0.35)
        return turn

    def _tokens(_path: Path) -> list[str]:
        clock.advance(2.0)
        calls["n"] += 1
        return ["streaming ", "partial"]

    outcome = sc.run_slack_converse(
        client=client,
        config=_enabled_config(throttle=0.5),
        channel="C1",
        thread_ts="1.0",
        user_message="tell me about reviews",
        bridge_enabled=False,
        build_turn=_slow_run,
        transcript_for=lambda fid: transcript,
        extract_tokens=_tokens,
        now=clock,
    )
    assert outcome.handled is True
    assert outcome.streamed is True
    # A partial update landed before the final reconciled answer.
    streamed_texts = [u["text"] for u in client.updates]
    assert "streaming partial" in streamed_texts
    assert streamed_texts[-1] == "Final answer."


def test_run_slack_converse_handles_unavailable_engine(tmp_path: Path) -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": []})
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    def _no_turn(*, messages, engine, timeout, firing_id, workdir):
        return None

    outcome = sc.run_slack_converse(
        client=client,
        config=_enabled_config(),
        channel="C1",
        thread_ts="1.0",
        user_message="anything",
        build_turn=_no_turn,
        transcript_for=lambda fid: transcript,
        extract_tokens=lambda p: [],
        now=FakeClock(),
    )
    # Honest: handled (we posted a fallback note) but no intent classified.
    assert outcome.handled is True
    assert outcome.intent == ""
    assert "could not reach" in client.updates[-1]["text"].lower()


def test_run_slack_converse_empty_message_is_not_handled(tmp_path: Path) -> None:
    client = FakeSlackClient(replies={"ok": True, "messages": []})

    outcome = sc.run_slack_converse(
        client=client,
        config=_enabled_config(),
        channel="C1",
        thread_ts="1.0",
        user_message="   <@UBOT>   ",
        build_turn=_fake_build_turn(_turn("x", INTENT_CONVERSATION), ""),
        transcript_for=lambda fid: tmp_path / "t.jsonl",
        extract_tokens=lambda p: [],
        now=FakeClock(),
    )
    assert outcome.handled is False
    # Nothing posted for an empty turn.
    assert client.posts == []


def test_run_slack_converse_threads_prior_context(tmp_path: Path) -> None:
    prior = {
        "ok": True,
        "messages": [
            {"ts": "1.0", "user": "U1", "text": "how does review work?"},
            {"ts": "1.1", "user": "UBOT", "text": "Codex reviews each PR."},
            {"ts": "1.2", "user": "U1", "text": "<@UBOT> and the mobile app?"},
        ],
    }
    client = FakeSlackClient(replies=prior)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    seen: dict[str, list[ConverseMessage]] = {}

    def _capture_run(*, messages, engine, timeout, firing_id, workdir):
        seen["messages"] = list(messages)
        return _turn("Same review path on mobile.", INTENT_CONVERSATION)

    sc.run_slack_converse(
        client=client,
        config=_enabled_config(),
        channel="C1",
        thread_ts="1.0",
        user_message="<@UBOT> and the mobile app?",
        bot_user_id="UBOT",
        exclude_ts="1.2",
        build_turn=_capture_run,
        transcript_for=lambda fid: transcript,
        extract_tokens=lambda p: [],
        now=FakeClock(),
    )
    msgs = seen["messages"]
    # Prior two messages (trigger excluded) plus the latest user turn.
    assert [m.content for m in msgs] == [
        "how does review work?",
        "Codex reviews each PR.",
        "and the mobile app?",
    ]
    assert msgs[1].role == "assistant"
    assert msgs[-1].role == "user"
