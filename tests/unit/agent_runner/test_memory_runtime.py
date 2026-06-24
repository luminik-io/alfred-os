from __future__ import annotations

import sys
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))


def test_parse_and_strip_memory_reflections() -> None:
    import agent_runner as ar

    text = f"""done
{ar.BEGIN_MARKER}
[
  {{"body": "Tests for API clients live next to the client.", "tags": ["tests"], "severity": "warning"}}
]
{ar.END_MARKER}
"""

    reflections = ar.parse_memory_reflections(text)

    assert len(reflections) == 1
    assert reflections[0].body == "Tests for API clients live next to the client."
    assert reflections[0].tags == ("tests",)
    assert reflections[0].severity == "warning"
    assert ar.BEGIN_MARKER not in ar.strip_memory_reflections(text)


def test_invoke_agent_engine_prepends_memory_and_records_reflection(monkeypatch) -> None:
    import agent_runner.process as proc
    from agent_runner.memory_runtime import BEGIN_MARKER, END_MARKER

    class Lesson:
        body = "GraphQL schema lives under src/schema.graphql."
        tags: ClassVar[list[str]] = ["graphql"]
        severity = "info"

    class Brain:
        def __init__(self) -> None:
            self.firings = []
            self.failures = []
            self.candidates = []

        def firing_log(self, **kwargs):
            self.firings.append(kwargs)

        def record_failure(self, **kwargs):
            self.failures.append(kwargs)

        def propose_memory(self, **kwargs):
            self.candidates.append(kwargs)
            return object()

    class Provider:
        name = "fleet"

        def __init__(self) -> None:
            self.brain = Brain()
            self.reflections = []

        def recall(self, **kwargs):
            return [Lesson()]

        def reflect(self, **kwargs):
            self.reflections.append(kwargs)
            return object()

    provider = Provider()
    monkeypatch.setattr(proc, "load_runtime_memory", lambda: provider)
    captured: dict[str, str] = {}

    def fake_claude(prompt, **kwargs):
        captured["prompt"] = prompt
        return proc.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="s",
            result_text=f"""done
{BEGIN_MARKER}
[{{"body": "Use the API fixture factory.", "tags": ["tests"]}}]
{END_MARKER}
""",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    result, engine_used = proc.invoke_agent_engine(
        "Implement the issue.",
        engine="claude",
        agent="lucius",
        firing_id="fid-1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        claude_fn=fake_claude,
        memory_repo="org/api",
    )

    assert engine_used == "claude"
    assert result.result_text == "done"
    assert "Alfred memory" in captured["prompt"]
    assert "GraphQL schema" in captured["prompt"]
    assert "ALFRED_MEMORY_REFLECTIONS_JSON" in captured["prompt"]
    assert provider.brain.candidates[0]["body"] == "Use the API fixture factory."
    assert provider.brain.candidates[0]["repo"] == "org/api"
    assert provider.brain.candidates[0]["source"] == "engine-reflection"
    assert provider.brain.firings[0]["firing_id"] == "fid-1"


