"""Condenser wiring inside ``compose_converse.run_turn``.

Proves the proactive path shrinks the interrogator prompt for a long
conversation, the reactive path condenses-and-retries after a context-overflow
result, and short conversations call the engine exactly once with no
condensation. The engine is a stub: no live model is invoked, and the
summarizer it backs is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import compose_converse as cc  # noqa: E402
import conversation_condenser as condenser  # noqa: E402
from spec_helper import IssueDraft  # noqa: E402


class _Result:
    """A minimal ClaudeResult-shaped stub."""

    def __init__(self, *, success: bool, result_text: str, error_message: str = "") -> None:
        self.success = success
        self.result_text = result_text
        self.error_message = error_message
        self.subtype = "success" if success else "error_api"


_VALID_TURN_JSON = (
    '{"reply": "Got it.", "draft": {"title": "Dark mode"}, '
    '"readiness": {"score": 40, "ready": false}, "done": false, "intent": "build"}'
)


def _messages(n: int) -> list[cc.ConverseMessage]:
    msgs = [cc.ConverseMessage(role="user", content="TASK: add a dark mode toggle")]
    for i in range(1, n):
        role = "assistant" if i % 2 else "user"
        msgs.append(cc.ConverseMessage(role=role, content=f"turn {i} body " + "x" * 40))
    return msgs


class _EngineSpy:
    """Records every invocation; routes condenser vs interrogator by agent name."""

    def __init__(self, *, interrogator_results: list[_Result]) -> None:
        self.interrogator_results = interrogator_results
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, **kwargs: Any) -> tuple[_Result, str]:
        agent = kwargs.get("agent")
        self.calls.append({"prompt": prompt, "agent": agent, "firing_id": kwargs.get("firing_id")})
        if agent == cc.CONDENSER_AGENT:
            return _Result(success=True, result_text="COMPACT SUMMARY of older turns"), "claude"
        # Pop the next scripted interrogator result.
        result = self.interrogator_results.pop(0)
        return result, "claude"

    @property
    def interrogator_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["agent"] == cc.CONVERSE_AGENT]

    @property
    def condenser_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["agent"] == cc.CONDENSER_AGENT]


def _run(spy: _EngineSpy, messages: list[cc.ConverseMessage], **kwargs: Any) -> Any:
    return cc.run_turn(
        system_prompt="SYS",
        messages=messages,
        repo_grounding="REPO",
        code_map="MAP",
        intake_guidance="GUIDE",
        base_draft=IssueDraft(title=""),
        engine="claude",
        workdir=Path("/tmp"),
        invoke=spy,
        **kwargs,
    )


def test_short_conversation_runs_once_without_condensing() -> None:
    spy = _EngineSpy(interrogator_results=[_Result(success=True, result_text=_VALID_TURN_JSON)])
    config = condenser.CondenserConfig(trigger_turns=40, trigger_chars=200_000)
    records: list[condenser.CondensationRecord] = []
    turn = _run(spy, _messages(4), condenser_config=config, on_condense=records.append)

    assert turn is not None
    assert turn.reply == "Got it."
    assert spy.condenser_calls == []  # no summarizer call
    assert len(spy.interrogator_calls) == 1
    assert records == []


def test_long_conversation_condenses_prompt_proactively() -> None:
    spy = _EngineSpy(interrogator_results=[_Result(success=True, result_text=_VALID_TURN_JSON)])
    config = condenser.CondenserConfig(keep_first=1, keep_last=3, trigger_turns=8)
    records: list[condenser.CondensationRecord] = []
    long_convo = _messages(30)

    turn = _run(spy, long_convo, condenser_config=config, on_condense=records.append)

    assert turn is not None
    # Summarizer fired exactly once.
    assert len(spy.condenser_calls) == 1
    # The interrogator prompt carries the injected summary block, not every turn.
    interrogator_prompt = spy.interrogator_calls[0]["prompt"]
    assert "COMPACT SUMMARY of older turns" in interrogator_prompt
    # A middle turn (turn 15) is no longer in the prompt verbatim.
    assert "turn 15 body" not in interrogator_prompt
    # The original task and the latest turn survive.
    assert "TASK: add a dark mode toggle" in interrogator_prompt
    assert "turn 29 body" in interrogator_prompt
    # An auditable record was emitted.
    assert len(records) == 1
    assert records[0].reason == "proactive"


def test_reactive_condense_and_retry_on_overflow() -> None:
    # First interrogator call overflows; after a reactive condense it succeeds.
    overflow = _Result(
        success=False,
        result_text="",
        error_message="API Error: prompt is too long: maximum context length exceeded",
    )
    ok = _Result(success=True, result_text=_VALID_TURN_JSON)
    spy = _EngineSpy(interrogator_results=[overflow, ok])
    # trigger_turns huge so the proactive gate does NOT fire; only the reactive
    # path can condense here.
    config = condenser.CondenserConfig(keep_first=1, keep_last=3, trigger_turns=100_000)
    records: list[condenser.CondensationRecord] = []

    turn = _run(spy, _messages(20), condenser_config=config, on_condense=records.append)

    assert turn is not None
    assert turn.reply == "Got it."
    # Two interrogator attempts: the overflow, then the post-condense retry.
    assert len(spy.interrogator_calls) == 2
    # Exactly one summarizer call (the reactive condensation).
    assert len(spy.condenser_calls) == 1
    # The retry prompt is the condensed one.
    retry_prompt = spy.interrogator_calls[1]["prompt"]
    assert "COMPACT SUMMARY of older turns" in retry_prompt
    # The emitted record is tagged reactive.
    assert len(records) == 1
    assert records[0].reason == "reactive_overflow"


def test_overflow_after_proactive_condense_does_not_double_retry() -> None:
    # Proactive condensation already ran; a subsequent overflow must NOT trigger
    # a second condense pass (it cannot shrink the same set further).
    overflow = _Result(
        success=False,
        result_text="maximum context length exceeded",
        error_message="",
    )
    spy = _EngineSpy(interrogator_results=[overflow])
    config = condenser.CondenserConfig(keep_first=1, keep_last=3, trigger_turns=5)

    turn = _run(spy, _messages(30), condenser_config=config)

    # Honest failure surfaced (None), no second interrogator attempt.
    assert turn is None
    assert len(spy.interrogator_calls) == 1
    # Only the single proactive condensation ran.
    assert len(spy.condenser_calls) == 1


def test_intent_reads_real_last_user_turn_not_summary() -> None:
    # Even after condensation, the intent heuristic must read the genuine last
    # user turn. Build a turn JSON with no explicit intent so the heuristic runs.
    no_intent_json = (
        '{"reply": "Hello!", "draft": {}, "readiness": {"score": 0, "ready": false}, "done": false}'
    )
    spy = _EngineSpy(interrogator_results=[_Result(success=True, result_text=no_intent_json)])
    config = condenser.CondenserConfig(keep_first=1, keep_last=2, trigger_turns=5)
    convo = _messages(20)
    # Make the genuine last user turn a plain conversational opener.
    convo[-1] = cc.ConverseMessage(role="user", content="who are you")

    turn = _run(spy, convo, condenser_config=config)
    assert turn is not None
    # The heuristic saw "who are you" (a conversation opener), not the summary.
    assert turn.intent == cc.INTENT_CONVERSATION
