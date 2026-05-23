"""Tests for bin/alfred-metrics.py and lib/metrics.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import metrics  # noqa: E402

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load_alfred_metrics():
    """Load bin/alfred-metrics.py as a module (file has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "alfred_metrics_cli", str(REPO_ROOT / "bin" / "alfred-metrics.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_spend(state_dir: Path, codename: str, day: str, **kw) -> Path:
    path = state_dir / codename / f"spend-{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "firings_today": kw.get("firings", 0),
        "successes_today": kw.get("successes", 0),
        "failures_today": kw.get("failures", 0),
        "turns_today": kw.get("turns", 0),
        "cost_usd_today": kw.get("cost", 0.0),
    }
    path.write_text(json.dumps(payload))
    return path


def _write_transcript(
    state_dir: Path, codename: str, firing_id: str, tools: list[tuple[str, dict]]
) -> Path:
    month = datetime.now(UTC).strftime("%Y-%m")
    path = state_dir / "transcripts" / codename / month / f"{firing_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": name, "input": inp} for name, inp in tools]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "num_turns": 3,
            "total_cost_usd": 0.05,
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _write_codex(state_dir: Path, codename: str, firing_id: str, tokens: int) -> Path:
    month = datetime.now(UTC).strftime("%Y-%m")
    path = state_dir / "codex" / codename / month / f"{firing_id}.stdout.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"session id: {firing_id}\n\ntokens used\n{tokens}\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# parse_since
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("7", 7),
        ("7d", 7),
        ("48h", 2),
        ("2w", 14),
        ("1m", 30),
        (None, 7),
        (3, 3),
    ],
)
def test_parse_since(value, expected):
    assert metrics.parse_since(value, default_days=7) == expected


def test_parse_since_falls_back_on_garbage():
    assert metrics.parse_since("garbage", default_days=14) == 14


# --------------------------------------------------------------------------
# discover_codenames
# --------------------------------------------------------------------------


def test_discover_codenames_dedupes_and_skips_reserved(tmp_path: Path):
    _write_spend(tmp_path, "lucius", "2026-05-22", firings=1)
    _write_transcript(tmp_path, "drake", "D1", [("Read", {"file_path": "/x"})])
    _write_codex(tmp_path, "bane", "C1", 500)
    # Reserved dirs that should be ignored.
    (tmp_path / "transcripts").mkdir(exist_ok=True)
    (tmp_path / "engines").mkdir(exist_ok=True)
    assert metrics.discover_codenames(tmp_path) == ["bane", "drake", "lucius"]


def test_discover_codenames_empty(tmp_path: Path):
    assert metrics.discover_codenames(tmp_path) == []


# --------------------------------------------------------------------------
# agent_metric / fleet_metrics
# --------------------------------------------------------------------------


def test_agent_metric_full_rollup(tmp_path: Path):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=3, successes=2, failures=1, turns=12, cost=1.50)
    _write_transcript(
        tmp_path,
        "lucius",
        "L1",
        [("Read", {"file_path": "/a"}), ("Edit", {"file_path": "/a"}), ("Bash", {"command": "ls"})],
    )
    _write_codex(tmp_path, "lucius", "L1", 2500)

    m = metrics.agent_metric(tmp_path, "lucius", days=7)
    assert m.spend.firings == 3
    assert m.spend.successes == 2
    assert m.spend.turns == 12
    assert m.spend.cost_usd == pytest.approx(1.50)
    assert m.tool_calls_total == 3
    assert m.tool_calls == {"Read": 1, "Edit": 1, "Bash": 1}
    assert m.files_read == 1
    assert m.files_edited == 1
    assert m.bash_commands == 1
    assert m.codex_runs == 1
    assert m.codex_tokens == 2500


def test_agent_metric_empty_state(tmp_path: Path):
    m = metrics.agent_metric(tmp_path, "nobody", days=7)
    assert m.is_empty()
    assert m.spend.firings == 0


def test_fleet_metrics_multi_agent(tmp_path: Path):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=1, successes=1)
    _write_spend(tmp_path, "drake", today, firings=2, successes=1, failures=1)
    report = metrics.fleet_metrics(tmp_path, days=7)
    by_name = {m.codename: m for m in report.metrics}
    assert set(by_name) == {"drake", "lucius"}
    assert by_name["drake"].spend.firings == 2


# --------------------------------------------------------------------------
# CLI invocation
# --------------------------------------------------------------------------


def test_cli_table_output_smoke(tmp_path: Path, capsys):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=2, successes=2, turns=8, cost=0.42)
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path), "--since", "7d"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alfred-metrics" in out
    assert "lucius" in out
    assert "TOTAL" in out
    assert "fleet success rate" in out


def test_cli_json_output_round_trip(tmp_path: Path, capsys):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=1, successes=1, cost=0.10)
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["days"] == 7
    assert payload["metrics"][0]["codename"] == "lucius"
    assert payload["metrics"][0]["spend"]["firings"] == 1


def test_cli_missing_state_dir_exit_2(tmp_path: Path, capsys):
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path / "missing")])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_unknown_codename_exit_1(tmp_path: Path, capsys):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=1, successes=1)
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path), "--codename", "ghost"])
    assert rc == 1
    assert "ghost" in capsys.readouterr().err


def test_cli_by_day(tmp_path: Path, capsys):
    today = datetime.now().strftime("%Y-%m-%d")
    _write_spend(tmp_path, "lucius", today, firings=2, successes=2)
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path), "--by-day"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--by-day" in out
    assert today in out


def test_cli_empty_window_renders_zero_rows(tmp_path: Path, capsys):
    cli = _load_alfred_metrics()
    rc = cli.main(["--state-dir", str(tmp_path)])
    assert rc == 0
    assert "no firings or transcripts in window" in capsys.readouterr().out
