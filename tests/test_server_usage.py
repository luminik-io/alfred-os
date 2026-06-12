"""Tests for the native subscription-usage rollup (``server.usage``) and the
``GET /api/usage`` endpoint.

The reader scans local CLI logs (Claude Code transcripts + Codex rollouts).
These tests seed ``.jsonl`` files in a tmp dir and point the reader's env
overrides at it, so they never touch the operator's real ``~/.claude`` /
``~/.codex`` and are deterministic anywhere CI runs.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from fastapi.testclient import TestClient  # noqa: E402
from server import FilesystemReader, create_app  # noqa: E402
from server import usage as usage_module  # noqa: E402

# A fixed "now" inside the seeded active block window (14:00-19:00) so the
# reset countdown and projection are stable.
_NOW = datetime(2026, 6, 3, 17, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


def _claude_event(
    *,
    ts: str,
    msg_id: str,
    request_id: str,
    model: str = "claude-opus-4-8",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> dict[str, Any]:
    """One Claude Code assistant transcript line carrying ``message.usage``."""
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": request_id,
        "uuid": msg_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _seed_claude(projects_dir: Path) -> None:
    """Seed two transcript files in the active 14:00-19:00 window (one event in
    an earlier, now-closed window) so block math and multi-file aggregation are
    both exercised."""
    proj_a = projects_dir / "-Users-op-projA"
    proj_b = projects_dir / "-Users-op-projB"

    # An earlier, closed window (08:xx) that must NOT be the active block.
    _write_jsonl(
        proj_a / "old-session.jsonl",
        [
            _claude_event(
                ts="2026-06-03T08:30:00.000Z",
                msg_id="old-1",
                request_id="req_old_1",
                model="claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
            )
        ],
    )

    # Active window file A: opens the 14:00 block.
    _write_jsonl(
        proj_a / "active-a.jsonl",
        [
            _claude_event(
                ts="2026-06-03T14:10:00.000Z",
                msg_id="a-1",
                request_id="req_a_1",
                model="claude-opus-4-8",
                input_tokens=1000,
                output_tokens=2000,
                cache_creation=3000,
                cache_read=4000,
            ),
            # A zero-token synthetic turn that must be ignored entirely.
            _claude_event(
                ts="2026-06-03T14:11:00.000Z",
                msg_id="a-synthetic",
                request_id="req_a_syn",
                model="<synthetic>",
            ),
        ],
    )

    # Active window file B: more usage in the same 14:00 block + a duplicate of
    # a-1 (same id+requestId) that must be deduped, not double-counted.
    _write_jsonl(
        proj_b / "active-b.jsonl",
        [
            _claude_event(
                ts="2026-06-03T16:40:00.000Z",
                msg_id="b-1",
                request_id="req_b_1",
                model="claude-sonnet-4-6",
                input_tokens=500,
                output_tokens=600,
                cache_creation=700,
                cache_read=800,
            ),
            _claude_event(
                ts="2026-06-03T14:10:00.000Z",
                msg_id="a-1",
                request_id="req_a_1",
                model="claude-opus-4-8",
                input_tokens=1000,
                output_tokens=2000,
                cache_creation=3000,
                cache_read=4000,
            ),
        ],
    )


def _codex_token_count(
    *,
    ts: str,
    last_total: int,
    last_input: int,
    last_output: int,
    cum_total: int | None = None,
    cum_input: int | None = None,
    cum_output: int | None = None,
    rate_limits: dict[str, Any] | None = None,
):
    """One Codex rollout ``token_count`` event.

    ``last_*`` is the per-turn delta; ``cum_*`` is the cumulative
    ``total_token_usage`` the real CLI writes (monotonically increasing across
    the session). When ``cum_*`` is omitted it mirrors the delta so legacy
    delta-only fixtures still parse. ``rate_limits`` rides on the payload when
    provided.
    """
    payload: dict[str, Any] = {
        "type": "token_count",
        "info": {
            "last_token_usage": {
                "input_tokens": last_input,
                "output_tokens": last_output,
                "total_tokens": last_total,
            },
            "total_token_usage": {
                "input_tokens": cum_input if cum_input is not None else last_input,
                "output_tokens": cum_output if cum_output is not None else last_output,
                "total_tokens": cum_total if cum_total is not None else last_total,
            },
        },
    }
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"timestamp": ts, "type": "event_msg", "payload": payload}


def _seed_codex(sessions_dir: Path) -> None:
    """Seed a Codex rollout spanning two days so the latest-day bucket and the
    all-time total are both exercised. ``total_token_usage`` is cumulative, as
    the real CLI writes it, so the latest-day contribution is derived from the
    cumulative session total (replay-over-count safe) rather than summed deltas.
    """
    _write_jsonl(
        sessions_dir / "2026" / "06" / "rollout-x.jsonl",
        [
            {"type": "session_meta", "timestamp": "2026-06-02T10:00:00.000Z"},
            _codex_token_count(
                ts="2026-06-02T10:05:00.000Z",
                last_total=1000,
                last_input=800,
                last_output=200,
                cum_total=1000,
                cum_input=800,
                cum_output=200,
            ),
            # Two turns on 2026-06-03 -> the latest day. Cumulative climbs to
            # 8500; minus the 1000 prior-day boundary -> 7500 for the day.
            _codex_token_count(
                ts="2026-06-03T09:00:00.000Z",
                last_total=5000,
                last_input=4000,
                last_output=1000,
                cum_total=6000,
                cum_input=4800,
                cum_output=1200,
            ),
            _codex_token_count(
                ts="2026-06-03T09:30:00.000Z",
                last_total=2500,
                last_input=2000,
                last_output=500,
                cum_total=8500,
                cum_input=6800,
                cum_output=1700,
            ),
        ],
    )


@pytest.fixture
def limits_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "usage-limits.json"
    monkeypatch.setenv("ALFRED_CLAUDE_USAGE_LIMITS_FILE", str(path))
    return path


@pytest.fixture(autouse=True)
def _no_real_usage_limit_cache(limits_file: Path) -> None:
    """Keep tests from reading the operator's real ~/.claude cache."""
    assert not limits_file.exists()


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the reader at a tmp dir seeded with both log shapes."""
    claude_dir = tmp_path / "claude-projects"
    codex_dir = tmp_path / "codex-sessions"
    _seed_claude(claude_dir)
    _seed_codex(codex_dir)
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(claude_dir))
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))
    return tmp_path


# --------------------------------------------------------------------------- #
# build_usage
# --------------------------------------------------------------------------- #


def test_build_usage_parses_active_block_and_codex(seeded: Path) -> None:
    payload = usage_module.build_usage(now=_NOW)

    assert payload["available"] is True
    assert payload["kind"] == "subscription"
    assert payload["source"] == "native"
    # We never fabricate a subscription quota.
    assert payload["weekly"]["available"] is False
    assert payload["weekly"]["utilization"] is None
    assert payload["weekly"]["remaining_percent"] is None
    assert payload["limits"] is None

    block = payload["block"]
    assert block is not None
    assert block["is_active"] is True
    # a-1 (1000+2000+3000+4000=10000) + b-1 (500+600+700+800=2600). The
    # synthetic zero-token turn and the duplicate a-1 are excluded.
    assert block["total_tokens"] == 12600
    assert block["entries"] == 2
    assert block["token_counts"] == {
        "input": 1500,
        "output": 2600,
        "cache_creation": 3700,
        "cache_read": 4800,
    }
    # Window floors the first event (14:10) to the hour -> 14:00; reset +5h.
    assert block["start_at"] == "2026-06-03T14:00:00Z"
    assert block["reset_at"] == "2026-06-03T19:00:00Z"
    # 17:00 -> 19:00 is 120 minutes to reset.
    assert block["minutes_to_reset"] == 120
    # Subscription usage has no meaningful per-token dollar cost.
    assert block["cost_usd"] is None
    assert block["models"] == ["claude-opus-4-8", "claude-sonnet-4-6"]

    five_hour = payload["five_hour"]
    assert five_hour["available"] is True
    assert five_hour["source"] == "claude_transcripts"
    assert five_hour["total_tokens"] == 12600
    assert five_hour["reset_at"] == "2026-06-03T19:00:00Z"
    assert five_hour["minutes_to_reset"] == 120
    assert five_hour["utilization"] is None
    assert five_hour["remaining_percent"] is None


def test_build_usage_projection_extrapolates_current_pace(seeded: Path) -> None:
    block = usage_module.build_usage(now=_NOW)["block"]
    assert block is not None
    proj = block["projection"]
    # 12600 tokens over 180 elapsed minutes (14:00 -> 17:00) = 70 tok/min;
    # extrapolated to the full 5h (300 min) = 21000.
    assert block["burn_rate"]["tokens_per_minute"] == 70.0
    assert proj["total_tokens"] == 21000
    assert proj["remaining_minutes"] == 120
    # No cost under a subscription.
    assert proj["total_cost_usd"] is None
    assert block["burn_rate"]["cost_per_hour"] is None


def test_build_usage_codex_latest_day(seeded: Path) -> None:
    codex = usage_module.build_usage(now=_NOW)["codex"]
    assert codex is not None
    # 2026-06-03 is the latest day. Its contribution is the session's final
    # cumulative total (8500) minus the prior-day boundary (1000) = 7500.
    assert codex["latest_day"]["date"] == "2026-06-03"
    assert codex["latest_day"]["total_tokens"] == 7500
    assert codex["latest_day"]["input_tokens"] == 6000
    assert codex["latest_day"]["output_tokens"] == 1500
    assert codex["latest_day"]["cost_usd"] is None
    # The endpoint deliberately avoids all-time totals so it does not parse old
    # multi-gigabyte Codex sessions on every dashboard refresh.
    assert codex["totals"]["total_tokens"] is None
    assert codex["totals"]["cost_usd"] is None
    # No rate_limits in the base fixture -> no quota key (honest empty state).
    assert "quota" not in codex


# --------------------------------------------------------------------------- #
# Codex rate_limits quota + replay-over-count dedupe
# --------------------------------------------------------------------------- #


def _rate_limits(
    *,
    primary_pct: float,
    primary_reset: str,
    secondary_pct: float,
    secondary_reset: str,
    plan_type: str = "pro",
) -> dict[str, Any]:
    return {
        "primary": {"used_percent": primary_pct, "resets_at": primary_reset},
        "secondary": {"used_percent": secondary_pct, "resets_at": secondary_reset},
        "plan_type": plan_type,
    }


def test_build_usage_codex_surfaces_rate_limits_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The codex builder exposes the LAST rate_limits payload as codex.quota."""
    codex_dir = tmp_path / "codex-sessions"
    _write_jsonl(
        codex_dir / "2026" / "06" / "rollout-q.jsonl",
        [
            _codex_token_count(
                ts="2026-06-03T09:00:00.000Z",
                last_total=5000,
                last_input=4000,
                last_output=1000,
                cum_total=5000,
                cum_input=4000,
                cum_output=1000,
                rate_limits=_rate_limits(
                    primary_pct=10.0,
                    primary_reset="2026-06-03T13:00:00Z",
                    secondary_pct=40.0,
                    secondary_reset="2026-06-08T09:00:00Z",
                ),
            ),
            # A later event -> its rate_limits wins (newest timestamp).
            _codex_token_count(
                ts="2026-06-03T09:30:00.000Z",
                last_total=2500,
                last_input=2000,
                last_output=500,
                cum_total=7500,
                cum_input=6000,
                cum_output=1500,
                rate_limits=_rate_limits(
                    primary_pct=22.5,
                    primary_reset="2026-06-03T14:00:00Z",
                    secondary_pct=41.0,
                    secondary_reset="2026-06-08T09:00:00Z",
                ),
            ),
        ],
    )
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude"))

    codex = usage_module.build_usage(now=_NOW)["codex"]
    assert codex is not None
    quota = codex["quota"]
    assert quota["plan_type"] == "pro"
    # The later event's percentages win.
    assert quota["primary"]["used_percent"] == 22.5
    assert quota["primary"]["resets_at"] == "2026-06-03T14:00:00Z"
    assert quota["secondary"]["used_percent"] == 41.0
    assert quota["secondary"]["resets_at"] == "2026-06-08T09:00:00Z"


