"""Tests for the conversational Slack extensions (ambient + multi-turn).

These cover the four things the conversational layer promises, all ADDITIVE
and all inert unless explicitly armed:

1. ambient channel listening engage / ignore decisions (deterministic gate +
   the full listener path, including the OFF-by-default contract);
2. bounded multi-turn context and follow-up entity resolution ("do it" /
   "that one" resolving against the previous turn's target);
3. conversational answers to read-only questions (verb selection + a
   conversational lead-in, reusing the existing control handlers);
4. the safety gate is unchanged: a mutating intent NEVER auto-executes from
   prose, ambient or otherwise; it only ever surfaces the operator-confirm card.

The engine is always mocked (``classify_intent`` takes an injected
``engine_invoke``); no network is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_intent import (  # noqa: E402
    ConversationContext,
    RepoCatalog,
    ambient_enabled,
    ambient_engages,
    looks_like_followup_reference,
)
from slack_listener import (  # noqa: E402
    SlackPlanningListener,
    _status_query_plan,
)
from slack_thread_registry import SlackThreadRegistry  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


class CardPoster:
    """Poster that returns an incrementing ``ts`` so a card can be tracked."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._n = 0

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        self._n += 1
        return {"ok": True, "ts": f"170000000{self._n}.000001"}

    def card_ts(self) -> str:
        return f"170000000{1}.000001"


class StubControl:
    """A control handler stub recording the verbs it was asked to run."""

    def __init__(
        self,
        text: str = "*Fleet status*\nall green",
        *,
        action: str | None = None,
        handled: bool = True,
        detail: str = "",
    ) -> None:
        self.calls: list[str] = []
        self._text = text
        self._action = action
        self._handled = handled
        self._detail = detail

    def handle(self, text, *, trusted, actor_user_id=None):
        self.calls.append(text)
        return SimpleNamespace(
            handled=self._handled,
            action=self._action or text.split()[0],
            text=self._text,
            detail=self._detail,
        )


def _intent_engine(payload: dict):
    def _invoke(_prompt: str) -> str:
        return json.dumps(payload)

    return _invoke


def _catalog() -> RepoCatalog:
    return RepoCatalog.build(
        {"acme-frontend": "frontend", "acme-backend": "backend"},
        gh_org="acme-io",
    )


def _channel_msg(
    text: str,
    *,
    event_id: str = "EvAmb",
    user: str = "U1",
    channel: str = "C-FLEET",
    channel_type: str = "channel",
    ts: str = "1716480600.000001",
) -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": channel,
            "channel_type": channel_type,
            "user": user,
            "text": text,
            "ts": ts,
        },
    }


def _reaction(*, reaction: str, ts: str, user: str, channel: str = "C-FLEET") -> dict:
    return {
        "event_id": f"EvReact-{ts}-{user}",
        "event": {
            "type": "reaction_added",
            "user": user,
            "reaction": reaction,
            "item": {"type": "message", "channel": channel, "ts": ts},
        },
    }


# ---------------------------------------------------------------------------
# 1. Ambient engage / ignore decisions (the deterministic gate)
# ---------------------------------------------------------------------------


def test_ambient_engages_on_addressed_by_name() -> None:
    assert ambient_engages("Alfred, what shipped today?") is True
    assert ambient_engages("hey Alfred can you hold acme-io/acme-backend#3") is True


def test_ambient_engages_on_bot_mention_token() -> None:
    assert ambient_engages("can you <@U0BOT> queue it", bot_user_id="U0BOT") is True


def test_ambient_engages_on_fleet_action_cue() -> None:
    assert ambient_engages("assign acme-io/acme-frontend#1") is True
    assert ambient_engages("queue acme-io/acme-frontend#1") is True
    assert ambient_engages("run Batman now") is True
    assert ambient_engages("what's blocked right now?") is True
    assert ambient_engages("what is running on the fleet?") is True


def test_ambient_ignores_ordinary_chatter() -> None:
    assert ambient_engages("lunch anyone?") is False
    assert ambient_engages("nice work on that PR everyone") is False
    assert ambient_engages("") is False
    # A name-mention mid-sentence (not addressed, no action cue) stays quiet.
    assert ambient_engages("I think Alfred Hitchcock movies are great") is False


