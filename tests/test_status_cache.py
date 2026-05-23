"""Coverage for the TTL-aware ``status_cache`` helper.

The helper replaces ``file.stat().st_mtime``-based freshness for any
caller that caches auth or identity probes. The embedded timestamp
survives ``touch``, restore-from-backup, copy-across-hosts — mtime
does not.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "test-cache.json"


def test_write_cache_embeds_cache_written_at(cache_path):
    from status_cache import write_cache

    write_cache(cache_path, {"aws_sso_alive": True})
    data = json.loads(cache_path.read_text())
    assert "cache_written_at" in data
    datetime.strptime(data["cache_written_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert data["aws_sso_alive"] is True


def test_write_cache_overwrites_caller_supplied_timestamp(cache_path):
    """A caller passing a stale ``cache_written_at`` must not poison
    the freshness check; the helper always stamps the current time."""
    from status_cache import write_cache

    stale = "2020-01-01T00:00:00Z"
    write_cache(cache_path, {"aws_sso_alive": True, "cache_written_at": stale})
    data = json.loads(cache_path.read_text())
    assert data["cache_written_at"] != stale


def test_read_cache_returns_none_when_missing(cache_path):
    from status_cache import read_cache

    assert read_cache(cache_path, ttl_seconds=60) is None


def test_read_cache_returns_payload_when_fresh(cache_path):
    from status_cache import read_cache, write_cache

    write_cache(cache_path, {"aws_sso_alive": True})
    out = read_cache(cache_path, ttl_seconds=60)
    assert out is not None
    assert out["aws_sso_alive"] is True
    assert out["cache_age_seconds"] < 5


def test_read_cache_returns_none_when_embedded_timestamp_is_old(cache_path):
    """Cache freshness uses the embedded timestamp, not file mtime.
    A file whose mtime is fresh but whose body says
    ``cache_written_at`` is 10 minutes ago must be treated as stale."""
    from status_cache import read_cache

    stale_ts = (datetime.now(UTC) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_path.write_text(json.dumps({"aws_sso_alive": True, "cache_written_at": stale_ts}))
    now = time.time()
    os.utime(cache_path, (now, now))

    assert read_cache(cache_path, ttl_seconds=60) is None


def test_read_cache_falls_back_to_mtime_when_field_missing(cache_path):
    """Older cache files without the field fall back to mtime so an
    upgrade does not flush every existing cache on the first read."""
    from status_cache import read_cache

    cache_path.write_text(json.dumps({"aws_sso_alive": True}))
    out = read_cache(cache_path, ttl_seconds=60)
    assert out is not None
    assert out["aws_sso_alive"] is True


def test_get_or_refresh_uses_cache_when_fresh(cache_path):
    from status_cache import get_or_refresh, write_cache

    write_cache(cache_path, {"value": "cached"})
    calls = {"n": 0}

    def refresh():
        calls["n"] += 1
        return {"value": "fresh"}

    out = get_or_refresh(cache_path, ttl_seconds=60, refresh_fn=refresh)
    assert out["value"] == "cached"
    assert calls["n"] == 0
    assert out["status_source"] == "cache"


def test_get_or_refresh_calls_refresh_when_stale(cache_path):
    from status_cache import get_or_refresh

    stale_ts = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cache_path.write_text(json.dumps({"value": "old", "cache_written_at": stale_ts}))

    calls = {"n": 0}

    def refresh():
        calls["n"] += 1
        return {"value": "fresh"}

    out = get_or_refresh(cache_path, ttl_seconds=60, refresh_fn=refresh)
    assert out["value"] == "fresh"
    assert calls["n"] == 1
    assert out["status_source"] == "live"
    persisted = json.loads(cache_path.read_text())
    assert persisted["value"] == "fresh"


def test_get_or_refresh_requires_dict_from_refresh_fn(cache_path):
    from status_cache import get_or_refresh

    with pytest.raises(TypeError):
        get_or_refresh(
            cache_path,
            ttl_seconds=60,
            refresh_fn=lambda: ["not", "a", "dict"],
        )
