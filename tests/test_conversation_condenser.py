"""Rolling conversation condenser: keep-first + keep-last, summarize the middle.

Covers the proactive threshold gate (short chats untouched, long chats
condensed), the reactive condense-on-overflow path, the overflow classifier, the
auditable record, and config resolution from env. The summarizer is a
deterministic stub so no model is invoked.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import conversation_condenser as cc  # noqa: E402


@dataclass(frozen=True)
class Msg:
    """A minimal Turn-shaped message for tests."""

    role: str
    content: str


def _convo(n: int) -> list[Msg]:
    """An n-turn conversation: turn 0 is the task, then alternating chatter."""
    msgs = [Msg(role="user", content="TASK: add a dark mode toggle")]
    for i in range(1, n):
        role = "assistant" if i % 2 else "user"
        msgs.append(Msg(role=role, content=f"turn {i} body"))
    return msgs


def _counting_summarizer() -> tuple[cc.Summarizer, list[int]]:
    """A deterministic summarizer; records how many turns it was asked to fold."""
    seen: list[int] = []

    def summarize(turns):  # type: ignore[no-untyped-def]
        seen.append(len(turns))
        return f"SUMMARY of {len(turns)} turns"

    return summarize, seen


# --- proactive gate: short conversations are untouched ----------------------


def test_short_conversation_is_not_condensed() -> None:
    config = cc.CondenserConfig(trigger_turns=40, trigger_chars=48_000)
    summarize, seen = _counting_summarizer()
    result = cc.condense(_convo(5), summarize=summarize, config=config)
    assert result.condensed is False
    assert result.record is None
    assert [m.content for m in result.messages] == [m.content for m in _convo(5)]
    assert seen == []  # summarizer never called for a short chat


def test_disabled_config_never_condenses() -> None:
    config = cc.CondenserConfig(enabled=False, trigger_turns=2)
    summarize, _ = _counting_summarizer()
    result = cc.condense(_convo(50), summarize=summarize, config=config)
    assert result.condensed is False


# --- proactive: long conversation condenses the middle, keeps first + last ---


def test_long_conversation_condenses_middle_keeps_first_and_last() -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=3, trigger_turns=10)
    summarize, seen = _counting_summarizer()
    convo = _convo(20)
    result = cc.condense(convo, summarize=summarize, config=config)

    assert result.condensed is True
    # First turn (the task) is preserved verbatim.
    assert result.messages[0].content == "TASK: add a dark mode toggle"
    # The summary block sits right after the kept-first head.
    assert result.messages[1].role == cc.SUMMARY_ROLE
    assert "SUMMARY of" in result.messages[1].content
    # The last 3 turns are preserved verbatim and in order.
    assert [m.content for m in result.messages[-3:]] == [m.content for m in convo[-3:]]
    # Shape: keep_first(1) + summary(1) + keep_last(3) == 5.
    assert len(result.messages) == 5
    # Exactly the middle run (indices 1..16) was summarized.
    assert result.record is not None
    assert result.record.summarized_indices == tuple(range(1, 17))
    assert seen == [16]


def test_char_budget_trigger_fires_on_few_long_turns() -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=1, trigger_turns=1000, trigger_chars=100)
    summarize, seen = _counting_summarizer()
    convo = [
        Msg(role="user", content="TASK"),
        Msg(role="assistant", content="x" * 200),
        Msg(role="user", content="latest"),
    ]
    result = cc.condense(convo, summarize=summarize, config=config)
    assert result.condensed is True
    assert seen == [1]


def test_summarizer_decline_leaves_conversation_intact() -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=2, trigger_turns=5)

    def decline(_turns):  # type: ignore[no-untyped-def]
        return "   "  # whitespace-only == decline

    convo = _convo(20)
    result = cc.condense(convo, summarize=decline, config=config)
    assert result.condensed is False
    assert len(result.messages) == len(convo)


def test_no_real_middle_is_not_condensed() -> None:
    # keep_first + keep_last covers the whole (triggered) conversation.
    config = cc.CondenserConfig(keep_first=3, keep_last=3, trigger_turns=4)
    summarize, seen = _counting_summarizer()
    result = cc.condense(_convo(6), summarize=summarize, config=config)
    assert result.condensed is False
    assert seen == []


# --- reactive: condense-on-overflow forces a pass ---------------------------


def test_reactive_condense_forces_pass_below_threshold() -> None:
    # trigger_turns is huge so the proactive gate would NOT fire, but the
    # reactive path condenses anyway.
    config = cc.CondenserConfig(keep_first=1, keep_last=2, trigger_turns=10_000)
    summarize, _seen = _counting_summarizer()
    convo = _convo(12)

    proactive = cc.condense(convo, summarize=summarize, config=config)
    assert proactive.condensed is False  # gate did not fire

    reactive = cc.condense_on_overflow(convo, summarize=summarize, config=config)
    assert reactive.condensed is True
    assert reactive.record is not None
    assert reactive.record.reason == "reactive_overflow"
    # keep_first(1) + summary(1) + keep_last(2) == 4.
    assert len(reactive.messages) == 4


def test_reactive_on_minimal_prompt_cannot_shrink() -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=2)
    summarize, _ = _counting_summarizer()
    # 3 turns: keep_first(1) + keep_last(2) leaves no middle.
    result = cc.condense_on_overflow(_convo(3), summarize=summarize, config=config)
    assert result.condensed is False


# --- overflow classifier ----------------------------------------------------


def test_overflow_classifier_matches_common_shapes() -> None:
    assert cc.looks_like_context_overflow("Error: prompt is too long")
    assert cc.looks_like_context_overflow("maximum context length exceeded")
    assert cc.looks_like_context_overflow("This model's context window is too large")
    assert cc.looks_like_context_overflow("too many input tokens for this request")


def test_overflow_classifier_ignores_ordinary_prose() -> None:
    assert not cc.looks_like_context_overflow("I improved the context window handling.")
    assert not cc.looks_like_context_overflow("")
    assert not cc.looks_like_context_overflow(None)


# --- auditable record + persistence -----------------------------------------


def test_record_is_auditable_and_persists(tmp_path: Path) -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=2, trigger_turns=5)
    summarize, _ = _counting_summarizer()
    result = cc.condense(_convo(15), summarize=summarize, config=config)
    assert result.record is not None

    record = result.record
    data = record.to_dict()
    assert data["reason"] == "proactive"
    assert data["summarized_indices"] == list(range(1, 13))
    assert data["original_turn_count"] == 15
    assert data["kept_first"] == 1
    assert data["kept_last"] == 2

    path = cc.persist_record(record, record_dir=tmp_path, slug="draft-1")
    assert path.exists()
    assert path.parent == tmp_path

    import json

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["summarized_indices"] == list(range(1, 13))


# --- config from env --------------------------------------------------------


def test_config_from_env_overrides_defaults() -> None:
    env = {
        cc.ENV_ENABLED: "0",
        cc.ENV_KEEP_FIRST: "2",
        cc.ENV_KEEP_LAST: "4",
        cc.ENV_TRIGGER_TURNS: "12",
        cc.ENV_TRIGGER_CHARS: "9000",
        cc.ENV_MAX_SUMMARY_CHARS: "1500",
    }
    config = cc.CondenserConfig.from_env(env)
    assert config.enabled is False
    assert config.keep_first == 2
    assert config.keep_last == 4
    assert config.trigger_turns == 12
    assert config.trigger_chars == 9000
    assert config.max_summary_chars == 1500


def test_config_from_env_clamps_garbage_to_defaults() -> None:
    env = {
        cc.ENV_KEEP_FIRST: "not-a-number",
        cc.ENV_KEEP_LAST: "-5",  # below the floor of 1
        cc.ENV_TRIGGER_TURNS: "",
    }
    config = cc.CondenserConfig.from_env(env)
    assert config.keep_first == cc.DEFAULT_KEEP_FIRST
    assert config.keep_last == 1  # clamped up to the minimum
    assert config.trigger_turns == cc.DEFAULT_TRIGGER_TURNS


def test_max_summary_chars_truncates_summary_block() -> None:
    config = cc.CondenserConfig(keep_first=1, keep_last=1, trigger_turns=2, max_summary_chars=20)

    def big(_turns):  # type: ignore[no-untyped-def]
        return "y" * 500

    result = cc.condense(_convo(10), summarize=big, config=config)
    assert result.summary_turn is not None
    # Summary body is a framing prefix plus at most max_summary_chars of payload.
    # The payload is appended last, so the block ends with exactly 20 "y"s and no
    # more (the prefix is fixed text and is not counted as payload).
    body = result.summary_turn.content
    assert body.endswith("y" * 20)
    assert not body.endswith("y" * 21)