def test_ambient_enabled_requires_both_flags(monkeypatch) -> None:
    monkeypatch.delenv("ALFRED_INTENT_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("ALFRED_SLACK_AMBIENT", raising=False)
    assert ambient_enabled() is False
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    assert ambient_enabled() is False  # ambient flag still unset
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    assert ambient_enabled() is True
    # Router flag off but ambient on => still off (double gate).
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "0")
    assert ambient_enabled() is False


# ---------------------------------------------------------------------------
# 1b. Ambient through the full listener path
# ---------------------------------------------------------------------------


def test_ambient_inert_by_default_even_with_engine(tmp_path: Path, monkeypatch) -> None:
    # Engine injected and channel allowlisted, but the ambient flags are unset:
    # a plain channel message must be ignored (no behavior change on merge).
    monkeypatch.delenv("ALFRED_INTENT_ROUTER_ENABLED", raising=False)
    monkeypatch.delenv("ALFRED_SLACK_AMBIENT", raising=False)
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(_channel_msg("Alfred, what's the status?"))
    assert result.handled is False
    assert "ambient listening disabled" in result.detail
    assert poster.messages == []


def test_ambient_ignores_non_allowlisted_channel(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-OTHER",),
    )
    result = listener.handle_payload(_channel_msg("Alfred, what's the status?", channel="C-FLEET"))
    assert result.handled is False
    assert "allowlist" in result.detail
    assert poster.messages == []


def test_ambient_ignores_ordinary_chatter_in_listener(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")

    def _engine_must_not_run(_prompt: str) -> str:
        raise AssertionError("the engine must not run for ordinary channel chatter")

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_engine_must_not_run,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(_channel_msg("lunch anyone?"))
    assert result.handled is False
    assert "chatter" in result.detail
    assert poster.messages == []


def test_ambient_engaged_status_question_is_answered(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    poster = CardPoster()
    control = StubControl()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_catalog(),
        control_handler=control,
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(_channel_msg("Alfred, what's running right now?"))
    assert result.handled is True
    assert result.action == "intent_status"
    assert "all green" in poster.messages[-1]["text"]


def test_ambient_engaged_but_unactionable_stays_quiet(tmp_path: Path, monkeypatch) -> None:
    # Engages (addressed by name) but the model returns plan_request: ambient
    # must NOT open a planning draft. It stays silent.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "plan_request", "confidence": 0.9}),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(
        _channel_msg("Alfred, we should redo the onboarding flow someday")
    )
    assert result.handled is False
    assert "not actionable" in result.detail
    # No planning draft, no card.
    assert poster.messages == []


def test_ambient_untrusted_user_dropped_before_engine(tmp_path: Path, monkeypatch) -> None:
    # The trust gate still applies to ambient: an untrusted channel poster is
    # dropped by handle_payload before any ambient / engine logic.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")

    def _engine_must_not_run(_prompt: str) -> str:
        raise AssertionError("untrusted users must be dropped before the engine")

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_engine_must_not_run,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(
        _channel_msg("Alfred queue acme-io/acme-frontend#1", user="U-EVIL")
    )
    assert result.handled is False
    assert "untrusted" in result.detail


def test_ambient_skips_bot_mention_to_avoid_double_processing(tmp_path: Path, monkeypatch) -> None:
    # With ambient enabled AND the app_mention subscription active, a channel
    # post that @mentions the bot arrives BOTH as app_mention and via the
    # channel message feed. The ambient path must skip the message-feed copy
    # (it is owned by the app_mention copy) so the same message is not handled
    # twice. The engine must never run for the skipped copy.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")

    def _engine_must_not_run(_prompt: str) -> str:
        raise AssertionError("a bot-mention message must not be routed by ambient")

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        bot_user_id="U0BOT",
        intent_engine=_engine_must_not_run,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )

    # The literal <@BOT> token survives _clean_slack_text (no pipe), so the
    # ambient gate can recognise the duplicate-delivery and bail out.
    result = listener.handle_payload(
        _channel_msg(
            "<@U0BOT> queue acme-io/acme-frontend#12",
            event_id="EvAmbMention",
        )
    )
    assert result.handled is False
    assert "app_mention" in result.detail
    # Nothing posted: no card, no draft, no ack.
    assert poster.messages == []


def test_ambient_still_engages_addressed_by_name_without_mention(
    tmp_path: Path, monkeypatch
) -> None:
    # The dedup guard is narrow: a message addressed by NAME (no <@BOT> token)
    # is only delivered via the channel feed, so ambient still owns it.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        bot_user_id="U0BOT",
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(_channel_msg("Alfred, queue acme-io/acme-frontend#12"))
    assert result.action == "intent_confirmation_posted"