def test_build_usage_codex_dedupes_subagent_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ccusage issue 950: a replayed last_token_usage delta must not double-count.

    The session writes the SAME per-turn delta twice (subagent replay) but the
    cumulative total_token_usage only advances once. Deriving the day from the
    cumulative total yields the true 7500, not a doubled 12500.
    """
    codex_dir = tmp_path / "codex-sessions"
    _write_jsonl(
        codex_dir / "2026" / "06" / "rollout-replay.jsonl",
        [
            _codex_token_count(
                ts="2026-06-03T09:00:00.000Z",
                last_total=5000,
                last_input=4000,
                last_output=1000,
                cum_total=5000,
                cum_input=4000,
                cum_output=1000,
            ),
            # Replayed identical delta; cumulative does NOT advance.
            _codex_token_count(
                ts="2026-06-03T09:00:00.000Z",
                last_total=5000,
                last_input=4000,
                last_output=1000,
                cum_total=5000,
                cum_input=4000,
                cum_output=1000,
            ),
            _codex_token_count(
                ts="2026-06-03T09:30:00.000Z",
                last_total=2500,
                last_input=2000,
                last_output=500,
                cum_total=7500,
                cum_input=6000,
                cum_output=1500,
            ),
        ],
    )
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude"))

    codex = usage_module.build_usage(now=_NOW)["codex"]
    assert codex is not None
    # Cumulative-derived: final 7500 - prior-day boundary 0 = 7500 (not 12500).
    assert codex["latest_day"]["total_tokens"] == 7500
    assert codex["latest_day"]["input_tokens"] == 6000
    assert codex["latest_day"]["output_tokens"] == 1500


def test_build_usage_codex_delta_only_session_resets_on_day_rollover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delta-only session spanning midnight must not leak yesterday into today.

    Legacy sessions without total_token_usage fall back to summing deltas.
    When the day advances, the old day's accumulated deltas must reset, or
    the latest-day bucket over-reports by yesterday's tokens.
    """

    def _delta_only(ts: str, total: int, inp: int, out: int) -> dict:
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "total_tokens": total,
                    }
                },
            },
        }

    codex_dir = tmp_path / "codex-sessions"
    _write_jsonl(
        codex_dir / "2026" / "06" / "rollout-midnight.jsonl",
        [
            _delta_only("2026-06-02T23:50:00.000Z", 4000, 3000, 1000),
            _delta_only("2026-06-03T00:10:00.000Z", 1500, 1000, 500),
        ],
    )
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude"))

    codex = usage_module.build_usage(now=_NOW)["codex"]
    assert codex is not None
    # Only the post-midnight delta counts toward the latest day.
    assert codex["latest_day"]["total_tokens"] == 1500
    assert codex["latest_day"]["input_tokens"] == 1000
    assert codex["latest_day"]["output_tokens"] == 500


