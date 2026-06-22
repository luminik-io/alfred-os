"""Real subscription-usage rollup from Claude Code's own local logs.

This module powers ``GET /api/usage``. It reports the operator's REAL
subscription headroom for the current Claude 5-hour block (and a Codex row),
not the API list-price of tokens. Under a Max/Pro subscription the per-token
dollar figure is meaningless (and is ``$0`` for Codex), so the dashboard's old
"$ spend" tile was misleading.

This is intentionally an in-process stdlib reader: no subprocess, no npm
dependency, and no version drift. It reads the same local CLI session logs the
engines write.

Data sources (both written by the local CLIs, JSONL, append-only):

* Claude Code session transcripts at ``~/.claude/projects/**/*.jsonl``. Each
  assistant turn is a line whose ``message.usage`` object carries
  ``input_tokens`` / ``output_tokens`` / ``cache_creation_input_tokens`` /
  ``cache_read_input_tokens`` and a top-level ISO-8601 ``timestamp``. We dedupe
  on ``message.id``+``requestId`` (the same event can reappear in a resumed
  transcript).
* Codex session rollouts at ``~/.codex/sessions/**/*.jsonl`` (and
  ``~/.codex/archived_sessions/``). Token usage rides on ``event_msg`` lines
  whose ``payload.type == "token_count"``; ``info.last_token_usage`` is the
  per-turn delta. The panel only needs the current Codex row, so we scan recent
  files until the latest UTC day is complete instead of parsing gigabytes of old
  sessions on every refresh.

Shape returned by :func:`build_usage`:

* ``five_hour`` - explicit current-window availability for Claude usage. Local
  transcripts provide token totals and reset timing; Claude's usage-limit cache,
  when present, adds true quota utilization and remaining percent.
* ``block`` - the active Claude 5-hour rolling window: total tokens used,
  ``reset_at`` (ISO-8601, when the window rolls over), ``minutes_to_reset``, a
  simple burn ``projection`` (current-pace tokens extrapolated to the full 5h),
  a ``burn_rate``, and the models seen this block. ``null`` when no block is
  active.
* ``codex`` - the most recent day's Codex usage, or ``null`` when Codex usage is
  unavailable. ``totals`` keeps the response shape but carries ``null`` because
  all-time totals are intentionally not computed per request.
* ``available`` - ``False`` with an ``error`` string when neither source can be
  read. The dashboard then shows a plain "usage unavailable" state instead of
  crashing.

We deliberately do NOT fabricate a weekly subscription cap or "headroom
percent": the plan's hidden quota is not published, so any percentage would be
invented. ``weekly`` is an explicit unavailable object unless Claude's local
usage-limit cache carries true seven-day quota data.

The 5-hour-block math uses rolling 5h windows: a new window opens on the first
event after a >5h gap or once an event falls beyond ``window_start + 5h``. The
window start is floored to the top of the hour (UTC). The ACTIVE window is the
one whose ``start + 5h`` is still in the future relative to ``now``.
``reset_at = window_start + 5h``; the burn projection extrapolates the current
token rate across the full 5h.

Pure stdlib. Reads degrade to an honest empty shape on any failure (missing
logs, unreadable files, malformed lines) and never raise into the request
handler.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from glob import iglob
from typing import Any

# Rolling-window length for subscription usage blocks.
_BLOCK_HOURS = 5
_BLOCK_DURATION = timedelta(hours=_BLOCK_HOURS)

# Log roots. Overridable via env so tests can point at a tmp dir, and so an
# operator with a non-default home can redirect them.
_CLAUDE_PROJECTS_ENV = "ALFRED_CLAUDE_PROJECTS_DIR"
_CODEX_SESSIONS_ENV = "ALFRED_CODEX_SESSIONS_DIR"
_CLAUDE_USAGE_LIMITS_ENV = "ALFRED_CLAUDE_USAGE_LIMITS_FILE"

# Models that are not real engine calls (synthetic / error placeholder turns
# carry zero tokens but a "<synthetic>" model). We skip them so they neither
# pollute the model list nor open spurious blocks.
_SYNTHETIC_MODELS = frozenset({"<synthetic>"})

# Scan ceilings so hosts with thousands of historical transcripts never make
# the endpoint slow. Claude reads newest-first and stops once files are too old
# to affect the active 5h block. Codex reads newest-first and stops after the
# latest UTC day is complete; the UI does not need all-time totals.
_MAX_CLAUDE_FILES = 400
_MAX_CODEX_FILES = 200


def _claude_projects_dir() -> str:
    override = (os.environ.get(_CLAUDE_PROJECTS_ENV) or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _codex_session_dirs() -> list[str]:
    override = (os.environ.get(_CODEX_SESSIONS_ENV) or "").strip()
    if override:
        return [override]
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".codex", "sessions"),
        os.path.join(home, ".codex", "archived_sessions"),
    ]


def _claude_usage_limits_file() -> str:
    override = (os.environ.get(_CLAUDE_USAGE_LIMITS_ENV) or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "usage-limits.json")


def build_usage(*, now: datetime | None = None) -> dict[str, Any]:
    """Assemble the subscription-usage rollup from local logs. Never raises.

    ``now`` is injectable so the reset-countdown and active-block math are
    deterministic in tests.
    """
    moment = now or datetime.now(UTC)

    block, block_error = _safe(lambda: _build_claude_block(now=moment))
    codex, codex_error = _safe(_build_codex)
    limits, limits_error = _safe(lambda: _build_claude_limits(now=moment))

    # The rollup is "unavailable" only when BOTH primary sources failed to read
    # at all. A working Claude side with a Codex miss (or vice versa) is still
    # useful, so we keep the half that resolved and note the other as a
    # per-source error. A successful read that simply finds no active block is
    # NOT an error (block is None, no error string). A readable real-quota cache
    # is also useful on its own, but an absent cache is the common no-data state.
    available = block_error is None or codex_error is None or limits is not None
    five_hour = _five_hour_from_sources(block, limits)
    weekly = _weekly_from_limits(limits)
    payload: dict[str, Any] = {
        "available": available,
        "kind": "subscription",
        "source": "native",
        "generated_at": moment.isoformat(),
        "five_hour": five_hour,
        "block": block,
        "codex": codex,
        "limits": limits,
        # Weekly headroom would require the plan's hidden quota; we never invent
        # one. If the Claude usage-limit cache has real seven-day utilization,
        # surface that as the weekly quota view; otherwise report unavailable.
        "weekly": weekly,
    }
    # Always record per-source read failures, even when the rollup is fully
    # unavailable. The provider-normalized view (and any other consumer) needs to
    # tell "all readers errored" (permission/disk failure under ~/.claude and
    # ~/.codex) apart from "no logs are present yet". Without this map an all-fail
    # state would carry only a top-level ``error`` and the provider projection
    # would misreport broken local state as merely absent logs.
    errors: dict[str, str] = {}
    if block_error:
        errors["block"] = block_error
    if codex_error:
        errors["codex"] = codex_error
    if limits_error:
        errors["limits"] = limits_error
    if errors:
        payload["errors"] = errors
    if not available:
        payload["error"] = block_error or codex_error or limits_error or "usage logs unavailable"
    return payload


def unavailable_usage_payload(
    error: str | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Build the honest unavailable shape used by endpoint-level fallbacks."""
    moment = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "available": False,
        "kind": "subscription",
        "source": "native",
        "generated_at": moment.isoformat(),
        "five_hour": _five_hour_from_sources(None, None),
        "block": None,
        "codex": None,
        "limits": None,
        "weekly": _weekly_from_limits(None),
    }
    if error:
        payload["error"] = error
    return payload