# ---------------------------------------------------------------------------
# 2. Multi-turn context (bounded) + follow-up resolution
# ---------------------------------------------------------------------------


def test_followup_reference_predicate() -> None:
    assert looks_like_followup_reference("do it") is True
    assert looks_like_followup_reference("yes that one") is True
    assert looks_like_followup_reference("the second one") is True
    # A message naming its own target is not a back-reference.
    assert looks_like_followup_reference("queue acme-io/acme-frontend#9") is False
    # Long prose is treated as a fresh request, not a follow-up.
    assert (
        looks_like_followup_reference(
            "yes and also can you go ahead and rebuild the whole pipeline now"
        )
        is False
    )


def test_context_records_and_returns_last_target() -> None:
    ctx = ConversationContext(now=lambda: 100.0)
    ctx.record(
        "c1",
        text="queue the web app #7",
        action="queue_issue",
        repo="acme-io/acme-frontend",
        issue=7,
    )
    assert ctx.last_target("c1") == ("acme-io/acme-frontend", 7)
    # A later status turn does not clobber the last actionable target.
    ctx.record("c1", text="how's it going", action="status_query")
    assert ctx.last_target("c1") == ("acme-io/acme-frontend", 7)
    # Separate conversation is isolated.
    assert ctx.last_target("c2") == ("", None)


def test_context_is_bounded_by_max_turns() -> None:
    ctx = ConversationContext(max_turns=2, now=lambda: 100.0)
    for n in range(5):
        ctx.record("c1", text=f"turn {n}", action="status_query")
    assert len(ctx.recent("c1")) == 2  # only the last 2 kept


def test_context_expires_by_ttl() -> None:
    clock = {"t": 0.0}
    ctx = ConversationContext(ttl_s=10.0, now=lambda: clock["t"])
    ctx.record("c1", text="queue it", action="queue_issue", repo="acme-io/acme-frontend", issue=7)
    clock["t"] = 5.0
    assert ctx.last_target("c1") == ("acme-io/acme-frontend", 7)  # still live
    clock["t"] = 11.0
    assert ctx.last_target("c1") == ("", None)  # expired
    assert ctx.recent("c1") == []


def test_followup_borrows_previous_turn_target(tmp_path: Path, monkeypatch) -> None:
    # Turn 1 (DM): conversational prose (NOT a leading-verb command) that the
    # router classifies as queue with a full ref, landing a target in context.
    # Turn 2 ("yes, do it") is a bare follow-up that must borrow that target
    # from context and post a card instead of asking "which repo?".
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    catalog = _catalog()

    # First turn: prose so it routes through the intent router (not the literal
    # leading-verb control fast path), so the target is recorded in context.
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 4,
                "confidence": 0.95,
            }
        ),
        repo_catalog=catalog,
        control_handler=StubControl(),
    )
    first = listener.handle_payload(
        {
            "event_id": "Ev-turn1",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "can you arm acme-io/acme-backend#4 for the fleet",
                "ts": "1716480000.000001",
            },
        }
    )
    assert first.action == "intent_confirmation_posted"

    # Second turn in the SAME conversation: a bare "do it" with no entity.
    # The engine now classifies it as queue with no repo/issue; the listener
    # must borrow acme-backend#4 from the recorded context and post a card
    # (not a "which repo?" clarification).
    listener._intent_engine = _intent_engine({"action": "queue_issue", "confidence": 0.8})
    second = listener.handle_payload(
        {
            "event_id": "Ev-turn2",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "yes, do it",
                "ts": "1716480000.000001",  # same root thread
            },
        }
    )
    assert second.action == "intent_confirmation_posted"
    card = poster.messages[-1]
    assert "acme-io/acme-backend#4" in card["text"]


def test_followup_without_prior_target_still_clarifies(tmp_path: Path, monkeypatch) -> None:
    # "do it" with NO prior actionable turn cannot borrow anything; the
    # mutating intent must clarify rather than guess.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "queue_issue", "confidence": 0.8}),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    result = listener.handle_payload(
        {
            "event_id": "Ev-orphan",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "do it",
                "ts": "1716480099.000001",
            },
        }
    )
    assert result.action == "intent_clarify"