def test_build_usage_codex_quota_absent_when_no_rate_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_dir = tmp_path / "codex-sessions"
    _seed_codex(codex_dir)
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude"))

    codex = usage_module.build_usage(now=_NOW)["codex"]
    assert codex is not None
    assert "quota" not in codex


def test_build_usage_reads_cached_real_5h_and_weekly_limits(
    seeded: Path,
    limits_file: Path,
) -> None:
    limits_file.write_text(
        json.dumps(
            {
                "five_hour": {
                    "utilization": 35.0,
                    "resets_at": "2026-06-03T19:00:00+00:00",
                },
                "seven_day": {
                    "utilization": 14.5,
                    "resets_at": "2026-06-08T09:30:00+00:00",
                },
                "seven_day_sonnet": {
                    "utilization": 39.0,
                    "resets_at": "2026-06-05T12:00:00+00:00",
                },
                "extra_usage": {
                    "is_enabled": True,
                    "monthly_limit": 100000,
                    "used_credits": 0,
                    "utilization": None,
                },
            }
        ),
        encoding="utf-8",
    )

    payload = usage_module.build_usage(now=_NOW)

    limits = payload["limits"]
    assert limits["source"] == "claude_usage_limits_cache"
    assert limits["five_hour"]["utilization"] == 35.0
    assert limits["five_hour"]["remaining_percent"] == 65.0
    assert limits["five_hour"]["minutes_to_reset"] == 120
    assert limits["seven_day"]["remaining_percent"] == 85.5
    assert limits["extra_usage"]["is_enabled"] is True
    five_hour = payload["five_hour"]
    assert five_hour["available"] is True
    assert five_hour["source"] == "claude_transcripts+claude_usage_limits_cache"
    assert five_hour["total_tokens"] == 12600
    assert five_hour["utilization"] == 35.0
    assert five_hour["remaining_percent"] == 65.0
    assert five_hour["quota_reset_at"] == "2026-06-03T19:00:00+00:00"
    assert five_hour["quota_minutes_to_reset"] == 120

    weekly = payload["weekly"]
    assert weekly["available"] is True
    assert weekly["aggregate_available"] is True
    assert weekly["total_tokens"] is None
    assert weekly["cost_usd"] is None
    assert weekly["utilization"] == 14.5
    assert weekly["remaining_percent"] == 85.5
    assert weekly["resets_at"] == "2026-06-08T09:30:00+00:00"
    assert weekly["minutes_to_reset"] == 6750
    assert weekly["source"] == "claude_usage_limits_cache"
    assert weekly["model_windows"]["sonnet"]["utilization"] == 39.0
    assert weekly["model_windows"]["opus"] is None
    assert weekly["unavailable_reason"] is None


