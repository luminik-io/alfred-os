"""Honest token-usage reporting for subscription (no-API-key) operation.

Under a Max/Pro subscription the ``claude -p`` ``total_cost_usd`` figure is
list-price noise (and ``$0`` for Codex). The real local signal is the per-result
token usage from the result envelope. These tests cover the plumbing that
threads those tokens through:

* ``ClaudeResult`` carries ``tokens_in`` / ``tokens_out`` /
  ``cache_creation_tokens`` / ``cache_read_tokens`` parsed from ``raw.usage``.
* ``SpendState.record_result`` persists the per-day token counters plus an
  ``engine_breakdown`` rollup, while keeping ``cost_usd_today`` for back-compat.
* ``metrics.agent_metric`` sums the new per-day token keys.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


# --------------------------------------------------------------------------- #
# ClaudeResult token fields
# --------------------------------------------------------------------------- #


def test_build_claude_result_populates_token_fields():
    from agent_runner.result import _build_claude_result

    raw = {
        "subtype": "success",
        "stop_reason": "end_turn",
        "num_turns": 7,
        "total_cost_usd": 11.94,
        "session_id": "abc",
        "result": "done",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 9000,
        },
    }
    result = _build_claude_result(raw)

    assert result.success is True
    assert result.tokens_in == 1200
    assert result.tokens_out == 340
    assert result.cache_creation_tokens == 50
    assert result.cache_read_tokens == 9000


def test_build_claude_result_tolerates_missing_usage():
    from agent_runner.result import _build_claude_result

    result = _build_claude_result({"subtype": "success", "stop_reason": "end_turn"})

    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.cache_creation_tokens == 0
    assert result.cache_read_tokens == 0


def test_build_claude_result_tolerates_garbage_usage():
    from agent_runner.result import _build_claude_result

    raw = {
        "subtype": "success",
        "stop_reason": "end_turn",
        # Non-dict usage and a negative / non-numeric field must not raise.
        "usage": {"input_tokens": "not-a-number", "output_tokens": -5},
    }
    result = _build_claude_result(raw)
    assert result.tokens_in == 0
    assert result.tokens_out == 0


def test_claude_result_token_fields_default_to_zero():
    from agent_runner.result import ClaudeResult

    result = ClaudeResult(
        success=True,
        subtype="success",
        num_turns=1,
        cost_usd=0.0,
        session_id=None,
        result_text="",
        raw={},
    )
    assert result.tokens_in == 0
    assert result.tokens_out == 0
    assert result.cache_creation_tokens == 0
    assert result.cache_read_tokens == 0


# --------------------------------------------------------------------------- #
# SpendState token counters + engine_breakdown
# --------------------------------------------------------------------------- #


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    from agent_runner import paths as agent_runner_paths
    from agent_runner import state as agent_runner_state

    monkeypatch.setattr(agent_runner_paths, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(agent_runner_state, "STATE_ROOT", tmp_path)
    return tmp_path


def _result(**kwargs):
    from agent_runner.result import ClaudeResult

    base = {
        "success": True,
        "subtype": "success",
        "num_turns": 5,
        "cost_usd": 11.94,
        "session_id": "s",
        "result_text": "",
        "raw": {},
        "tokens_in": 1000,
        "tokens_out": 200,
        "cache_creation_tokens": 10,
        "cache_read_tokens": 90,
    }
    base.update(kwargs)
    return ClaudeResult(**base)


def test_spend_state_defaults_new_token_counters(isolated_state):
    from agent_runner.state import SpendState

    spend = SpendState("lucius")
    assert spend.state["tokens_in_today"] == 0
    assert spend.state["tokens_out_today"] == 0
    assert spend.state["cache_tokens_today"] == 0
    assert spend.state["engine_breakdown"] == {}
    # Back-compat counter still present.
    assert spend.state["cost_usd_today"] == 0.0


def test_record_result_accumulates_tokens_and_legacy_counters(isolated_state):
    from agent_runner.state import SpendState

    spend = SpendState("lucius")
    spend.record_result(_result(), engine="claude")
    spend.record_result(_result(num_turns=3, cost_usd=0.0), engine="claude")

    reread = SpendState("lucius")
    assert reread.state["firings_today"] == 2
    assert reread.state["turns_today"] == 8
    assert reread.state["cost_usd_today"] == pytest.approx(11.94)
    assert reread.state["tokens_in_today"] == 2000
    assert reread.state["tokens_out_today"] == 400
    # cache_creation + cache_read summed per firing: (10 + 90) * 2.
    assert reread.state["cache_tokens_today"] == 200


def test_record_result_builds_per_engine_breakdown(isolated_state):
    from agent_runner.state import SpendState

    spend = SpendState("lucius")
    spend.record_result(_result(), engine="claude")
    spend.record_result(
        _result(
            tokens_in=0,
            tokens_out=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            num_turns=1,
        ),
        engine="codex",
    )

    breakdown = SpendState("lucius").state["engine_breakdown"]
    assert set(breakdown) == {"claude", "codex"}
    assert breakdown["claude"]["firings"] == 1
    assert breakdown["claude"]["tokens_in"] == 1000
    assert breakdown["claude"]["turns"] == 5
    assert breakdown["codex"]["firings"] == 1
    assert breakdown["codex"]["tokens_in"] == 0


def test_record_result_without_engine_skips_breakdown(isolated_state):
    from agent_runner.state import SpendState

    spend = SpendState("lucius")
    spend.record_result(_result())
    reread = SpendState("lucius")
    assert reread.state["engine_breakdown"] == {}
    # Token counters still recorded even with no engine label.
    assert reread.state["tokens_in_today"] == 1000


def test_record_result_tolerates_pre_token_result_object(isolated_state):
    """A duck-typed result object missing token attrs must default to 0."""
    from agent_runner.state import SpendState

    class LegacyResult:
        num_turns = 4
        cost_usd = 2.5

    spend = SpendState("lucius")
    spend.record_result(LegacyResult(), engine="claude")
    reread = SpendState("lucius")
    assert reread.state["turns_today"] == 4
    assert reread.state["tokens_in_today"] == 0
    assert reread.state["engine_breakdown"]["claude"]["tokens_in"] == 0


# --------------------------------------------------------------------------- #
# metrics.agent_metric token rollup
# --------------------------------------------------------------------------- #


def test_agent_metric_sums_token_counters(tmp_path):
    import metrics

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    agent_dir = tmp_path / "lucius"
    agent_dir.mkdir(parents=True)
    (agent_dir / f"spend-{today}.json").write_text(
        json.dumps(
            {
                "firings_today": 2,
                "turns_today": 8,
                "cost_usd_today": 11.94,
                "tokens_in_today": 2000,
                "tokens_out_today": 400,
                "cache_tokens_today": 200,
            }
        )
    )

    metric = metrics.agent_metric(tmp_path, "lucius", days=1)
    assert metric.spend.firings == 2
    assert metric.spend.turns == 8
    assert metric.spend.cost_usd == pytest.approx(11.94)
    assert metric.spend.tokens_in == 2000
    assert metric.spend.tokens_out == 400
    assert metric.spend.cache_tokens == 200


def test_agent_metric_defaults_tokens_to_zero_on_legacy_spend_file(tmp_path):
    """A spend file written before token counters existed rolls up as 0."""
    import metrics

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    agent_dir = tmp_path / "bane"
    agent_dir.mkdir(parents=True)
    (agent_dir / f"spend-{today}.json").write_text(
        json.dumps({"firings_today": 1, "turns_today": 3, "cost_usd_today": 1.0})
    )

    metric = metrics.agent_metric(tmp_path, "bane", days=1)
    assert metric.spend.firings == 1
    assert metric.spend.tokens_in == 0
    assert metric.spend.tokens_out == 0
    assert metric.spend.cache_tokens == 0