def test_dm_followup_shares_context_across_distinct_ts(tmp_path: Path, monkeypatch) -> None:
    # FINDING 1 regression: a real DM follow-up is a fresh top-level message,
    # NOT a threaded reply, so Slack gives it a new ``ts`` and no ``thread_ts``.
    # Context must be keyed on the conversation (the DM channel + user), not on
    # ``root_ts`` -- otherwise turn 2 lands in a different bucket and the bare
    # "do it" can never find the repo/issue from turn 1, falling back to a
    # "which repo?" clarification. The two turns below use DISTINCT ts values
    # (as Slack really delivers them) and must still share context.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    catalog = _catalog()

    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 4,
                "confidence": 0.95,
            }
        ),
        repo_catalog=catalog,
        control_handler=StubControl(),
    )
    first = listener.handle_payload(
        {
            "event_id": "Ev-dm1",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "can you arm acme-io/acme-backend#4 for the fleet",
                "ts": "1716480000.000001",
            },
        }
    )
    assert first.action == "intent_confirmation_posted"

    # Second top-level DM with a DIFFERENT ts and NO thread_ts (a normal DM
    # follow-up, not a threaded reply).
    listener._intent_engine = _intent_engine({"action": "queue_issue", "confidence": 0.8})
    second = listener.handle_payload(
        {
            "event_id": "Ev-dm2",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "yes, do it",
                "ts": "1716480055.000099",  # a fresh ts, no thread_ts
            },
        }
    )
    assert second.action == "intent_confirmation_posted"
    card = poster.messages[-1]
    assert "acme-io/acme-backend#4" in card["text"]


def test_thread_followups_stay_isolated_per_thread(tmp_path: Path, monkeypatch) -> None:
    # The DM keying must NOT bleed threaded conversations together: a bare
    # follow-up in thread B cannot borrow a target recorded only in thread A,
    # even in the same channel from the same user. Thread context still keys on
    # the thread root, so the two threads stay isolated. (Threaded DM replies
    # are still direct intake, so this exercises the router without ambient.)
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 4,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    # Turn in thread A records acme-backend#4.
    first = listener.handle_payload(
        {
            "event_id": "Ev-thrA",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "can you arm acme-io/acme-backend#4 for the fleet",
                "ts": "1716480000.000010",
                "thread_ts": "1716480000.000001",  # thread A root
            },
        }
    )
    assert first.action == "intent_confirmation_posted"

    # A bare "do it" in a DIFFERENT thread (B) must NOT borrow thread A's
    # target; with no prior target in thread B it clarifies.
    listener._intent_engine = _intent_engine({"action": "queue_issue", "confidence": 0.8})
    second = listener.handle_payload(
        {
            "event_id": "Ev-thrB",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "yes, do it",
                "ts": "1716480200.000010",
                "thread_ts": "1716480200.000001",  # thread B root
            },
        }
    )
    assert second.action == "intent_clarify"


# ---------------------------------------------------------------------------
# 3. Conversational read-only answers (verb selection + lead-in)
# ---------------------------------------------------------------------------


def test_status_query_plan_routes_ship_to_runs() -> None:
    verb, lead_in = _status_query_plan("what did you ship today?")
    assert verb == "runs"
    assert lead_in


def test_status_query_plan_routes_blocked_to_plans() -> None:
    verb, lead_in = _status_query_plan("what's blocked right now?")
    assert verb == "plans"
    assert lead_in


def test_status_query_plan_defaults_to_status() -> None:
    verb, lead_in = _status_query_plan("what's running?")
    assert verb == "status"
    assert lead_in


def test_conversational_answer_frames_with_lead_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    control = StubControl(text="*Recent runs*\nLucius shipped PR #5")
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_catalog(),
        control_handler=control,
    )
    result = listener.handle_payload(
        {
            "event_id": "Ev-ship",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "hey what did you ship today?",
                "ts": "1716480700.000001",
            },
        }
    )
    assert result.handled is True
    assert result.action == "intent_status"
    # The 'ship' question routed to the runs handler...
    assert "runs" in control.calls
    text = poster.messages[-1]["text"]
    # ...and the structured output is framed with a conversational lead-in.
    assert "working on recently" in text
    assert "Lucius shipped PR #5" in text


