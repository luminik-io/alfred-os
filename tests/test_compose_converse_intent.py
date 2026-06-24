"""Intent classification for the Compose conversational spec-builder.

Covers the new "conversation vs build" turn kind that lets a plain question
("who are you?") get a chat answer instead of a forced planning card, while a
real build request still produces the structured draft.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import compose_converse as cc  # noqa: E402
from spec_helper import IssueDraft  # noqa: E402


def _empty_draft() -> IssueDraft:
    return IssueDraft(title="")


def _build_draft() -> IssueDraft:
    return IssueDraft(
        title="Add a dark mode toggle",
        desired_behavior="Settings page has a dark mode toggle.",
        repos=["your-org/frontend"],
    )


# --- resolve_intent: model verdict wins -------------------------------------


def test_model_conversation_intent_is_honored() -> None:
    intent = cc.resolve_intent(
        "conversation",
        last_user_message="add a dark mode toggle to the settings page",
        draft=_build_draft(),
        done=False,
    )
    assert intent == cc.INTENT_CONVERSATION


def test_model_build_intent_is_honored() -> None:
    intent = cc.resolve_intent(
        "build",
        last_user_message="who are you?",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_BUILD


def test_unknown_model_intent_falls_back_to_build() -> None:
    # An unexpected value must never suppress the plan surface for real work.
    intent = cc.resolve_intent(
        "smalltalk",
        last_user_message="add a CSV export button",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_BUILD


# --- resolve_intent: heuristic backstop when the model omits intent ----------


def test_heuristic_classifies_identity_question_as_conversation() -> None:
    intent = cc.resolve_intent(
        None,
        last_user_message="Who are you?",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_CONVERSATION


def test_heuristic_classifies_capability_question_as_conversation() -> None:
    intent = cc.resolve_intent(
        None,
        last_user_message="what can you do",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_CONVERSATION


def test_heuristic_classifies_build_request_as_build() -> None:
    intent = cc.resolve_intent(
        None,
        last_user_message="Add a dark mode toggle to the settings page",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_BUILD


def test_heuristic_keeps_build_when_a_draft_already_has_content() -> None:
    # A "thanks" mid-build must not flip an in-progress spec to conversation and
    # wipe the plan; existing draft content forces build.
    intent = cc.resolve_intent(
        None,
        last_user_message="thanks",
        draft=_build_draft(),
        done=False,
    )
    assert intent == cc.INTENT_BUILD


def test_heuristic_mixed_message_stays_build() -> None:
    # "who are you, and can you add X" is a build turn: the opener only matches
    # when the WHOLE short message is a known greeting.
    intent = cc.resolve_intent(
        None,
        last_user_message="who are you, and can you add a dark mode toggle?",
        draft=_empty_draft(),
        done=False,
    )
    assert intent == cc.INTENT_BUILD


def test_heuristic_empty_message_defaults_to_build() -> None:
    intent = cc.resolve_intent(None, last_user_message="", draft=_empty_draft(), done=False)
    assert intent == cc.INTENT_BUILD


# --- parse_turn threads intent through -------------------------------------


def test_parse_turn_reads_model_intent() -> None:
    raw = json.dumps(
        {
            "intent": "conversation",
            "reply": "I'm Alfred. I turn an outcome into a planned change.",
            "draft": {},
            "readiness": {"score": 0, "ready": False, "missing": []},
            "done": False,
        }
    )
    turn = cc.parse_turn(raw, base_draft=_empty_draft(), last_user_message="who are you?")
    assert turn is not None
    assert turn.intent == cc.INTENT_CONVERSATION


def test_parse_turn_backfills_intent_from_heuristic_when_model_omits_it() -> None:
    raw = json.dumps(
        {
            "reply": "I can plan a change with you.",
            "draft": {},
            "readiness": {"score": 0, "ready": False, "missing": []},
            "done": False,
        }
    )
    turn = cc.parse_turn(raw, base_draft=_empty_draft(), last_user_message="what can you do")
    assert turn is not None
    assert turn.intent == cc.INTENT_CONVERSATION


def test_parse_turn_build_request_yields_build_intent() -> None:
    raw = json.dumps(
        {
            "reply": "Which repo is the settings page in?",
            "draft": {"title": "Dark mode toggle"},
            "readiness": {"score": 30, "ready": False, "missing": ["repo scope"]},
            "done": False,
        }
    )
    turn = cc.parse_turn(
        raw,
        base_draft=_empty_draft(),
        last_user_message="add a dark mode toggle to the settings page",
    )
    assert turn is not None
    assert turn.intent == cc.INTENT_BUILD


def test_default_converse_turn_intent_is_build() -> None:
    # The dataclass default keeps older call sites planner-first by default.
    turn = cc.ConverseTurn(
        reply="hi",
        draft=_empty_draft(),
        readiness=cc.ConverseReadiness(score=0, ready=False),
        done=False,
    )
    assert turn.intent == cc.INTENT_BUILD
