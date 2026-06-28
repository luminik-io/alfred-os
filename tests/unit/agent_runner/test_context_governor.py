from __future__ import annotations

from pathlib import Path


def test_govern_prompt_context_leaves_small_prompts_alone(fresh_agent_runner) -> None:
    ar = fresh_agent_runner

    text, stats = ar.govern_prompt_context(
        "small prompt",
        env={"ALFRED_CONTEXT_MAX_CHARS": "100"},
    )

    assert text == "small prompt"
    assert stats.applied is False
    assert stats.reason == "within_budget"
    assert stats.omitted_chars == 0


def test_govern_prompt_context_can_be_disabled(fresh_agent_runner) -> None:
    ar = fresh_agent_runner
    prompt = "a" * 200

    text, stats = ar.govern_prompt_context(
        prompt,
        env={"ALFRED_CONTEXT_GOVERNOR": "0", "ALFRED_CONTEXT_MAX_CHARS": "50"},
    )

    assert text == prompt
    assert stats.applied is False
    assert stats.reason == "disabled"


def test_blank_governor_env_keeps_default_enabled(fresh_agent_runner) -> None:
    ar = fresh_agent_runner
    prompt = "a" * 5_000

    text, stats = ar.govern_prompt_context(
        prompt,
        env={"ALFRED_CONTEXT_GOVERNOR": "", "ALFRED_CONTEXT_MAX_CHARS": "4096"},
    )

    assert stats.applied is True
    assert stats.reason == "over_budget"
    assert len(text) <= stats.max_chars


def test_govern_prompt_context_preserves_head_tail_and_marks_omission(
    fresh_agent_runner,
) -> None:
    ar = fresh_agent_runner
    prompt = "HEAD-" + ("middle-" * 2000) + "-TAIL"

    text, stats = ar.govern_prompt_context(
        prompt,
        env={
            "ALFRED_CONTEXT_MAX_CHARS": "4096",
            "ALFRED_CONTEXT_HEAD_CHARS": "1200",
            "ALFRED_CONTEXT_TAIL_CHARS": "1200",
        },
    )

    assert stats.applied is True
    assert stats.reason == "over_budget"
    assert len(text) <= stats.max_chars
    assert text.startswith("HEAD-")
    assert text.endswith("-TAIL")
    assert "ALFRED_CONTEXT_GOVERNOR" in text
    assert "omitted_chars=" in text
    assert stats.omitted_chars > 0


def test_govern_prompt_context_caps_utf8_bytes_for_argv_safety(
    fresh_agent_runner,
) -> None:
    ar = fresh_agent_runner
    prompt = "HEAD-" + ("🔥" * 40_000) + "-TAIL"

    text, stats = ar.govern_prompt_context(
        prompt,
        env={
            "ALFRED_CONTEXT_MAX_CHARS": "200000",
            "ALFRED_CONTEXT_MAX_BYTES": "200000",
            "ALFRED_CONTEXT_HEAD_CHARS": "100000",
            "ALFRED_CONTEXT_TAIL_CHARS": "100000",
        },
    )

    assert stats.applied is True
    assert stats.max_bytes == 96_000
    assert stats.final_bytes <= stats.max_bytes
    assert len(text.encode("utf-8")) <= 96_000
    assert text.startswith("HEAD-")
    assert text.endswith("-TAIL")


def test_invoke_agent_engine_uses_governed_prompt_and_stamps_result(
    fresh_agent_runner,
    monkeypatch,
) -> None:
    import agent_runner.process as proc

    monkeypatch.setenv("ALFRED_CONTEXT_MAX_CHARS", "4096")
    monkeypatch.setenv("ALFRED_CONTEXT_HEAD_CHARS", "1200")
    monkeypatch.setenv("ALFRED_CONTEXT_TAIL_CHARS", "1200")

    captured: dict[str, str] = {}

    def fake_claude(prompt, **kwargs):
        captured["prompt"] = prompt
        return proc.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="s",
            result_text="done",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    result, engine_used = proc.invoke_agent_engine(
        "start\n" + ("middle\n" * 3000) + "finish",
        engine="claude",
        agent="lucius",
        firing_id="fid-1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        claude_fn=fake_claude,
    )

    assert engine_used == "claude"
    assert result.success is True
    assert len(captured["prompt"]) <= 4096
    assert "ALFRED_CONTEXT_GOVERNOR" in captured["prompt"]
    assert result.raw["context_governor"]["applied"] is True
    assert (
        result.raw["context_governor"]["original_chars"]
        > result.raw["context_governor"]["final_chars"]
    )