def test_conversational_dry_run_executes_directly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    control = StubControl(text="*Dry-run* `lucius`.\n```ok```")
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "dry_run_agent", "agent": "lucius", "confidence": 0.95}
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )

    result = listener.handle_payload(
        {
            "event_id": "Ev-dryrun",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "can you dry run Lucius?",
                "ts": "1716480800.000001",
            },
        }
    )

    assert result.handled is True
    assert result.action == "intent_dry_run_agent"
    assert control.calls == ["dry-run lucius"]
    assert "I ran the dry-run" in poster.messages[-1]["text"]


def test_conversational_dry_run_failure_does_not_claim_success(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    control = StubControl(
        text="*Dry-run failed* `lucius`.\n```\nunknown agent\n```",
        action="dry-run_failed",
        detail="unknown agent",
    )
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "dry_run_agent", "agent": "lucius", "confidence": 0.95}
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )

    result = listener.handle_payload(
        {
            "event_id": "Ev-dryrun-fails",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "text": "can you dry run Lucius?",
                "ts": "1716480801.000001",
            },
        }
    )

    assert result.handled is True
    assert result.action == "intent_dry_run_agent_failed"
    assert control.calls == ["dry-run lucius"]
    assert "Dry-run failed" in poster.messages[-1]["text"]
    assert "I ran the dry-run" not in poster.messages[-1]["text"]


def test_conversational_run_posts_confirmation_then_executes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UOPERATOR")
    poster = CardPoster()
    control = StubControl(text="*Triggered* `batman`.")
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {"action": "run_agent", "agent": "Batman", "confidence": 0.95}
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )

    posted = listener.handle_payload(
        {
            "event_id": "Ev-run",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "UOPERATOR",
                "text": "please run Batman now",
                "ts": "1716480810.000001",
            },
        }
    )

    assert posted.action == "intent_confirmation_posted"
    assert control.calls == []
    card = json.dumps(poster.messages[-1]["blocks"], ensure_ascii=False)
    assert "trigger" in card
    assert "Batman · Architect" in card

    confirmed = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UOPERATOR",
            channel="D1",
        )
    )

    assert confirmed.handled is True
    assert confirmed.action == "intent_run_agent"
    assert control.calls == ["run batman"]
    assert "Triggered" in poster.messages[-1]["text"]


def test_confirmed_agent_command_failure_marks_thread_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UOPERATOR")
    poster = CardPoster()
    control = StubControl(
        text="*Could not run* `batman`.\n```\nunknown agent\n```",
        action="run_failed",
        detail="unknown agent",
    )
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "run_agent",
                "agent": "batman",
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )

    posted = listener.handle_payload(
        {
            "event_id": "Ev-run-fails",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "UOPERATOR",
                "text": "please run Batman now",
                "ts": "1716480811.000001",
            },
        }
    )
    assert posted.action == "intent_confirmation_posted"

    confirmed = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UOPERATOR",
            channel="D1",
        )
    )

    assert confirmed.handled is True
    assert confirmed.action == "intent_run_agent_failed"
    assert control.calls == ["run batman"]
    assert "Could not run" in poster.messages[-1]["text"]
    record = SlackThreadRegistry(tmp_path / "slack-threads").lookup("D1", poster.card_ts())
    assert record is not None
    assert record.status == "failed"


def test_conversational_schedule_posts_confirmation_then_executes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UOPERATOR")
    poster = CardPoster()
    control = StubControl(text="alfred schedule: updated lucius interval:600 -> interval:1200")
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "schedule_agent",
                "agent": "lucius",
                "schedule": "20m",
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )

    posted = listener.handle_payload(
        {
            "event_id": "Ev-schedule",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "UOPERATOR",
                "text": "change Lucius to every 20 minutes",
                "ts": "1716480820.000001",
            },
        }
    )

    assert posted.action == "intent_confirmation_posted"
    assert control.calls == []
    card = json.dumps(poster.messages[-1]["blocks"], ensure_ascii=False)
    assert "reschedule" in card
    assert "Lucius · Senior Developer -> 20m" in card

    confirmed = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UOPERATOR",
            channel="D1",
        )
    )

    assert confirmed.action == "intent_schedule_agent"
    assert control.calls == ["schedule set lucius 20m"]
    assert "updated lucius" in poster.messages[-1]["text"]


# ---------------------------------------------------------------------------
# 4. Safety gate unchanged: ambient prose NEVER auto-executes a mutation
# ---------------------------------------------------------------------------


