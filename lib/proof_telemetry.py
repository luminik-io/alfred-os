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
      "period":     "lifetime",
      "prs_opened":   <int>,
      "prs_merged":   <int>,
      "prs_reviewed": <int>,
      "loc_added":    <int>
    }

The four counts are CUMULATIVE LIFETIME totals (everything the local brain has
ever cached), not a per-run or per-month delta. ``period`` is the constant
``"lifetime"`` so that an install always reports into the same
``{install_id, period}`` bucket on the Worker. The Worker stores the last counts
it saw for that bucket and folds only the *increase* into the public aggregate,
so re-sending every day, and re-sending after a calendar month rolls over, never
double-counts. This agent/worker contract is the whole reason the public counter
stays honest.

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
    ALFRED_TELEMETRY_TOKEN     Optional shared ingest token. When the collector
                               is configured with INGEST_TOKEN, set this to the
                               same value; it is sent as the X-Ingest-Token
                               header. Unset is fine for an open counter.
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
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The single master switch. Off unless this is exactly "1".
ENABLE_ENV = "ALFRED_TELEMETRY_ENABLED"
URL_ENV = "ALFRED_TELEMETRY_URL"
# Optional shared ingest token. Sent as X-Ingest-Token when set, so a collector
# configured with INGEST_TOKEN accepts this host's writes. Unset is fine.
TOKEN_ENV = "ALFRED_TELEMETRY_TOKEN"

# Where the persisted random install_id lives. Under ALFRED_HOME/state so it
# travels with the rest of the runtime state and survives restarts.
_INSTALL_ID_FILENAME = "telemetry-install-id"

# Defensive bounds mirrored from the Worker. The server clamps too, but a
# well-behaved client should not send absurd values in the first place.
_MAX_PER_FIELD = 100_000

# Counting is done by raising the brain's list `limit` until the returned row
# count stops growing (we have seen every row, or the brain's own internal cap
# truncates us). _COUNT_PAGE is the step. We stop once a fetch returns fewer
# rows than asked for, which means there were no more rows beyond it.
_COUNT_PAGE = 1000
# Absolute ceiling so a runaway brain can never make us allocate without bound.
# Matches the per-field clamp: anything above this is reported as the clamp
# anyway, so there is no point fetching past it.
_COUNT_HARD_LIMIT = _MAX_PER_FIELD

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


def telemetry_token(env: Mapping[str, str] | None = None) -> str:
    """Optional shared ingest token, or empty string when unset."""
    source = env if env is not None else os.environ
    return source.get(TOKEN_ENV, "").strip()


def _clamp(value: int) -> int:
    if value <= 0:
        return 0
    return value if value <= _MAX_PER_FIELD else _MAX_PER_FIELD


# The reporter sends LIFETIME-cumulative counts, so the period must be a single
# stable bucket per install. A calendar bucket (e.g. "2026-06") would break the
# contract: when the month rolls over, the Worker's {install_id, period}
# idempotency key changes, prior becomes 0, and the full lifetime total gets
# re-added (double-counting on every busy install). With one fixed bucket the
# Worker always applies only the increase in the cumulative total, no matter how
# often (daily) or how long (across months) an install reports.
_LIFETIME_PERIOD = "lifetime"


def current_period(now: datetime | None = None) -> str:
    """Stable lifetime bucket for an install's cumulative counts.

    Returns the constant ``"lifetime"``. The counts this module reports are
    cumulative lifetime totals, not per-month deltas, so they must always land
    in the same ``{install_id, period}`` bucket on the Worker. The ``now``
    argument is accepted for signature stability (some callers and tests pass a
    fixed clock) but is intentionally ignored: the bucket never depends on the
    calendar.
    """
    return _LIFETIME_PERIOD


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