def test_build_usage_no_active_block_when_now_outside_window(seeded: Path) -> None:
    # 21:00 is past the 14:00-19:00 window's reset, and no later events exist.
    payload = usage_module.build_usage(now=datetime(2026, 6, 3, 21, 0, 0, tzinfo=UTC))
    assert payload["available"] is True
    assert payload["block"] is None
    assert payload["five_hour"]["available"] is False
    # Codex still resolved, so the rollup is usable and not an error.
    assert payload["codex"] is not None
    assert "error" not in payload


def test_build_usage_no_logs_degrades_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point both env overrides at empty / nonexistent dirs.
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "no-claude-here"))
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(tmp_path / "no-codex-here"))
    payload = usage_module.build_usage(now=_NOW)
    # Empty (not unreadable) logs are a real, available "no usage" state, not an
    # error: both reads succeed and simply find nothing.
    assert payload["available"] is True
    assert payload["block"] is None
    assert payload["codex"] is None
    assert payload["five_hour"]["available"] is False
    assert payload["weekly"]["available"] is False
    assert "error" not in payload


def test_build_usage_malformed_lines_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_dir = tmp_path / "claude-projects"
    proj = claude_dir / "-Users-op-projA"
    proj.mkdir(parents=True)
    good = json.dumps(
        _claude_event(
            ts="2026-06-03T14:10:00.000Z",
            msg_id="g-1",
            request_id="req_g_1",
            model="claude-opus-4-8",
            input_tokens=10,
            output_tokens=20,
        )
    )
    # A corrupt line and an empty line sit between two valid events; the reader
    # must skip them and still count the good ones.
    another_good = json.dumps(
        _claude_event(
            ts="2026-06-03T14:20:00.000Z",
            msg_id="g-2",
            request_id="req_g_2",
            model="claude-opus-4-8",
            input_tokens=5,
            output_tokens=5,
        )
    )
    (proj / "session.jsonl").write_text(
        f"{good}\nnot json {{{{\n\n{another_good}\n", encoding="utf-8"
    )
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(claude_dir))
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(tmp_path / "no-codex"))

    payload = usage_module.build_usage(now=_NOW)
    assert payload["available"] is True
    block = payload["block"]
    assert block is not None
    # 10+20 + 5+5 = 40, from the two valid events only.
    assert block["total_tokens"] == 40
    assert block["entries"] == 2
    assert payload["codex"] is None