def test_ambient_mutating_intent_only_posts_card_never_executes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")

    import issue_queue

    def _must_not_run(*args, **kwargs):
        raise AssertionError("ambient prose must never auto-execute a mutation")

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _must_not_run)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )
    result = listener.handle_payload(_channel_msg("Alfred, queue acme-io/acme-frontend#12"))
    assert result.handled is True
    assert result.action == "intent_confirmation_posted"
    card = poster.messages[-1]
    assert "acme-io/acme-frontend#12" in card["text"]
    assert card.get("blocks")

    # A pending conversational-action record is registered, awaiting the
    # operator's confirm reaction.
    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    record = registry.lookup("C-FLEET", poster.card_ts())
    assert record is not None
    assert record.kind == "conversational_action"
    assert record.status == "awaiting_confirmation"


def test_ambient_confirm_executes_only_after_operator_reaction(tmp_path: Path, monkeypatch) -> None:
    # End-to-end through ambient: nothing runs until the operator reacts on the
    # card, and then it runs exactly the same set_issue_pickup the literal-verb
    # command uses.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UOPERATOR UTEAM")

    import issue_queue

    calls: list[dict] = []

    def _capture(repo, number, *, hold):
        calls.append({"repo": repo, "number": number, "hold": hold})
        return True, f"{repo}#{number} queued for Alfred"

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _capture)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )

    posted = listener.handle_payload(
        _channel_msg("Alfred queue acme-io/acme-frontend#12", user="UOPERATOR")
    )
    assert posted.action == "intent_confirmation_posted"
    assert calls == []  # nothing yet

    confirmed = listener.handle_payload(
        _reaction(reaction="white_check_mark", ts=poster.card_ts(), user="UOPERATOR")
    )
    assert confirmed.handled is True
    assert confirmed.action == "intent_queue_issue"
    assert calls == [{"repo": "acme-io/acme-frontend", "number": 12, "hold": False}]


def test_conversational_assign_posts_confirmation_then_executes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_SLACK_AMBIENT", "1")
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UOPERATOR UTEAM")

    import issue_assignment

    calls: list[dict] = []

    def _capture(repo, number):
        calls.append({"repo": repo, "number": number})
        return SimpleNamespace(
            ok=True,
            detail=f"{repo}#{number} assigned to Lucius by adding `agent:implement`.",
            error="",
        )

    monkeypatch.setattr(issue_assignment, "assign_issue", _capture)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "assign_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        ambient_channels=("C-FLEET",),
    )

    posted = listener.handle_payload(
        _channel_msg("Alfred, assign acme-io/acme-frontend#12", user="UOPERATOR")
    )
    assert posted.action == "intent_confirmation_posted"
    assert calls == []
    card = json.dumps(poster.messages[-1]["blocks"])
    assert "<https://github.com/acme-io/acme-frontend/issues/12|acme-io/acme-frontend#12>" in card

    confirmed = listener.handle_payload(
        _reaction(reaction="white_check_mark", ts=poster.card_ts(), user="UOPERATOR")
    )
    assert confirmed.handled is True
    assert confirmed.action == "intent_assign_issue"
    assert calls == [{"repo": "acme-io/acme-frontend", "number": 12}]
    assert (
        "<https://github.com/acme-io/acme-frontend/issues/12|acme-io/acme-frontend#12>"
        in poster.messages[-1]["text"]
    )
    assert "assigned to Lucius" in poster.messages[-1]["text"]


# ---------------------------------------------------------------------------
# 5. converse action: tier-1 direct answer + tier-2 read-only escalation
# ---------------------------------------------------------------------------


def _dm(text: str, *, event_id: str = "Ev-dm", user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": user,
            "text": text,
            "ts": "1716480600.000001",
        },
    }


def test_converse_tier1_reply_answered_directly(tmp_path: Path, monkeypatch) -> None:
    # A confident converse turn with a usable reply is answered from tier-1; the
    # escalation engine must NOT run.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")

    def _no_escalation(_prompt: str) -> str:
        raise AssertionError("tier-2 must not run when tier-1 answered")

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "converse", "reply": "All quiet. Fleet is green.", "confidence": 0.9}
        ),
        escalation_engine=_no_escalation,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    result = listener.handle_payload(_dm("how are things looking?"))
    assert result.handled is True
    assert result.action == "intent_converse"
    assert "All quiet. Fleet is green." in poster.messages[-1]["text"]


