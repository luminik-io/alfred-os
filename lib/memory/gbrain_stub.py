"""Optional subprocess-backed personal-knowledge-base provider.

This module is **not** bundled functionality. It is a shim: if an
operator maintains a personal knowledge base CLI somewhere on their
machine, they can point Alfred at it via ``ALFRED_GBRAIN_BIN`` and
have ``recall`` consult it. When the binary is missing or fails,
the provider degrades to an empty result so the rest of the chain
keeps working.

The "gbrain" name here is generic ("the operator's optional personal
knowledge base"). The provider does not assume any particular CLI
shape; it invokes the configured binary with a small JSON payload
and parses a JSON response (a list of ``{"body": str, "tags": [str]}``
objects, plus optional ``codename`` / ``repo`` / ``severity`` /
``created_at`` / ``id``).

Wire format (request, on stdin):

.. code-block:: json

   {
     "op": "recall",
     "query": "graphql",
     "codename": "lucius",
     "repo": "acme-org/api",
     "limit": 5
   }

Wire format (response, on stdout):

.. code-block:: json

   [
     {
       "body": "GraphQL schema lives in src/schema.graphql",
       "tags": ["graphql"],
       "codename": "lucius",
       "repo": "acme-org/api"
     }
   ]

The shim is **read-only**: :meth:`reflect` raises
:class:`NotImplementedError`. The chained provider catches that and
writes to the next writable provider in the chain (typically the
fleet-brain).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleet_brain import Lesson, Severity, new_id

__all__ = ["GBrainProvider"]

_LOG = logging.getLogger(__name__)

# Cap subprocess wall-time so a hung knowledge-base CLI cannot stall
# the runner. Five seconds is generous for a local-only query.
_DEFAULT_TIMEOUT_S = 5.0


@dataclass
class GBrainProvider:
    """Read-only provider that shells out to an external CLI.

    The binary path is read from ``ALFRED_GBRAIN_BIN`` (override via
    ``binary_path=`` for tests). If the path is unset or the file is
    not executable, the provider degrades to an empty result.

    The shim is intentionally tolerant: any subprocess error, JSON
    parse failure, or wrong-shape response logs at debug level and
    returns ``[]`` so the chain falls through cleanly.
    """

    binary_path: Path | None = None
    timeout_s: float = _DEFAULT_TIMEOUT_S
    name: str = "gbrain"

    @classmethod
    def from_env(cls, *, env: dict[str, str] | None = None) -> GBrainProvider:
        """Build from process environment. Returns a provider even if
        the binary is missing -- ``recall`` just returns ``[]``."""
        envmap = env if env is not None else dict(os.environ)
        raw = envmap.get("ALFRED_GBRAIN_BIN", "").strip()
        path = Path(raw).expanduser() if raw else None
        return cls(binary_path=path)

    # ----- recall --------------------------------------------------------

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        binary = self._resolved_binary()
        if binary is None:
            _LOG.debug("memory.gbrain: binary not configured; returning empty")
            return []
        payload = {
            "op": "recall",
            "query": query,
            "codename": codename,
            "repo": repo,
            "limit": int(limit),
        }
        try:
            result = subprocess.run(
                [str(binary)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _LOG.debug("memory.gbrain: subprocess failed: %s", exc)
            return []
        if result.returncode != 0:
            _LOG.debug(
                "memory.gbrain: binary exited %d: %s",
                result.returncode,
                result.stderr[:200],
            )
            return []
        return _parse_response(result.stdout)

    # ----- reflect (read-only) ------------------------------------------

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        firing_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        raise NotImplementedError(
            "GBrainProvider is read-only; the chain should write to a "
            "writable provider (e.g. the fleet-brain) instead."
        )

    # ----- helpers ------------------------------------------------------

    def _resolved_binary(self) -> Path | None:
        path = self.binary_path
        if path is None or not str(path):
            return None
        # Accept bare names (resolved via PATH) and absolute paths.
        if path.is_absolute():
            return path if path.exists() and os.access(path, os.X_OK) else None
        found = shutil.which(str(path))
        return Path(found) if found else None


def _parse_response(raw: str) -> list[Lesson]:
    """Parse the binary's JSON response into :class:`Lesson` rows.

    Best-effort: any malformed entry is skipped, and a non-list root
    yields an empty result.
    """
    raw = raw.strip()
    if not raw:
        return []
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.debug("memory.gbrain: JSON parse failed: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    out: list[Lesson] = []
    for entry in data:
        lesson = _entry_to_lesson(entry)
        if lesson is not None:
            out.append(lesson)
    return out


def _entry_to_lesson(entry: Any) -> Lesson | None:
    if not isinstance(entry, dict):
        return None
    body = entry.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    raw_tags = entry.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = sorted({str(t).strip() for t in raw_tags if str(t).strip()})
    severity_raw = entry.get("severity", "info")
    severity: Severity = severity_raw if severity_raw in ("info", "warning", "blocker") else "info"
    created_raw = entry.get("created_at")
    created_at = _parse_iso(created_raw) if isinstance(created_raw, str) else datetime.now(UTC)
    return Lesson(
        id=str(entry.get("id") or new_id()),
        codename=str(entry.get("codename") or ""),
        repo=str(entry.get("repo") or ""),
        body=body.strip(),
        tags=tags,
        created_at=created_at,
        firing_id=entry.get("firing_id"),
        severity=severity,
    )


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(UTC)