def test_build_usage_keeps_codex_when_claude_dir_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude side raises (the projects path is a file, not a dir -> the glob
    # under it fails), Codex resolves. The whole rollup stays available and the
    # Claude miss is reported as a per-source error.
    bad_claude = tmp_path / "claude-is-a-file"
    bad_claude.write_text("not a directory", encoding="utf-8")
    codex_dir = tmp_path / "codex-sessions"
    _seed_codex(codex_dir)
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(bad_claude))
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(codex_dir))

    payload = usage_module.build_usage(now=_NOW)
    # A path that is a file is treated by ``os.path.isdir`` as "no dir" -> the
    # Claude reader simply yields nothing (available, no block), Codex resolves.
    assert payload["available"] is True
    assert payload["block"] is None
    assert payload["five_hour"]["available"] is False
    assert payload["codex"] is not None


def test_build_usage_never_raises_on_reader_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the Claude reader to blow up; build_usage must swallow it into a
    # per-source error and still surface the Codex side as available.
    def boom() -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(usage_module, "_build_claude_block", lambda *, now: boom())
    monkeypatch.setattr(usage_module, "_build_codex", lambda: {"latest_day": None, "totals": None})
    payload = usage_module.build_usage(now=_NOW)
    assert payload["available"] is True
    assert payload["block"] is None
    assert payload["errors"]["block"]
    assert "disk on fire" in payload["errors"]["block"]


