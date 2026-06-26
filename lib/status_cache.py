"""TTL-aware status cache for the alfred-status fast path.

Background: ``alfred status`` and the morning brief both probe auth and
identity signals that are expensive to re-resolve every call. The
original implementation used file mtime as the freshness signal. That
broke after a host reboot or a manual ``touch`` because the file
system timestamp could disagree with the cache's own internal "when
was this captured" assertion. It also conflated short-lived auth state
with slow-changing identity state under a single TTL.

This module fixes both:

* Every record stores a ``cache_written_at`` ISO8601 UTC timestamp
  inside its JSON body. Readers parse the embedded timestamp first
  and fall back to file mtime only when the field is missing (older
  cache files). The cache survives a manual ``touch`` correctly.

* Two named TTL profiles: ``AUTH_TTL_SECONDS`` (60s) for AWS/Codex/
  Claude auth probes whose state flips on the order of minutes, and
  ``SLOW_TTL_SECONDS`` (1800s) for slow-changing state.

The helper is intentionally small: no schema, no migration, no
locking. Callers serialize whatever dict they want and supply a TTL.
Reads return ``None`` on a miss or stale entry so the caller falls
through to its live-probe path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AUTH_TTL_SECONDS = int(os.environ.get("ALFRED_STATUS_AUTH_TTL_SECONDS", "60"))
SLOW_TTL_SECONDS = int(os.environ.get("ALFRED_STATUS_SLOW_TTL_SECONDS", "1800"))

_TIMESTAMP_KEY = "cache_written_at"


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def cache_age_seconds(path: Path) -> float | None:
    """Return the cache's self-reported age in seconds, or ``None`` when
    the file is missing or unparseable.

    Prefers the embedded ``cache_written_at`` field; falls back to file
    mtime when the field is absent (older cache files).
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        ts = data.get(_TIMESTAMP_KEY)
        parsed = _parse_iso_utc(ts) if isinstance(ts, str) else None
        if parsed is not None:
            return max(0.0, (datetime.now(UTC) - parsed).total_seconds())
    try:
        return max(
            0.0,
            datetime.now(UTC).timestamp() - path.stat().st_mtime,
        )
    except OSError:
        return None


def read_cache(path: Path, *, ttl_seconds: int) -> dict[str, Any] | None:
    """Return the cached payload if fresh, else ``None``."""
    age = cache_age_seconds(path)
    if age is None or age > ttl_seconds:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["cache_age_seconds"] = round(age, 1)
    return data


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Persist ``payload`` to ``path`` with a fresh ``cache_written_at``.

    Always overwrites the existing file. The caller's keys win on
    conflict with the timestamp key: a payload that already contains
    ``cache_written_at`` will see it replaced with the current UTC
    stamp so a re-probe never inherits the previous read's timestamp.
    """
    record = dict(payload)
    record[_TIMESTAMP_KEY] = _now_utc_iso()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
    except OSError:
        pass


def get_or_refresh(
    path: Path,
    *,
    ttl_seconds: int,
    refresh_fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Return the cached payload when fresh, otherwise re-probe via
    ``refresh_fn`` and persist the new result.

    ``refresh_fn`` is invoked with no arguments and must return a
    ``dict``. Errors raised by ``refresh_fn`` propagate to the caller -     the cache is a freshness primitive, not an exception swallow.
    """
    cached = read_cache(path, ttl_seconds=ttl_seconds)
    if cached is not None:
        cached["status_source"] = "cache"
        return cached
    fresh = refresh_fn()
    if not isinstance(fresh, dict):
        raise TypeError(
            f"status_cache.get_or_refresh: refresh_fn must return a dict; "
            f"got {type(fresh).__name__}"
        )
    write_cache(path, fresh)
    out = dict(fresh)
    out["status_source"] = "live"
    out["cache_age_seconds"] = 0.0
    return out


__all__ = [
    "AUTH_TTL_SECONDS",
    "SLOW_TTL_SECONDS",
    "cache_age_seconds",
    "get_or_refresh",
    "read_cache",
    "write_cache",
]
