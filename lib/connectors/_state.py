"""
Per-connector seen-cache.

The cache lives at ``$ALFRED_HOME/state/connectors/<name>.json`` and
holds two keys:

    {
      "last_poll_at": "2026-05-23T14:00:00+00:00",
      "seen_ids": ["LIN-1042", "LIN-1043", "sentry-abc123", ...]
    }

* ``last_poll_at`` is the high-water mark passed back to
  ``Connector.poll(since=...)`` on the next run.
* ``seen_ids`` is the dedup set used by the runner to skip drafts that
  already produced a GitHub issue, even if upstream returns them again.

The file is rewritten atomically. The seen-id list is FIFO-trimmed to
``MAX_SEEN`` entries so the cache never grows unbounded; in practice an
average connector polls every 10-30 min and writes a few IDs per poll,
so 10k entries is comfortably above one quarter of traffic.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path

MAX_SEEN = 10_000


def state_dir() -> Path:
    """Return ``$ALFRED_HOME/state/connectors`` (created on demand)."""
    alfred_home = Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred"))
    d = alfred_home / "state" / "connectors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(name: str) -> Path:
    return state_dir() / f"{name}.json"


def load_state(name: str) -> dict:
    """Return ``{"last_poll_at": str|None, "seen_ids": list[str]}``."""
    p = state_path(name)
    if not p.exists():
        return {"last_poll_at": None, "seen_ids": []}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        # Corrupt cache: treat as empty rather than crash the sync.
        return {"last_poll_at": None, "seen_ids": []}
    if not isinstance(data, dict):
        return {"last_poll_at": None, "seen_ids": []}
    last = data.get("last_poll_at")
    seen = data.get("seen_ids") or []
    if not isinstance(seen, list):
        seen = []
    return {
        "last_poll_at": last if isinstance(last, str) else None,
        "seen_ids": [str(s) for s in seen],
    }


def save_state(name: str, *, last_poll_at: datetime | None, seen_ids: list[str]) -> None:
    """Atomically write the cache, trimming ``seen_ids`` to ``MAX_SEEN``."""
    trimmed = list(deque(seen_ids, maxlen=MAX_SEEN))
    payload = {
        "last_poll_at": last_poll_at.isoformat() if last_poll_at else None,
        "seen_ids": trimmed,
    }
    p = state_path(name)
    # NamedTemporaryFile with delete=False - we close + rename ourselves so
    # the write is atomic on the same filesystem.
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - manual close + atomic rename
        mode="w",
        dir=p.parent,
        prefix=f".{p.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise


def parse_last_poll(last: str | None) -> datetime | None:
    if not last:
        return None
    try:
        return datetime.fromisoformat(last)
    except ValueError:
        return None