def test_converse_honors_raised_confidence_floor(tmp_path: Path, monkeypatch) -> None:
    # An operator-raised ALFRED_INTENT_ROUTER_MIN_CONFIDENCE must gate tier-1
    # replies: a 0.7-confidence answer under a 0.9 floor escalates instead of
    # posting directly.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_MIN_CONFIDENCE", "0.9")

    escalations: list[str] = []

    def _escalation(prompt: str) -> str:
        escalations.append(prompt)
        return "Escalated answer with full context."

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "converse", "reply": "Half-sure answer.", "confidence": 0.7}
        ),
        escalation_engine=_escalation,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    result = listener.handle_payload(_dm("how are things looking?"))
    assert result.handled is True
    assert escalations, "low-confidence tier-1 reply must escalate under a raised floor"
    assert "Escalated answer with full context." in poster.messages[-1]["text"]


def test_converse_without_reply_escalates_to_tier2(tmp_path: Path, monkeypatch) -> None:
    # No tier-1 reply: escalate ONCE to the read-only tier-2 engine and post
    # its answer. The tier-2 prompt must be read-only framed and carry the
    # persona block and assembled context.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    seen_prompts: list[str] = []

    def _escalation(prompt: str) -> str:
        seen_prompts.append(prompt)
        return "Batman failed on the retry path; the gh token expired."

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "converse", "confidence": 0.4}),
        escalation_engine=_escalation,
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        persona="gilfoyle",
    )
    result = listener.handle_payload(_dm("why did batman fail earlier?"))
    assert result.handled is True
    assert result.action == "intent_converse_escalated"
    assert "retry path" in poster.messages[-1]["text"]
    # Exactly one escalation for the one message.
    assert len(seen_prompts) == 1
    prompt = seen_prompts[0]
    assert "READ-ONLY" in prompt
    assert "Persona:" in prompt
    # Assembled read-only context from the control handler is present.
    assert "all green" in prompt


def test_converse_escalation_disabled_falls_back_to_tier1_reply(
    tmp_path: Path, monkeypatch
) -> None:
    # Tier-2 disabled (no escalation engine) but tier-1 gave SOMETHING even
    # though it was below the floor: we still surface the tier-1 reply rather
    # than going silent.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "converse", "reply": "Probably fine.", "confidence": 0.4}
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    # Disable tier-2 to exercise the tier-1-reply fallback path.
    listener._escalation_engine = None
    result = listener.handle_payload(_dm("anything on fire?"))
    assert result.handled is True
    assert result.action == "intent_converse"
    assert "Probably fine." in poster.messages[-1]["text"]


def test_converse_no_answer_anywhere_is_honest(tmp_path: Path, monkeypatch) -> None:
    # No tier-1 reply and tier-2 returns nothing: post an honest fallback, never
    # a fabricated answer and never a mutation.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "converse", "confidence": 0.3}),
        escalation_engine=lambda _p: "",
        repo_catalog=_catalog(),
        control_handler=StubControl(),
    )
    result = listener.handle_payload(_dm("ponder the meaning of it all"))
    assert result.handled is True
    assert result.action == "intent_converse_unanswered"
    assert "could not answer" in poster.messages[-1]["text"].lower()


def test_converse_never_mutates(tmp_path: Path, monkeypatch) -> None:
    # SAFETY: a converse turn must never reach the queue/assign/hold/run
    # primitives. We make the control handler explode if asked to mutate and
    # confirm converse only ever reads (status/runs/plans for context).
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")

    class ReadOnlyControl(StubControl):
        def handle(self, text, *, trusted, actor_user_id=None):
            verb = text.split()[0]
            assert verb in {"status", "runs", "plans"}, f"mutating verb leaked: {verb}"
            return super().handle(text, trusted=trusted, actor_user_id=actor_user_id)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "converse", "confidence": 0.4}),
        escalation_engine=lambda _p: "Here is a summary.",
        repo_catalog=_catalog(),
        control_handler=ReadOnlyControl(),
    )
    result = listener.handle_payload(_dm("summarize what happened today"))
    assert result.handled is True
    assert result.action == "intent_converse_escalated"


# ---------------------------------------------------------------------------
# 6. status_facet: model hint beats keyword cues, with graceful fallback
# ---------------------------------------------------------------------------