# --------------------------------------------------------------------------- #
# Provider-normalized view (``alfred usage`` + ``GET /api/usage/providers``)
# --------------------------------------------------------------------------- #
#
# ``build_usage`` returns the dashboard's rich shape (a single Claude 5-hour
# block, a quota cache, a Codex latest-day row). The provider view below
# re-projects that same data, with no additional file reads, into a flat
# ``{"claude": {...}, "codex": {...}}`` contract keyed by provider. Each
# provider carries an explicit ``available`` flag plus a ``five_hour`` and a
# ``weekly`` window normalized to the same keys, so a consumer can render both
# engines uniformly and degrade gracefully (``available: false``) when a
# provider's local state cannot be read. Nothing here invents numbers: every
# field is either copied from the local logs/cache or left ``None``.


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a dict, else an empty dict (type-narrowing)."""
    return value if isinstance(value, dict) else {}


def _minutes_to_reset(reset_at: Any, *, now: datetime) -> int | None:
    """Minutes from ``now`` until ``reset_at`` (ISO-8601), or ``None`` if absent.

    Codex persists only ``resets_at`` per window, so we derive the countdown
    here rather than leaving it blank. A reset already in the past clamps to 0.
    """
    parsed = _parse_iso(reset_at)
    if parsed is None:
        return None
    return _minutes_until(parsed, now=now)


def build_provider_usage(*, now: datetime | None = None) -> dict[str, Any]:
    """Normalize local usage into ``{"claude": {...}, "codex": {...}}``.

    Pure re-projection of :func:`build_usage` (no extra I/O). Each provider has:

    * ``available`` - ``True`` only when that provider's local state could be
      read at all. A provider whose logs are absent/unreadable is
      ``available: false`` with an ``unavailable_reason``; we never fabricate
      usage for it.
    * ``five_hour`` / ``weekly`` - the rolling 5-hour and weekly windows, each
      with ``used_percent`` / ``remaining_percent`` / ``reset_at`` /
      ``minutes_to_reset`` (any of which may be ``None`` when the local CLI does
      not persist that figure) plus provider-specific extras (Claude token
      totals, Codex plan type).

    ``now`` is injectable for deterministic reset-countdown math in tests.
    """
    moment = now or datetime.now(UTC)
    base = build_usage(now=moment)
    errors = _as_dict(base.get("errors"))

    claude = _provider_claude(base, errors)
    codex = _provider_codex(base, errors, now=moment)
    return {
        "available": bool(claude.get("available") or codex.get("available")),
        "generated_at": moment.isoformat(),
        "claude": claude,
        "codex": codex,
    }


def _provider_window(
    *,
    used_percent: Any = None,
    remaining_percent: Any = None,
    reset_at: Any = None,
    minutes_to_reset: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One normalized rolling window. Absent figures stay ``None`` (no guesses)."""
    window: dict[str, Any] = {
        "used_percent": used_percent if isinstance(used_percent, (int, float)) else None,
        "remaining_percent": (
            remaining_percent if isinstance(remaining_percent, (int, float)) else None
        ),
        "reset_at": reset_at if isinstance(reset_at, str) else None,
        "minutes_to_reset": (minutes_to_reset if isinstance(minutes_to_reset, int) else None),
    }
    if extra:
        window.update(extra)
    return window