def _count_rows(lister: Callable[[int], list[Any]]) -> int:
    """Count rows from a brain `list_*` method without a silent low cap.

    ``lister`` takes a ``limit`` and returns up to that many rows from the top.
    The brain has no offset/cursor, so we count by raising ``limit`` one
    ``_COUNT_PAGE`` at a time and stopping when a fetch returns fewer rows than
    we asked for, which proves there were no more rows beyond it. This turns the
    old hard ``limit=500`` (which silently froze any total at 500) into an
    honest count, while ``_COUNT_HARD_LIMIT`` keeps it bounded so a runaway
    brain can never make us allocate without end.

    Any total at or above the hard limit is reported as the limit; the field is
    clamped to the same bound on the wire anyway, so fetching further is wasted.
    """
    limit = _COUNT_PAGE
    last = 0
    while True:
        rows = lister(limit)
        got = len(rows)
        if got >= _COUNT_HARD_LIMIT:
            return _COUNT_HARD_LIMIT
        if got < limit:
            # A short page means we have reached the end of the rows.
            return got
        if got == last:
            # The brain returned a full page but no new rows beyond the previous
            # request: it has hit its own internal cap. Honest stop.
            return got
        last = got
        limit += _COUNT_PAGE


def derive_counts(brain: Any) -> TelemetryCounts:
    """Roll the local fleet-brain rows up into the four anonymous counts.

    Pure read: queries the brain, returns counts, touches nothing else. Counts
    are CUMULATIVE LIFETIME totals (the Worker treats them as such and folds in
    only the increase, see the module docstring), so they must reflect every row
    the brain holds, not a truncated page. Counting paginates via ``_count_rows``
    rather than taking ``len()`` of a single capped ``limit=500`` fetch, which
    would silently freeze any total at 500 on a busy install.

    Derivation, with an honest mapping to what the brain actually stores:

      prs_opened   distinct PRs the brain has ever cached (github_items where
                   kind == "pr").
      prs_merged   that subset whose state == "merged" (counted with a server-
                   side state filter, so it never undercounts behind a page cap).
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
        prs_opened = _count_rows(lambda n: brain.list_github_items(kind="pr", limit=n))
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        logger.debug("telemetry: PR-opened count derivation failed: %s", exc)

    # Count merged and terminal (merged|closed) PRs with a state filter so each
    # is an accurate total rather than a sample of the first page. Fall back to
    # an in-memory tally of a single page if the brain does not accept `state`.
    try:
        prs_merged = _count_rows(
            lambda n: brain.list_github_items(kind="pr", state="merged", limit=n)
        )
        closed = _count_rows(lambda n: brain.list_github_items(kind="pr", state="closed", limit=n))
        prs_reviewed = prs_merged + closed
    except TypeError:
        # Brain's list_github_items has no `state` kwarg: derive from one page.
        try:
            prs = brain.list_github_items(kind="pr", limit=_COUNT_HARD_LIMIT)
            prs_merged = sum(1 for p in prs if getattr(p, "state", None) == "merged")
            prs_reviewed = sum(1 for p in prs if getattr(p, "state", None) in ("merged", "closed"))
        except Exception as exc:  # fail-soft by contract
            logger.debug("telemetry: PR-state count derivation failed: %s", exc)
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        logger.debug("telemetry: PR-state count derivation failed: %s", exc)

    # prs_reviewed is defined as a subset of opened, so never let the state-based
    # tally exceed the opened total (e.g. if filters and the top-level count
    # raced against a concurrent write).
    if prs_opened and prs_reviewed > prs_opened:
        prs_reviewed = prs_opened
    if prs_opened and prs_merged > prs_opened:
        prs_merged = prs_opened

    try:
        loc_added = _count_rows(lambda n: brain.list_file_touches(limit=n))
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


def _post(
    url: str,
    payload: dict[str, Any],
    *,
    token: str = "",
    timeout: int = _HTTP_TIMEOUT_SECONDS,
) -> bool:
    """POST the payload as JSON. Returns True on a 2xx, False on any failure.

    Never raises. Network errors, timeouts, and non-2xx responses all map to
    False so a caller can treat telemetry as strictly best-effort. When ``token``
    is non-empty it is sent as the ``X-Ingest-Token`` header so a collector
    configured with ``INGEST_TOKEN`` accepts the write.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Ingest-Token"] = token
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=headers,
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
        # Default poster carries the optional ingest token; an injected poster
        # (tests) keeps the simple (url, payload) signature.
        token = telemetry_token(source)
        send = poster or (lambda u, p: _post(u, p, token=token))
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