def test_record_reflections_defaults_to_candidates(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    class Brain:
        def __init__(self) -> None:
            self.candidates = []

        def propose_memory(self, **kwargs):
            self.candidates.append(kwargs)

    class Provider:
        name = "fleet"

        def __init__(self) -> None:
            self.brain = Brain()

    provider = Provider()
    monkeypatch.delenv("ALFRED_MEMORY_REFLECTION_MODE", raising=False)
    written = runtime.record_reflections(
        provider,
        [runtime.MemoryReflection(body="Use fixture factory.", tags=("tests",))],
        codename="lucius",
        repo="org/api",
        firing_id="fid-1",
    )
    assert written == 1
    assert provider.brain.candidates[0]["source"] == "engine-reflection"
    assert provider.brain.candidates[0]["source_firing_id"] == "fid-1"


def test_record_reflections_can_write_direct_lessons(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    class Provider:
        name = "fleet"

        def __init__(self) -> None:
            self.reflections = []

        def reflect(self, **kwargs):
            self.reflections.append(kwargs)

    provider = Provider()
    monkeypatch.setenv("ALFRED_MEMORY_REFLECTION_MODE", "direct")
    written = runtime.record_reflections(
        provider,
        [runtime.MemoryReflection(body="Use fixture factory.", tags=("tests",))],
        codename="lucius",
        repo="org/api",
        firing_id="fid-1",
    )
    assert written == 1
    assert provider.reflections[0]["body"] == "Use fixture factory."


def test_record_firing_records_failure_memory() -> None:
    from agent_runner import memory_runtime as runtime
    from agent_runner.result import ClaudeResult

    class Brain:
        def __init__(self) -> None:
            self.firings = []
            self.failures = []

        def firing_log(self, **kwargs):
            self.firings.append(kwargs)

        def record_failure(self, **kwargs):
            self.failures.append(kwargs)

    class Provider:
        name = "fleet"

        def __init__(self) -> None:
            self.brain = Brain()

    provider = Provider()
    result = ClaudeResult(
        success=False,
        subtype="error_timeout",
        num_turns=0,
        cost_usd=0,
        session_id=None,
        result_text="",
        raw={},
        stop_reason=None,
        error_message="timeout",
    )
    assert runtime.record_firing(
        provider,
        codename="huntress",
        repo="org/web",
        firing_id="fid-2",
        result=result,
        engine_used="claude",
    )
    assert provider.brain.firings[0]["status"] == "partial"
    assert provider.brain.failures[0]["subtype"] == "error_timeout"
    assert provider.brain.failures[0]["engine"] == "claude"


def test_invoke_agent_engine_strips_malformed_memory_block(monkeypatch) -> None:
    import agent_runner.process as proc
    from agent_runner.memory_runtime import BEGIN_MARKER, END_MARKER

    class Provider:
        name = "fleet"

        def recall(self, **kwargs):
            return []

        def reflect(self, **kwargs):
            raise AssertionError("malformed memory blocks should not be reflected")

    monkeypatch.setattr(proc, "load_runtime_memory", lambda: Provider())

    def fake_claude(prompt, **kwargs):
        return proc.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="s",
            result_text=f"done\n{BEGIN_MARKER}\nnot-json\n{END_MARKER}\n",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    result, _engine_used = proc.invoke_agent_engine(
        "Implement the issue.",
        engine="claude",
        agent="lucius",
        firing_id="fid-1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        claude_fn=fake_claude,
        memory_repo="org/api",
    )

    assert result.result_text == "done"


class _Scored:
    """Minimal scored-capable provider stub for gating tests."""

    name = "redis"

    def __init__(self, pairs) -> None:
        self._pairs = pairs

    def recall_scored(self, *, codename, repo, query=None, limit=5):
        return list(self._pairs)

    def recall(self, *, codename, repo, query=None, limit=5):
        return [lesson for lesson, _ in self._pairs]


class _LessonStub:
    def __init__(self, body, severity="info", tags=None) -> None:
        self.body = body
        self.severity = severity
        self.tags = tags or []


def test_format_memory_context_drops_below_threshold(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    provider = _Scored(
        [
            (_LessonStub("Strongly relevant lesson."), 0.92),
            (_LessonStub("Weakly relevant lesson."), 0.20),
        ]
    )
    monkeypatch.setenv("ALFRED_MEMORY_RECALL_THRESHOLD", "0.5")
    out = runtime.format_memory_context(provider, codename="lucius", repo="org/api")
    assert "Strongly relevant lesson." in out
    assert "Weakly relevant lesson." not in out


def test_format_memory_context_dedupes_bodies(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    provider = _Scored(
        [
            (_LessonStub("Use the API fixture factory."), 0.9),
            (_LessonStub("use the   API fixture factory."), 0.9),
        ]
    )
    monkeypatch.delenv("ALFRED_MEMORY_RECALL_THRESHOLD", raising=False)
    out = runtime.format_memory_context(provider, codename="lucius", repo="org/api")
    assert out.count("fixture factory") == 1


def test_format_memory_context_keeps_unscored_lessons(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    # No score reported (None) must never be dropped by the threshold.
    provider = _Scored([(_LessonStub("Unscored but important."), None)])
    monkeypatch.setenv("ALFRED_MEMORY_RECALL_THRESHOLD", "0.99")
    out = runtime.format_memory_context(provider, codename="lucius", repo="org/api")
    assert "Unscored but important." in out


def test_format_memory_context_default_threshold_injects_all(monkeypatch) -> None:
    from agent_runner import memory_runtime as runtime

    provider = _Scored(
        [
            (_LessonStub("Lesson A."), 0.05),
            (_LessonStub("Lesson B."), 0.5),
        ]
    )
    monkeypatch.delenv("ALFRED_MEMORY_RECALL_THRESHOLD", raising=False)
    out = runtime.format_memory_context(provider, codename="lucius", repo="org/api")
    # Default threshold 0.0 preserves existing inject-everything behavior.
    assert "Lesson A." in out and "Lesson B." in out


def test_format_memory_context_falls_back_to_plain_recall() -> None:
    from agent_runner import memory_runtime as runtime

    class PlainProvider:
        name = "fleet"

        def recall(self, *, codename, repo, query=None, limit=5):
            return [_LessonStub("Plain recall lesson.")]

    out = runtime.format_memory_context(PlainProvider(), codename="lucius", repo="org/api")
    assert "Plain recall lesson." in out