def _provider_claude(base: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    """Project the Claude side of ``build_usage`` into a provider entry.

    Claude is "available" when transcripts produced an active block OR the local
    usage-limit cache was readable. When neither is present (and neither raised),
    the provider is an honest unavailable shape.
    """
    five = _as_dict(base.get("five_hour"))
    weekly = _as_dict(base.get("weekly"))
    block = base.get("block") if isinstance(base.get("block"), dict) else None
    limits = base.get("limits") if isinstance(base.get("limits"), dict) else None

    read_error = errors.get("block") or errors.get("limits")
    available = bool(five.get("available")) or bool(weekly.get("available"))

    if not available:
        reason = (
            f"Claude local usage could not be read: {read_error}"
            if read_error
            else (
                five.get("unavailable_reason")
                or "No Claude 5-hour block or usage-limit cache was found in "
                "local logs (~/.claude)."
            )
        )
        return {
            "available": False,
            "five_hour": _provider_window(),
            "weekly": _provider_window(),
            "unavailable_reason": reason,
        }

    five_hour = _provider_window(
        used_percent=five.get("utilization"),
        remaining_percent=five.get("remaining_percent"),
        # Prefer the true quota reset (cache); fall back to the block reset.
        reset_at=five.get("quota_reset_at") or five.get("reset_at"),
        minutes_to_reset=(
            five.get("quota_minutes_to_reset")
            if isinstance(five.get("quota_minutes_to_reset"), int)
            else five.get("minutes_to_reset")
        ),
        extra={
            "total_tokens": five.get("total_tokens"),
            "source": five.get("source"),
        },
    )
    weekly_window = _provider_window(
        used_percent=weekly.get("utilization"),
        remaining_percent=weekly.get("remaining_percent"),
        reset_at=weekly.get("resets_at"),
        minutes_to_reset=weekly.get("minutes_to_reset"),
        extra={"source": weekly.get("source")},
    )
    if not weekly.get("available"):
        weekly_window["available"] = False
        weekly_window["unavailable_reason"] = weekly.get("unavailable_reason")

    return {
        "available": True,
        "five_hour": five_hour,
        "weekly": weekly_window,
        "block_active": bool(block.get("is_active")) if block else False,
        "usage_limit_cache": limits is not None,
    }


def _provider_codex(
    base: dict[str, Any], errors: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Project the Codex side of ``build_usage`` into a provider entry.

    Codex is "available" when its latest-day rollout could be read. Its 5-hour
    (primary) and weekly (secondary) windows come from the CLI's own
    ``rate_limits`` payload when present; Codex emits ``used_percent`` but not a
    remaining figure, so ``remaining_percent`` is derived as ``100 - used`` only
    when ``used`` is known.
    """
    codex = base.get("codex") if isinstance(base.get("codex"), dict) else None
    read_error = errors.get("codex")

    if codex is None:
        reason = (
            f"Codex local usage could not be read: {read_error}"
            if read_error
            else "No Codex sessions were found in local logs (~/.codex)."
        )
        return {
            "available": False,
            "five_hour": _provider_window(),
            "weekly": _provider_window(),
            "unavailable_reason": reason,
        }

    quota = _as_dict(codex.get("quota"))
    primary = quota.get("primary") if isinstance(quota.get("primary"), dict) else None
    secondary = quota.get("secondary") if isinstance(quota.get("secondary"), dict) else None
    latest_day = codex.get("latest_day") if isinstance(codex.get("latest_day"), dict) else None

    primary_reset = primary.get("resets_at") if primary else None
    secondary_reset = secondary.get("resets_at") if secondary else None
    five_hour = _provider_window(
        used_percent=primary.get("used_percent") if primary else None,
        remaining_percent=_remaining_from_used(primary.get("used_percent") if primary else None),
        reset_at=primary_reset,
        minutes_to_reset=_minutes_to_reset(primary_reset, now=now),
        extra={
            "latest_day_tokens": (latest_day.get("total_tokens") if latest_day else None),
        },
    )
    weekly_window = _provider_window(
        used_percent=secondary.get("used_percent") if secondary else None,
        remaining_percent=_remaining_from_used(
            secondary.get("used_percent") if secondary else None
        ),
        reset_at=secondary_reset,
        minutes_to_reset=_minutes_to_reset(secondary_reset, now=now),
    )

    return {
        "available": True,
        "five_hour": five_hour,
        "weekly": weekly_window,
        "plan_type": quota.get("plan_type") if quota else None,
        "quota_available": bool(quota),
    }


def _remaining_from_used(used: Any) -> float | None:
    """Codex reports ``used_percent`` only; derive remaining when used is known."""
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return round(max(0.0, 100.0 - float(used)), 2)


def _safe(fn) -> tuple[Any | None, str | None]:
    """Run a reader, returning ``(result, None)`` or ``(None, error_string)``.

    Any exception is turned into a short error string so a single bad log file
    can never break the endpoint.
    """
    try:
        return fn(), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Claude real quota cache
# --------------------------------------------------------------------------- #


def _build_claude_limits(*, now: datetime) -> dict[str, Any] | None:
    """Read Claude's cached real quota utilization, if available.

    ``ccusage`` documents ``~/.claude/usage-limits.json`` as the local cache for
    Anthropic's OAuth usage endpoint. When present, it carries actual 5h and
    7d utilization percentages plus reset times. When absent, we return None
    rather than guessing from local tokens.
    """
    path = _claude_usage_limits_file()
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        return None
    data = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
    updated_at = _iso_z(datetime.fromtimestamp(_safe_mtime(path), tz=UTC))
    return {
        "source": "claude_usage_limits_cache",
        "path": path,
        "updated_at": updated_at,
        "five_hour": _limit_bucket(data.get("five_hour"), now=now),
        "seven_day": _limit_bucket(data.get("seven_day"), now=now),
        "seven_day_sonnet": _limit_bucket(data.get("seven_day_sonnet"), now=now),
        "seven_day_opus": _limit_bucket(data.get("seven_day_opus"), now=now),
        "extra_usage": _extra_usage(data.get("extra_usage")),
    }


def _limit_bucket(value: Any, *, now: datetime) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    utilization = _float(value.get("utilization"))
    resets_at = value.get("resets_at")
    reset_dt = _parse_iso(resets_at)
    return {
        "utilization": utilization,
        "remaining_percent": (
            round(max(0.0, 100.0 - utilization), 2) if utilization is not None else None
        ),
        "resets_at": resets_at if isinstance(resets_at, str) else None,
        "minutes_to_reset": _minutes_until(reset_dt, now=now) if reset_dt else None,
    }


def _extra_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "is_enabled": bool(value.get("is_enabled")),
        "monthly_limit": _float(value.get("monthly_limit")),
        "used_credits": _float(value.get("used_credits")),
        "utilization": _float(value.get("utilization")),
    }


def _five_hour_from_sources(block: Any, limits: Any) -> dict[str, Any]:
    """Build the client-facing 5-hour window from transcripts plus real quota.

    Claude transcripts provide true local token totals, but not the hidden plan
    cap. The usage-limit cache, when present, provides true quota utilization.
    This object keeps those facts separate so the UI can show the useful parts
    without inventing headroom.
    """
    block_data = block if isinstance(block, dict) else None
    limit_data = None
    if isinstance(limits, dict) and isinstance(limits.get("five_hour"), dict):
        limit_data = limits["five_hour"]

    if block_data is None and limit_data is None:
        return {
            "available": False,
            "source": None,
            "start_at": None,
            "reset_at": None,
            "minutes_to_reset": None,
            "total_tokens": None,
            "cost_usd": None,
            "token_counts": None,
            "entries": None,
            "models": [],
            "projection": None,
            "burn_rate": None,
            "utilization": None,
            "remaining_percent": None,
            "quota_reset_at": None,
            "quota_minutes_to_reset": None,
            "unavailable_reason": (
                "No active Claude 5-hour block or cached 5-hour quota was found in local logs."
            ),
        }

    sources: list[str] = []
    if block_data is not None:
        sources.append("claude_transcripts")
    if limit_data is not None:
        sources.append(
            str(limits.get("source") or "claude_usage_limits_cache")
            if isinstance(limits, dict)
            else "claude_usage_limits_cache"
        )

    return {
        "available": True,
        "source": "+".join(sources),
        "start_at": block_data.get("start_at") if block_data else None,
        "reset_at": (
            block_data.get("reset_at")
            if block_data
            else limit_data.get("resets_at")
            if limit_data
            else None
        ),
        "minutes_to_reset": (
            block_data.get("minutes_to_reset")
            if block_data
            else limit_data.get("minutes_to_reset")
            if limit_data
            else None
        ),
        "total_tokens": block_data.get("total_tokens") if block_data else None,
        "cost_usd": block_data.get("cost_usd") if block_data else None,
        "token_counts": block_data.get("token_counts") if block_data else None,
        "entries": block_data.get("entries") if block_data else None,
        "models": block_data.get("models", []) if block_data else [],
        "projection": block_data.get("projection") if block_data else None,
        "burn_rate": block_data.get("burn_rate") if block_data else None,
        "utilization": limit_data.get("utilization") if limit_data else None,
        "remaining_percent": (limit_data.get("remaining_percent") if limit_data else None),
        "quota_reset_at": limit_data.get("resets_at") if limit_data else None,
        "quota_minutes_to_reset": (limit_data.get("minutes_to_reset") if limit_data else None),
        "unavailable_reason": None,
    }


def _weekly_from_limits(limits: Any) -> dict[str, Any]:
    if not isinstance(limits, dict):
        return _weekly_unavailable("True weekly Claude quota is unavailable from local logs.")

    seven_day = limits.get("seven_day") if isinstance(limits.get("seven_day"), dict) else None
    sonnet = (
        limits.get("seven_day_sonnet") if isinstance(limits.get("seven_day_sonnet"), dict) else None
    )
    opus = limits.get("seven_day_opus") if isinstance(limits.get("seven_day_opus"), dict) else None
    if seven_day is None and sonnet is None and opus is None:
        return _weekly_unavailable(
            "True weekly Claude quota is unavailable from local logs.",
            source=limits.get("source"),
        )

    return {
        "available": True,
        "total_tokens": None,
        "cost_usd": None,
        "utilization": seven_day.get("utilization") if seven_day else None,
        "remaining_percent": (seven_day.get("remaining_percent") if seven_day else None),
        "resets_at": seven_day.get("resets_at") if seven_day else None,
        "minutes_to_reset": seven_day.get("minutes_to_reset") if seven_day else None,
        "source": limits.get("source"),
        "aggregate_available": seven_day is not None,
        "model_windows": {
            "sonnet": sonnet,
            "opus": opus,
        },
        "unavailable_reason": None,
    }


def _weekly_unavailable(reason: str, *, source: Any = None) -> dict[str, Any]:
    return {
        "available": False,
        "total_tokens": None,
        "cost_usd": None,
        "utilization": None,
        "remaining_percent": None,
        "resets_at": None,
        "minutes_to_reset": None,
        "source": source if isinstance(source, str) else None,
        "aggregate_available": False,
        "model_windows": {
            "sonnet": None,
            "opus": None,
        },
        "unavailable_reason": reason,
    }


# --------------------------------------------------------------------------- #
# Claude 5-hour block
# --------------------------------------------------------------------------- #


def _build_claude_block(*, now: datetime) -> dict[str, Any] | None:
    """Read Claude Code transcripts and return the active 5-hour block, or None.

    Returns ``None`` (not an error) when there are no logs or no window that is
    still active relative to ``now``.
    """
    events = list(_iter_claude_events(now=now))
    if not events:
        return None
    events.sort(key=lambda e: e["ts"])

    block = _active_block(events, now=now)
    if block is None:
        return None

    start = block["start"]
    reset_at = start + _BLOCK_DURATION
    total_tokens = block["total_tokens"]

    # Burn projection: extrapolate the current token rate across the full 5h.
    # The rate is tokens-so-far divided by elapsed wall time since the floored
    # window start, capped at the window length so a window that has run its
    # full 5h projects to exactly what was used. This is our own simple
    # estimate at the current pace.
    elapsed = now - start
    elapsed_min = elapsed.total_seconds() / 60.0
    elapsed_min = min(max(elapsed_min, 0.0), _BLOCK_HOURS * 60.0)
    tokens_per_minute = (total_tokens / elapsed_min) if elapsed_min > 0 else None
    projected_total = (
        round(tokens_per_minute * _BLOCK_HOURS * 60.0)
        if tokens_per_minute is not None
        else total_tokens
    )
    remaining = reset_at - now
    remaining_min = max(int(remaining.total_seconds() // 60), 0)

    projection = {
        "total_tokens": projected_total,
        # Subscription usage has no meaningful per-token cost, so cost stays
        # null. The shape is preserved for the client.
        "total_cost_usd": None,
        "remaining_minutes": remaining_min,
    }
    burn_rate = {
        "tokens_per_minute": (
            round(tokens_per_minute, 4) if tokens_per_minute is not None else None
        ),
        # No cost under a subscription.
        "cost_per_hour": None,
    }

    counts = block["counts"]
    return {
        "start_at": _iso_z(start),
        "reset_at": _iso_z(reset_at),
        "minutes_to_reset": _minutes_until(reset_at, now=now),
        "is_active": True,
        "total_tokens": total_tokens,
        # No meaningful dollar cost under a subscription.
        "cost_usd": None,
        "entries": block["entries"],
        "token_counts": {
            "input": counts["input"],
            "output": counts["output"],
            "cache_creation": counts["cache_creation"],
            "cache_read": counts["cache_read"],
        },
        "projection": projection,
        "burn_rate": burn_rate,
        "models": sorted(block["models"]),
    }


def _active_block(events: list[dict[str, Any]], *, now: datetime) -> dict[str, Any] | None:
    """Group sorted events into rolling 5h windows; return the active one.

    A new window opens on the first event, on the first event more than 5h
    after the previous event, or on the first event at/after the current
    window's ``start + 5h``. The window start is floored to the top of the hour
    (UTC). Because a new window only opens at/after the prior
    window's ``start + 5h`` (or after a >5h gap), the windows' ``[start, start +
    5h)`` intervals are disjoint, so at most one window contains ``now`` and
    that window is the active block. In production ``now`` is the real clock and
    this is simply the final window being filled right now.
    """
    current: dict[str, Any] | None = None
    last_ts: datetime | None = None
    active: dict[str, Any] | None = None

    for ev in events:
        ts = ev["ts"]
        start_new = (
            current is None
            or last_ts is None
            or (ts - last_ts) >= _BLOCK_DURATION
            or ts >= current["start"] + _BLOCK_DURATION
        )
        if start_new:
            current = {
                "start": _floor_hour(ts),
                "total_tokens": 0,
                "entries": 0,
                "counts": {
                    "input": 0,
                    "output": 0,
                    "cache_creation": 0,
                    "cache_read": 0,
                },
                "models": set(),
            }
            # Capture the window the moment it is opened if it contains ``now``;
            # intervals are disjoint so this can only match one window.
            if _window_is_active(current, now=now):
                active = current
        assert current is not None
        _accumulate(current, ev)
        last_ts = ts

    return active


def _window_is_active(window: dict[str, Any], *, now: datetime) -> bool:
    """A window is active when ``now`` falls within ``[start, start + 5h)``."""
    start = window["start"]
    return start <= now < start + _BLOCK_DURATION


def _accumulate(window: dict[str, Any], ev: dict[str, Any]) -> None:
    window["entries"] += 1
    c = window["counts"]
    c["input"] += ev["input"]
    c["output"] += ev["output"]
    c["cache_creation"] += ev["cache_creation"]
    c["cache_read"] += ev["cache_read"]
    window["total_tokens"] += ev["input"] + ev["output"] + ev["cache_creation"] + ev["cache_read"]
    if ev["model"]:
        window["models"].add(ev["model"])


def _iter_claude_events(*, now: datetime) -> Iterable[dict[str, Any]]:
    """Yield deduped usage events from Claude Code transcripts, newest files first.

    Each event: ``{ts, input, output, cache_creation, cache_read, model}``.
    Dedupe key is ``message.id``+``requestId`` so a resumed/forked transcript
    that replays the same assistant turn is not double-counted.
    """
    root = _claude_projects_dir()
    if not os.path.isdir(root):
        return
    files = _recent_files(os.path.join(root, "**", "*.jsonl"), limit=_MAX_CLAUDE_FILES)
    cutoff = (now - _BLOCK_DURATION - timedelta(hours=1)).timestamp()
    seen: set[str] = set()
    for path in files:
        if _safe_mtime(path) < cutoff:
            break
        for obj in _iter_jsonl(path):
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            model = msg.get("model")
            if not isinstance(model, str) or model in _SYNTHETIC_MODELS:
                model = ""
            else:
                model = model.strip()
            ts = _parse_iso(obj.get("timestamp"))
            if ts is None:
                continue
            inp = _int(usage.get("input_tokens"))
            out = _int(usage.get("output_tokens"))
            cc = _int(usage.get("cache_creation_input_tokens"))
            cr = _int(usage.get("cache_read_input_tokens"))
            # Skip zero-token turns (synthetic/error placeholders and empty
            # interrupts). They carry no real usage and would otherwise open a
            # misleading "active block, 0 tokens" window.
            if inp + out + cc + cr == 0:
                continue
            key = f"{msg.get('id')}:{obj.get('requestId')}"
            if key in seen:
                continue
            seen.add(key)
            yield {
                "ts": ts,
                "input": inp,
                "output": out,
                "cache_creation": cc,
                "cache_read": cr,
                "model": model,
            }


# --------------------------------------------------------------------------- #
# Codex latest day
# --------------------------------------------------------------------------- #


def _build_codex() -> dict[str, Any] | None:
    """Read Codex rollouts into a latest-day row plus a quota view, or None.

    Codex ``token_count`` events carry a per-turn delta in
    ``info.last_token_usage`` AND a cumulative ``info.total_token_usage`` for the
    whole session. We bucket only the latest UTC day because that is the row the
    desktop panel renders. All-time totals would require scanning old
    multi-gigabyte sessions on every refresh, so they intentionally stay ``null``
    in the preserved response shape.

    Replay over-count guard (ccusage issue 950): when Codex spawns subagents the
    same ``last_token_usage`` delta can be replayed into a session's JSONL, so a
    naive sum of deltas double-counts. Each session file carries a monotonic
    cumulative ``total_token_usage``; we derive the day's contribution from that
    cumulative total per session instead of summing deltas. The latest-day
    contribution for a session is its final cumulative total on the latest day
    minus its cumulative total as of the prior day's last event (0 if the session
    began on the latest day).

    Quota: each ``token_count`` event can also carry ``payload.rate_limits`` with
    a ``primary`` (5h) and ``secondary`` (weekly) window (``used_percent`` plus
    ``resets_at``) and a ``plan_type``. The published quota is the LAST
    rate_limits payload seen across sessions (newest event timestamp wins), which
    is the operator's current Codex headroom.
    """
    latest_date = None
    # Per-session latest-day token contribution, keyed by session file path so a
    # replayed delta in one file cannot inflate another. Each entry stores the
    # cumulative total/input/output at the prior-day boundary and on the latest
    # day's final event.
    sessions: dict[str, dict[str, Any]] = {}
    # Newest non-null rate_limits windows, tracked independently so a later
    # payload that reports a null window (Codex emits these between billing
    # windows) cannot wipe an earlier valid one.
    primary_ts: datetime | None = None
    secondary_ts: datetime | None = None
    plan_ts: datetime | None = None
    primary_raw: dict[str, Any] | None = None
    secondary_raw: dict[str, Any] | None = None
    plan_type: str | None = None

    for root in _codex_session_dirs():
        if not os.path.isdir(root):
            continue
        for path in _recent_files(os.path.join(root, "**", "*.jsonl"), limit=_MAX_CODEX_FILES):
            if latest_date is not None and _safe_mtime(path) < _utc_midnight_ts(latest_date):
                break
            for obj in _iter_jsonl(path):
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                ts = _parse_iso(obj.get("timestamp"))
                if ts is None:
                    continue

                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, dict):
                    prim = rate_limits.get("primary")
                    if _codex_quota_window(prim) is not None and (
                        primary_ts is None or ts >= primary_ts
                    ):
                        primary_ts = ts
                        primary_raw = prim
                    sec = rate_limits.get("secondary")
                    if _codex_quota_window(sec) is not None and (
                        secondary_ts is None or ts >= secondary_ts
                    ):
                        secondary_ts = ts
                        secondary_raw = sec
                    plan = rate_limits.get("plan_type")
                    if (
                        isinstance(plan, str)
                        and plan.strip()
                        and (plan_ts is None or ts >= plan_ts)
                    ):
                        plan_ts = ts
                        plan_type = plan.strip()

                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                cumulative = info.get("total_token_usage")
                last = info.get("last_token_usage")
                # Prefer the cumulative session total to dodge the replay
                # over-count. Fall back to the per-turn delta only when a session
                # omits the cumulative block.
                if isinstance(cumulative, dict):
                    total = _int(cumulative.get("total_tokens"))
                    inp = _int(cumulative.get("input_tokens"))
                    outp = _int(cumulative.get("output_tokens"))
                    is_cumulative = True
                elif isinstance(last, dict):
                    total = _int(last.get("total_tokens"))
                    inp = _int(last.get("input_tokens"))
                    outp = _int(last.get("output_tokens"))
                    is_cumulative = False
                else:
                    continue
                if total == 0 and inp == 0 and outp == 0:
                    continue

                event_date = ts.date()
                if latest_date is None or event_date > latest_date:
                    latest_date = event_date
                    # A newer day rolls every session's latest-day accounting
                    # forward: the old day's final cumulative total becomes the
                    # prior-day boundary for the new latest day.
                    for entry in sessions.values():
                        day_final = entry.get("day_final")
                        if isinstance(day_final, dict):
                            entry["prior"] = dict(day_final)
                        entry["day_final"] = None
                        # Delta-only sessions have no cumulative boundary, so
                        # their old-day deltas must be dropped here or a
                        # session spanning midnight double-counts yesterday
                        # inside today's bucket.
                        entry["delta_total"] = {"total": 0, "input": 0, "output": 0}

                entry = sessions.setdefault(
                    path,
                    {
                        "prior": {"total": 0, "input": 0, "output": 0},
                        "day_final": None,
                        "delta_total": {"total": 0, "input": 0, "output": 0},
                    },
                )
                if is_cumulative:
                    if event_date < latest_date:
                        entry["prior"] = {"total": total, "input": inp, "output": outp}
                    elif event_date == latest_date:
                        entry["day_final"] = {
                            "total": total,
                            "input": inp,
                            "output": outp,
                        }
                else:
                    # Delta fallback: only count events on the latest day.
                    if event_date == latest_date:
                        d = entry["delta_total"]
                        d["total"] += total
                        d["input"] += inp
                        d["output"] += outp

    if latest_date is None:
        return None

    bucket = {"total": 0, "input": 0, "output": 0}
    for entry in sessions.values():
        day_final = entry.get("day_final")
        if isinstance(day_final, dict):
            prior = entry.get("prior") or {"total": 0, "input": 0, "output": 0}
            for key in ("total", "input", "output"):
                bucket[key] += max(day_final[key] - _int(prior.get(key)), 0)
        else:
            d = entry.get("delta_total") or {}
            for key in ("total", "input", "output"):
                bucket[key] += _int(d.get(key))

    latest_day = {
        "date": latest_date.isoformat(),
        "total_tokens": bucket["total"],
        # No meaningful dollar cost (Codex subscription).
        "cost_usd": None,
        "input_tokens": bucket["input"],
        "output_tokens": bucket["output"],
    }
    totals = {"total_tokens": None, "cost_usd": None}
    out: dict[str, Any] = {"latest_day": latest_day, "totals": totals}
    quota_raw: dict[str, Any] | None = None
    if primary_raw is not None or secondary_raw is not None or plan_type is not None:
        quota_raw = {
            "primary": primary_raw,
            "secondary": secondary_raw,
            "plan_type": plan_type,
        }
    quota = _codex_quota(quota_raw)
    if quota is not None:
        out["quota"] = quota
    return out


def _codex_quota(rate_limits: Any) -> dict[str, Any] | None:
    """Shape a Codex ``rate_limits`` block into the panel's quota view.

    Codex emits ``primary`` (the 5-hour window) and ``secondary`` (the weekly
    window) buckets, each with ``used_percent`` and ``resets_at``, plus a
    top-level ``plan_type``. Returns None when nothing usable is present so the
    panel keeps its honest empty state.
    """
    if not isinstance(rate_limits, dict):
        return None
    primary = _codex_quota_window(rate_limits.get("primary"))
    secondary = _codex_quota_window(rate_limits.get("secondary"))
    plan_type = rate_limits.get("plan_type")
    if primary is None and secondary is None and not isinstance(plan_type, str):
        return None
    return {
        "primary": primary,
        "secondary": secondary,
        "plan_type": plan_type if isinstance(plan_type, str) else None,
    }


def _codex_quota_window(value: Any) -> dict[str, Any] | None:
    """Shape one Codex rate-limit window (``used_percent`` + ``resets_at``)."""
    if not isinstance(value, dict):
        return None
    used = _float(value.get("used_percent"))
    resets_at = _codex_resets_at(value.get("resets_at"))
    if used is None and resets_at is None:
        return None
    return {
        "used_percent": used,
        "resets_at": resets_at,
    }


def _codex_resets_at(value: Any) -> str | None:
    """Normalize a Codex ``resets_at`` to an ISO-8601 ``...Z`` string.

    Codex emits the reset either as an ISO string or as a Unix epoch (seconds).
    The countdown math (:func:`_minutes_to_reset`) and the desktop panel (which
    ``Date.parse``es the value) both need a string, so epoch numbers were being
    silently dropped, hiding the Codex reset time. Coerce both forms here.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (int, float)):
        try:
            return _iso_z(datetime.fromtimestamp(float(value), tz=UTC))
        except (OverflowError, OSError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _recent_files(pattern: str, *, limit: int | None) -> list[str]:
    """Glob ``pattern`` and return matching files, newest-first.

    With an int ``limit`` the scan is bounded to the most-recently-modified
    files (covers the active block). With ``limit=None`` every match is
    returned, for paths that need a complete history.
    """
    paths = [p for p in iglob(pattern, recursive=True) if os.path.isfile(p)]
    paths.sort(key=_safe_mtime, reverse=True)
    return paths if limit is None else paths[:limit]


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _utc_midnight_ts(value) -> float:
    """Unix timestamp for midnight UTC at the start of ``value``'s date."""
    return datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()


def _iter_jsonl(path: str) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON objects from a JSONL file, skipping malformed lines.

    A single corrupt line never aborts the file or the scan.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield obj
    except (OSError, UnicodeError):
        return


def _floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _minutes_until(target: datetime, *, now: datetime) -> int:
    seconds = (target - now).total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 60)


def _iso_z(value: datetime) -> str:
    """Render a UTC datetime as ``...Z`` (matching the log timestamp style)."""
    out = value.astimezone(UTC).replace(tzinfo=None).isoformat()
    return out + "Z"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Logs emit ``...Z``; ``fromisoformat`` on older Pythons does not accept the
    # ``Z`` suffix, so normalize it. Any parse miss degrades to None.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _int(value: Any) -> int:
    """Coerce a token field to a non-negative int; absent/bad -> 0.

    Token counts are integral; clamping at 0 keeps a stray negative from a
    malformed line out of the totals.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    try:
        return max(int(float(value)), 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