def test_build_usage_unavailable_when_both_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(usage_module, "_build_claude_block", lambda *, now: boom())
    monkeypatch.setattr(usage_module, "_build_codex", lambda: boom())
    payload = usage_module.build_usage(now=_NOW)
    assert payload["available"] is False
    assert payload["block"] is None
    assert payload["codex"] is None
    assert payload["five_hour"]["available"] is False
    assert payload["weekly"]["available"] is False
    assert payload["error"]
    assert "kaboom" in payload["error"]


# --------------------------------------------------------------------------- #
# GET /api/usage endpoint
# --------------------------------------------------------------------------- #


def test_api_usage_endpoint_returns_parsed_usage(
    seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    real_build_usage = usage_module.build_usage
    monkeypatch.setattr(
        usage_module,
        "build_usage",
        lambda: real_build_usage(now=_NOW),
    )
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/usage")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["block"]["total_tokens"] == 12600
    assert payload["five_hour"]["total_tokens"] == 12600
    assert payload["codex"]["latest_day"]["date"] == "2026-06-03"


def test_api_usage_endpoint_degrades_when_logs_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No logs anywhere: the endpoint still returns 200 with an honest shape.
    monkeypatch.setenv("ALFRED_CLAUDE_PROJECTS_DIR", str(tmp_path / "nope-claude"))
    monkeypatch.setenv("ALFRED_CODEX_SESSIONS_DIR", str(tmp_path / "nope-codex"))
    state = tmp_path / "state"
    state.mkdir()
    client = TestClient(create_app(FilesystemReader(state_root=state)))

    response = client.get("/api/usage")
    assert response.status_code == 200
    payload = response.json()
    # Empty logs are a valid "available, but no active block / no codex" state.
    assert payload["available"] is True
    assert payload["block"] is None
    assert payload["codex"] is None
    assert payload["five_hour"]["available"] is False
    assert payload["weekly"]["available"] is False
