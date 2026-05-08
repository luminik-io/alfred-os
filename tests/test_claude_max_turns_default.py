"""Guard the no-cap-by-default policy at the CLI invocation layer.

Background — Claude Code's ``-p`` (non-interactive) mode applies a hidden
40-turn default when ``--max-turns`` is omitted. That default is far too
tight for our agents — Lucius routinely needs 60-150 turns on cross-file
work, Drake's healthy planning runs hit 60+. ``claude_invoke`` always
passes an explicit ``--max-turns``: caller's value if given, otherwise
``_CLAUDE_UNLIMITED_TURNS`` (999). The wall-clock ``timeout`` is the
only real ceiling.

These tests pin two invariants:

1. When the caller passes ``max_turns=None``, the constructed argv MUST
   contain ``--max-turns <high number>`` so the CLI's hidden 40-turn
   default cannot kick in.
2. When the caller passes a finite cap, the wrapper forwards that exact
   value (no silent override or clamp at this layer).

We don't actually invoke Claude — we monkey-patch ``run`` to capture the
argv and short-circuit before the subprocess starts.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(_LIB))

import agent_runner  # noqa: E402


def _stub_completed_process(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    """Cheap CompletedProcess look-alike that satisfies claude_invoke's reads."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_unlimited_turns_constant_is_high_enough_to_be_effectively_unlimited() -> None:
    """We picked 999. Don't accidentally lower it without realising — it
    must stay well clear of the longest legitimate planning run we have
    observed (Lucius routinely needs 60-150 turns on cross-file work)."""
    assert agent_runner._CLAUDE_UNLIMITED_TURNS >= 200, (
        "_CLAUDE_UNLIMITED_TURNS should stay well above the longest "
        "observed planner run. 999 was chosen as 'effectively unlimited' "
        "for any normal agent firing — bounded by wall-clock timeout."
    )


def test_claude_invoke_passes_unlimited_when_max_turns_is_none() -> None:
    """The bug we're guarding against is that ``-p`` defaults to 40 when
    ``--max-turns`` is omitted. Even when the caller passes None, the
    wrapper must emit ``--max-turns <_CLAUDE_UNLIMITED_TURNS>`` so the
    CLI never falls back to its hidden 40-turn default."""
    captured: dict = {}

    def fake_run(cmd, *, cwd=None, timeout=60, capture=True, env=None, **kwargs):
        captured["cmd"] = list(cmd)
        # Return a minimal CompletedProcess-shaped object so the parsing
        # code in claude_invoke gets through without raising.
        return _stub_completed_process(
            returncode=0,
            stdout='{"type":"result","subtype":"success","is_error":false,'
            '"stop_reason":"end_turn","num_turns":1,"total_cost_usd":0,'
            '"result":""}',
        )

    with mock.patch.object(agent_runner, "run", fake_run):
        agent_runner.claude_invoke(
            prompt="hi",
            workdir=Path("/tmp"),
            allowed_tools="Read",
            max_turns=None,
            timeout=10,
        )

    cmd = captured["cmd"]
    assert "--max-turns" in cmd, (
        f"--max-turns flag must be passed even when max_turns=None; got {cmd}"
    )
    flag_idx = cmd.index("--max-turns")
    assert cmd[flag_idx + 1] == str(agent_runner._CLAUDE_UNLIMITED_TURNS), (
        f"max_turns=None should map to {agent_runner._CLAUDE_UNLIMITED_TURNS}, "
        f"got {cmd[flag_idx + 1]}"
    )


def test_claude_invoke_forwards_explicit_max_turns_unchanged() -> None:
    """When the operator opts into a specific cap (debug knob), the
    wrapper must forward that exact value — no silent override to the
    'unlimited' default."""
    captured: dict = {}

    def fake_run(cmd, *, cwd=None, timeout=60, capture=True, env=None, **kwargs):
        captured["cmd"] = list(cmd)
        return _stub_completed_process(
            returncode=0,
            stdout='{"type":"result","subtype":"success","is_error":false,'
            '"stop_reason":"end_turn","num_turns":1,"total_cost_usd":0,'
            '"result":""}',
        )

    with mock.patch.object(agent_runner, "run", fake_run):
        agent_runner.claude_invoke(
            prompt="hi",
            workdir=Path("/tmp"),
            allowed_tools="Read",
            max_turns=77,
            timeout=10,
        )

    cmd = captured["cmd"]
    flag_idx = cmd.index("--max-turns")
    assert cmd[flag_idx + 1] == "77", (
        f"explicit max_turns=77 must pass through unchanged, got {cmd[flag_idx + 1]}"
    )


def test_optional_env_int_returns_none_when_unset(monkeypatch) -> None:
    """The default-no-cap path: env var unset → None → claude_invoke
    layer maps to _CLAUDE_UNLIMITED_TURNS."""
    monkeypatch.delenv("ALFRED_LUCIUS_MAX_TURNS", raising=False)
    assert agent_runner.optional_env_int("ALFRED_LUCIUS_MAX_TURNS", minimum=40) is None


def test_optional_env_int_clamps_to_floor(monkeypatch) -> None:
    """The optional knob clamps to the documented floor when set, so a
    typo can't drive the cap below an agent's sensible floor."""
    monkeypatch.setenv("ALFRED_LUCIUS_MAX_TURNS", "5")
    assert agent_runner.optional_env_int("ALFRED_LUCIUS_MAX_TURNS", minimum=40) == 40


def test_optional_env_int_returns_none_on_garbage(monkeypatch) -> None:
    """Unparseable values fall back to None, just like unset."""
    monkeypatch.setenv("ALFRED_LUCIUS_MAX_TURNS", "not-an-int")
    assert agent_runner.optional_env_int("ALFRED_LUCIUS_MAX_TURNS", minimum=40) is None


def test_env_int_uses_default_when_unset(monkeypatch) -> None:
    """``env_int`` is the variant with a finite default; missing or bad
    values fall back to ``default`` and are still range-clamped."""
    monkeypatch.delenv("ALFRED_LUCIUS_TURN_CAP", raising=False)
    assert agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=5000, minimum=100) == 5000


def test_env_int_clamps_in_range(monkeypatch) -> None:
    """Range clamping protects against typos pushing the value out of
    a documented sensible range."""
    monkeypatch.setenv("ALFRED_LUCIUS_TURN_CAP", "9999")
    assert (
        agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=5000, minimum=100, maximum=8000)
        == 8000
    )

    monkeypatch.setenv("ALFRED_LUCIUS_TURN_CAP", "1")
    assert (
        agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=5000, minimum=100, maximum=8000)
        == 100
    )


def test_env_int_clamps_out_of_range_default(monkeypatch) -> None:
    """An out-of-range ``default`` must be clamped too — the safety
    guarantee is unconditional. Codex P2 review (PR #13): the unset /
    unparseable paths previously returned ``default`` as-is, defeating
    the clamp the docstring promises.
    """
    monkeypatch.delenv("ALFRED_LUCIUS_TURN_CAP", raising=False)
    # Default above maximum: must be clamped to maximum.
    assert (
        agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=99999, minimum=100, maximum=8000)
        == 8000
    )
    # Default below minimum: must be clamped to minimum.
    assert (
        agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=1, minimum=100, maximum=8000) == 100
    )

    # Same protection on the unparseable path.
    monkeypatch.setenv("ALFRED_LUCIUS_TURN_CAP", "garbage")
    assert (
        agent_runner.env_int("ALFRED_LUCIUS_TURN_CAP", default=99999, minimum=100, maximum=8000)
        == 8000
    )
