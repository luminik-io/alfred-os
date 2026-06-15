"""Opt-in, off-by-default anonymous proof-telemetry reporter.

This module is the install half of the proof counter. It does **nothing**
unless the operator has explicitly opted in by setting
``ALFRED_TELEMETRY_ENABLED=1``. With that unset (the default) every public
entry point here is a hard no-op: no file is written, no network call is made,
no ``install_id`` is generated.

When enabled, ``report_once`` derives a small anonymous aggregate from the
local fleet-brain counts and POSTs it to a configured endpoint. It is
deliberately fail-soft: any error (missing brain, network failure, bad
response) is swallowed and surfaced only as a return status, never raised. A
telemetry hiccup must never break a firing.

What is sent (the entire payload)::

    {
      "install_id": "<random opaque token, persisted locally>",
      "period":     "<YYYY-MM, the current month>",
      "prs_opened":   <int>,
      "prs_merged":   <int>,
      "prs_reviewed": <int>,
      "loc_added":    <int>
    }

What is NEVER sent: repo names, file paths, code, commit text, branch names,
hostnames, IP addresses, Slack handles, codenames, or anything that identifies
a person or machine. The ``install_id`` is random and has no link to identity;
it exists only so the server can de-duplicate re-sends and count distinct
installs.

Configuration (all read from the environment, never hardcoded):

    ALFRED_TELEMETRY_ENABLED   "1" to opt in. Anything else (including unset)
                               is OFF. This is the single master switch.
    ALFRED_TELEMETRY_URL       Ingest endpoint. Required when enabled; if unset
                               the reporter no-ops rather than guessing a host.
    ALFRED_FLEET_BRAIN_DB /    Locate the local counts (see fleet_brain).
    ALFRED_HOME
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The single master switch. Off unless this is exactly "1".
ENABLE_ENV = "ALFRED_TELEMETRY_ENABLED"
URL_ENV = "ALFRED_TELEMETRY_URL"

# Where the persisted random install_id lives. Under ALFRED_HOME/state so it
# travels with the rest of the runtime state and survives restarts.
_INSTALL_ID_FILENAME = "telemetry-install-id"

# Defensive bounds mirrored from the Worker. The server clamps too, but a
# well-behaved client should not send absurd values in the first place.
_MAX_PER_FIELD = 100_000

_HTTP_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class TelemetryCounts:
    """The four anonymous aggregate counts a host reports."""

    prs_opened: int = 0
    prs_merged: int = 0
    prs_reviewed: int = 0
    loc_added: int = 0


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True only when the operator has explicitly opted in.

    The default (env var unset, or any value other than ``"1"``) is OFF. This
    is intentionally strict: a typo like ``ALFRED_TELEMETRY_ENABLED=true`` does
    NOT turn telemetry on, so an install never reports by accident.
    """
    source = env if env is not None else os.environ
    return source.get(ENABLE_ENV, "").strip() == "1"