def test_status_facet_overrides_keyword_cues(tmp_path: Path, monkeypatch) -> None:
    # The text reads like fleet health, but the model attaches status_facet=runs.
    # The facet wins: the listener calls the ``runs`` control verb.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    control = StubControl()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "status_query", "status_facet": "runs", "confidence": 0.9}
        ),
        repo_catalog=_catalog(),
        control_handler=control,
    )
    result = listener.handle_payload(_dm("how's everything doing?"))
    assert result.handled is True
    assert result.action == "intent_status"
    assert control.calls == ["runs"]


def test_status_facet_absent_falls_back_to_keyword_cue(tmp_path: Path, monkeypatch) -> None:
    # No facet: the deterministic keyword cue ("shipped" -> runs) still drives
    # the verb, preserving prior behavior.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    poster = CardPoster()
    control = StubControl()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.9}),
        repo_catalog=_catalog(),
        control_handler=control,
    )
    result = listener.handle_payload(_dm("what shipped today?"))
    assert result.handled is True
    assert control.calls == ["runs"]


def test_status_query_plan_facet_wins() -> None:
    # Direct unit check of the facet override in the planner helper.
    assert _status_query_plan("how is everything", facet="plans")[0] == "plans"
    assert _status_query_plan("what shipped", facet="fleet")[0] == "status"
    # No facet -> keyword cue.
    assert _status_query_plan("what shipped", facet="")[0] == "runs"
    # Unknown facet -> keyword cue fallback.
    assert _status_query_plan("what shipped", facet="bogus")[0] == "runs"


# ---------------------------------------------------------------------------
# 7. Persona threads through the listener but never alters a gate
# ---------------------------------------------------------------------------


def test_listener_passes_persona_into_classifier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    seen: list[str] = []

    def _engine(prompt: str) -> str:
        seen.append(prompt)
        return json.dumps({"action": "converse", "reply": "Indeed.", "confidence": 0.9})

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_engine,
        escalation_engine=lambda _p: "",
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        persona="gilfoyle",
    )
    listener.handle_payload(_dm("status of the universe?"))
    assert seen, "engine was not called"
    assert "Persona:" in seen[0]
    assert "sardonic" in seen[0].lower()


def test_persona_does_not_change_confirmation_gate(tmp_path: Path, monkeypatch) -> None:
    # SAFETY: even with a persona set, a mutating intent still surfaces the
    # confirmation card and never auto-executes.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    calls: list[dict] = []

    def _set_pickup(repo, number, *, hold):  # pragma: no cover - asserted not called
        calls.append({"repo": repo, "number": number, "hold": hold})
        return True, "done"

    monkeypatch.setattr("issue_queue.set_issue_pickup", _set_pickup, raising=False)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {"action": "hold_issue", "repo": "acme-io/acme-backend", "issue": 3, "confidence": 0.95}
        ),
        repo_catalog=_catalog(),
        control_handler=StubControl(),
        persona="gilfoyle",
    )
    result = listener.handle_payload(_dm("take acme-io/acme-backend#3 off the queue"))
    assert result.action == "intent_confirmation_posted"
    # The mutation has NOT run; it awaits a confirmation reaction.
    assert calls == []


# ---------------------------------------------------------------------------
# 8. Conversation context persistence across a listener restart
# ---------------------------------------------------------------------------


def test_context_survives_listener_restart(tmp_path: Path, monkeypatch) -> None:
    # Turn 1 records a target on one listener; a fresh listener over the same
    # state_root rehydrates it and the bare "do it" follow-up borrows it.
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "1")
    catalog = _catalog()

    poster1 = CardPoster()
    listener1 = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster1,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 4,
                "confidence": 0.95,
            }
        ),
        repo_catalog=catalog,
        control_handler=StubControl(),
    )
    first = listener1.handle_payload(
        _dm("arm acme-io/acme-backend#4 for the fleet", event_id="Ev-r1")
    )
    assert first.action == "intent_confirmation_posted"
    # The context file was written under the shared state root.
    assert (tmp_path / "slack-conversation-context.json").exists()

    # New listener instance, same state root: simulates a process restart.
    poster2 = CardPoster()
    listener2 = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster2,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "queue_issue", "confidence": 0.8}),
        repo_catalog=catalog,
        control_handler=StubControl(),
    )
    second = listener2.handle_payload(_dm("yes, do it", event_id="Ev-r2"))
    assert second.action == "intent_confirmation_posted"
    assert "acme-io/acme-backend#4" in poster2.messages[-1]["text"]