def telemetry_url(env: Mapping[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return source.get(URL_ENV, "").strip()


def _clamp(value: int) -> int:
    if value <= 0:
        return 0
    return value if value <= _MAX_PER_FIELD else _MAX_PER_FIELD


def current_period(now: datetime | None = None) -> str:
    """Coarse month bucket, e.g. ``2026-06``. UTC so all installs agree."""
    moment = now or datetime.now(UTC)
    return moment.strftime("%Y-%m")


def _install_id_path() -> Path:
    alfred_home = os.environ.get("ALFRED_HOME")
    root = Path(alfred_home).expanduser() if alfred_home else Path.home() / ".alfred"
    return root / "state" / _INSTALL_ID_FILENAME


def load_or_create_install_id(path: Path | None = None) -> str:
    """Return a stable random install id, creating one on first use.

    The id is a 128-bit URL-safe random token. It is NOT derived from any host
    attribute (hostname, MAC, user). It exists solely so the server can
    de-duplicate re-sends and count distinct installs. Callers only reach this
    when telemetry is already enabled, so generating the file is itself an
    opt-in side effect.
    """
    target = path or _install_id_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = secrets.token_urlsafe(16)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_id + "\n", encoding="utf-8")
    except OSError as exc:
        # Could not persist; still return the id so this run can report. Next
        # run regenerates, which the server tolerates (it just looks like a new
        # install). Better than crashing the firing.
        logger.debug("telemetry: could not persist install id: %s", exc)
    return new_id


def derive_counts(brain: Any) -> TelemetryCounts:
    """Roll the local fleet-brain rows up into the four anonymous counts.

    Pure read: queries the brain, returns counts, touches nothing else. Counts
    are cumulative totals (the server treats them as such and de-duplicates).

    Derivation, with an honest mapping to what the brain actually stores:

      prs_opened   distinct PRs the brain has ever cached (github_items where
                   kind == "pr").
      prs_merged   that subset whose state == "merged".
      prs_reviewed that subset whose state is terminal (merged or closed); a PR
                   reaching a terminal state went through review in Alfred's
                   flow. Conservative: never exceeds prs_opened.
      loc_added    a file-delta proxy: the count of file_touches rows (one per
                   repo file an agent added/modified). The brain does not store
                   per-line LOC, so this is a file-delta count, documented as
                   such in docs/TELEMETRY.md. The field keeps the wire name
                   ``loc_added`` for forward compatibility.

    Any query failure yields zeroes for the affected fields rather than raising.
    """
    prs_opened = 0
    prs_merged = 0
    prs_reviewed = 0
    loc_added = 0

    try:
        prs = brain.list_github_items(kind="pr", limit=500)
        prs_opened = len(prs)
        prs_merged = sum(1 for p in prs if getattr(p, "state", None) == "merged")
        prs_reviewed = sum(1 for p in prs if getattr(p, "state", None) in ("merged", "closed"))
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        logger.debug("telemetry: PR count derivation failed: %s", exc)

    try:
        touches = brain.list_file_touches(limit=500)
        loc_added = len(touches)
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        logger.debug("telemetry: file-touch count derivation failed: %s", exc)

    return TelemetryCounts(
        prs_opened=_clamp(prs_opened),
        prs_merged=_clamp(prs_merged),
        prs_reviewed=_clamp(prs_reviewed),
        loc_added=_clamp(loc_added),
    )


def build_payload(install_id: str, counts: TelemetryCounts, period: str) -> dict[str, Any]:
    """Assemble the exact JSON body that goes on the wire. No extra keys."""
    return {
        "install_id": install_id,
        "period": period,
        "prs_opened": _clamp(counts.prs_opened),
        "prs_merged": _clamp(counts.prs_merged),
        "prs_reviewed": _clamp(counts.prs_reviewed),
        "loc_added": _clamp(counts.loc_added),
    }


def _post(url: str, payload: dict[str, Any], *, timeout: int = _HTTP_TIMEOUT_SECONDS) -> bool:
    """POST the payload as JSON. Returns True on a 2xx, False on any failure.

    Never raises. Network errors, timeouts, and non-2xx responses all map to
    False so a caller can treat telemetry as strictly best-effort.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        # URL is operator-configured (ALFRED_TELEMETRY_URL); not attacker input.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        logger.debug("telemetry: server returned HTTP %s", exc.code)
        return False
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("telemetry: post failed: %s", exc)
        return False


def report_once(
    *,
    env: Mapping[str, str] | None = None,
    brain: Any | None = None,
    now: datetime | None = None,
    poster: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Derive, build, and send one telemetry report. Fully fail-soft.

    Returns a small status dict describing what happened. The status is the
    only output; this function never raises and never prints.

    Status ``status`` values:
      ``disabled``  the master switch is off (the default). Nothing happened.
      ``no_url``    enabled but ``ALFRED_TELEMETRY_URL`` is unset. No-op.
      ``sent``      payload posted and the server accepted it.
      ``failed``    enabled and attempted, but the post did not succeed.
      ``error``     an unexpected internal error, swallowed.

    ``brain`` and ``poster`` are injectable for tests; in production they
    default to the real fleet-brain and a urllib POST.
    """
    source = env if env is not None else os.environ
    if not is_enabled(source):
        return {"status": "disabled", "sent": False}

    url = telemetry_url(source)
    if not url:
        return {"status": "no_url", "sent": False}

    try:
        if brain is None:
            # Imported lazily so the disabled path never imports the brain.
            from fleet_brain import FleetBrain

            brain = FleetBrain.from_env()
        install_id = load_or_create_install_id()
        period = current_period(now)
        counts = derive_counts(brain)
        payload = build_payload(install_id, counts, period)
        send = poster or _post
        ok = bool(send(url, payload))
        return {
            "status": "sent" if ok else "failed",
            "sent": ok,
            "period": period,
            "counts": {
                "prs_opened": payload["prs_opened"],
                "prs_merged": payload["prs_merged"],
                "prs_reviewed": payload["prs_reviewed"],
                "loc_added": payload["loc_added"],
            },
        }
    except Exception as exc:  # fail-soft by contract: a telemetry hiccup is silent
        logger.debug("telemetry: report_once swallowed error: %s", exc)
        return {"status": "error", "sent": False}
