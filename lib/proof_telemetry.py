"""Anonymous proof-telemetry reporter.

This module is the install half of the proof counter. It does **nothing**
when the operator opts out by setting ``ALFRED_TELEMETRY_ENABLED=0`` (or
``false``, ``no``, ``off``, ``disabled``). With the switch left unset, the
reporter is eligible to run, but a report is still sent only when
``ALFRED_TELEMETRY_URL`` is configured. Without a URL, no file is written, no
network call is made, and no ``install_id`` is generated.

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
      "issues_opened": <int>,
      "issues_closed": <int>,
      "files_changed": <int>,
      "lines_changed": <int>,
      "loc_added":     <int>  # legacy alias for files_changed
    }

The counts are CUMULATIVE LIFETIME totals (everything the local brain has
ever cached), not a per-run or per-month delta. The agent/worker contract is
"latest-wins per install": the Worker keys exactly ONE record on ``install_id``
and replaces it on every report, and the public total is DERIVED on read by
summing every install's latest record. Re-sending the same lifetime total for
the same install therefore changes nothing, forever, no matter how often or for
how long an install reports (the record is replaced with an identical value, so
the sum is unchanged). ``period`` is advisory metadata only (always the constant
``"lifetime"`` here); the Worker does NOT use it as part of the storage key, so a
calendar rollover can never re-add a constant lifetime total. This contract is
the whole reason the public counter stays honest.

What is NEVER sent: repo names, file paths, code, commit text, branch names,
hostnames, IP addresses, Slack handles, codenames, or anything that identifies
a person or machine. The ``install_id`` is random and has no link to identity;
it exists only so the server can de-duplicate re-sends and count distinct
installs.

Configuration (all read from the environment):

    ALFRED_TELEMETRY_ENABLED   Opt-out switch. ``0``, ``false``, ``no``,
                               ``off``, or ``disabled`` is OFF. Unset is ON.
    ALFRED_TELEMETRY_URL       Ingest endpoint. Required to send; if unset the
                               reporter no-ops rather than guessing a host.
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

# The single master switch. On unless explicitly disabled.
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
_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}


def _switch_value(raw: str) -> str:
    value = raw.strip()
    comment_at = value.find("#")
    if comment_at > 0 and value[comment_at - 1].isspace():
        value = value[:comment_at].strip()
    return value.lower()


@dataclass(frozen=True)
class TelemetryCounts:
    """The anonymous aggregate counts a host reports."""

    prs_opened: int = 0
    prs_merged: int = 0
    prs_reviewed: int = 0
    issues_opened: int = 0
    issues_closed: int = 0
    files_changed: int | None = None
    lines_changed: int = 0
    loc_added: int = 0
    read_complete: bool = True


def is_enabled(env: Mapping[str, str] | None = None) -> bool:
    """False only when the operator has explicitly opted out.

    Unset is enabled. A configured endpoint is still required before anything is
    sent; ``report_once`` returns ``no_url`` without generating an install id
    when the endpoint is missing.
    """
    source = env if env is not None else os.environ
    raw = source.get(ENABLE_ENV)
    if raw is None:
        return True
    return _switch_value(raw) not in _DISABLED_VALUES


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


# The reporter sends LIFETIME-cumulative counts. Under the install-keyed
# latest-wins model the Worker de-duplicates on install_id alone, so this label
# is advisory metadata, not an idempotency key. We still send a single stable
# value ("lifetime") so the wire payload is self-describing and so the contract
# reads clearly: the counts are an install's whole-life cumulative total, never
# a per-month delta. A calendar label here would be misleading (and, were the
# Worker ever to key on it, would re-add the constant total on rollover), so the
# value is intentionally clock-independent.
_LIFETIME_PERIOD = "lifetime"


def current_period(now: datetime | None = None) -> str:
    """Stable lifetime label for an install's cumulative counts.

    Returns the constant ``"lifetime"``. The counts this module reports are
    cumulative lifetime totals, not per-month deltas. The Worker keys its single
    per-install record on ``install_id`` alone (latest-wins), so this label is
    advisory metadata rather than an idempotency key. The ``now`` argument is
    accepted for signature stability (some callers and tests pass a fixed clock)
    but is intentionally ignored: the label never depends on the calendar.
    """
    return _LIFETIME_PERIOD


def _install_id_path() -> Path:
    alfred_home = os.environ.get("ALFRED_HOME")
    root = Path(alfred_home).expanduser() if alfred_home else Path.home() / ".alfred"
    return root / "state" / _INSTALL_ID_FILENAME


def load_persisted_install_id(path: Path | None = None) -> str | None:
    """Return the existing install id without creating a new one."""
    target = path or _install_id_path()
    try:
        existing = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return existing or None


def load_or_create_persisted_install_id(path: Path | None = None) -> str | None:
    """Return a STABLE, PERSISTED random install id, or ``None`` if it cannot
    be persisted.

    The id is a 128-bit URL-safe random token. It is NOT derived from any host
    attribute (hostname, MAC, user). It exists solely so the server can
    de-duplicate re-sends and count distinct installs. Callers only reach this
    when telemetry is already enabled, so generating the file is itself an
    opt-in side effect.

    Crucially, this returns an id ONLY when it is durable: an existing file is
    read back, or a freshly minted id is successfully written to disk. If the
    id can be neither read nor written (``$ALFRED_HOME/state`` is unwritable, a
    read-only filesystem, a permissions problem), this returns ``None`` rather
    than minting an ephemeral token. An ephemeral token would be different on
    every run, and since the Worker de-duplicates on ``install_id`` alone, every
    scheduled report from such a host would look like a brand-new install and
    inflate the public install count. Returning ``None`` lets the caller skip
    reporting entirely on that run, which keeps the distinct-install count
    honest. See ``report_once``.
    """
    target = path or _install_id_path()
    existing = load_persisted_install_id(target)
    if existing:
        return existing
    new_id = secrets.token_urlsafe(16)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_id + "\n", encoding="utf-8")
    except OSError as exc:
        # Could not persist. Do NOT return an ephemeral id: a fresh token every
        # run would make this host look like a new install on every report and
        # inflate the install count. Signal the failure so the caller skips
        # this run's report instead. Next run retries persistence.
        logger.debug("telemetry: could not persist install id, skipping report: %s", exc)
        return None
    return new_id


def load_or_create_install_id(path: Path | None = None) -> str:
    """Return a stable random install id, creating one on first use.

    Best-effort variant of :func:`load_or_create_persisted_install_id` for
    local, non-reporting callers (e.g. the ``--dry-run`` preview): if the id
    cannot be persisted it still returns a fresh token so the operator can see a
    sample payload. The REPORTING path must NOT use this; it uses
    :func:`load_or_create_persisted_install_id` and skips reporting when the id
    cannot be persisted, so an unpersisted ephemeral id is never POSTed.
    """
    persisted = load_or_create_persisted_install_id(path)
    if persisted is not None:
        return persisted
    # Persistence failed; mint an ephemeral token for local display only. This
    # is never sent (report_once uses the persisted-only path).
    return secrets.token_urlsafe(16)


def _count_rows(
    lister: Callable[[int], list[Any]],
    predicate: Callable[[Any], bool] | None = None,
) -> int:
    """Fallback row counter for a brain that exposes only a `list_*` method.

    The real ``FleetBrain`` exposes exact ``count_*`` methods (a SQL
    ``COUNT(*)``) which the callers below prefer; this paginating fallback is for
    brains/test-doubles that lack them. ``lister`` takes a ``limit`` and returns
    up to that many RAW rows from the top (NOT pre-filtered). ``predicate``, when
    given, selects which raw rows count toward the total (e.g. agent-authored
    rows); when ``None`` every raw row counts.

    Pagination decides continuation on the RAW fetched row count, never on the
    post-filter match count. This is load-bearing: a ``predicate`` discards rows,
    so a page can return fewer MATCHES than ``limit`` while the brain still has
    more rows. Stopping on the filtered count would end early and undercount
    (e.g. a brain with 1200 PRs, 600 authored + 600 operator, would stop on the
    first page once the authored matches fell short of ``limit``). We raise
    ``limit`` one ``_COUNT_PAGE`` at a time and stop only when the brain returns
    fewer RAW rows than asked for (true end of data), the same RAW count twice in
    a row (the brain hit an internal cap or list clamp), or the hard ceiling (a
    runaway brain).

    IMPORTANT: if the underlying list method silently CLAMPS ``limit`` (the real
    ``FleetBrain`` clamps to 500), this fallback cannot count past that clamp:
    raising ``limit`` does nothing once it is clamped, so the raw ``got == last``
    trips and we stop at the clamp. That is exactly why the exact ``count_*`` path
    exists and is tried first; this fallback's honest stop at a clamp is strictly
    better than the old ``len(list(limit=500))`` that always froze at 500, but it
    is still bounded by whatever clamp the list method imposes.

    Any match total at or above the hard limit is reported as the limit; the
    field is clamped to the same bound on the wire anyway, so fetching further is
    wasted.
    """
    limit = _COUNT_PAGE
    last_raw = 0
    while True:
        rows = lister(limit)
        got = len(rows)  # RAW rows fetched, BEFORE any predicate filtering.
        matched = got if predicate is None else sum(1 for r in rows if predicate(r))
        if matched >= _COUNT_HARD_LIMIT:
            return _COUNT_HARD_LIMIT
        if got < limit:
            # Fewer RAW rows than requested means the brain is exhausted: this is
            # the true end of data. Decided on the raw count so a predicate that
            # discards rows cannot make a full page look short and stop us early.
            return matched
        if got == last_raw:
            # The brain returned a full page but no new RAW rows beyond the
            # previous request: it has hit its own internal cap (or a list clamp).
            # Honest stop. The exact count_* path avoids this; see the docstring.
            return matched
        if limit >= _COUNT_HARD_LIMIT:
            # The caller has observed the largest raw prefix we are willing to
            # request. A sparse predicate can keep matched far below the hard
            # cap even when raw history is huge; do not keep growing the raw
            # request past the same ceiling we apply to reported fields.
            return matched
        last_raw = got
        limit += _COUNT_PAGE


# Agent-authorship signals, mirrored from fleet_brain.store. The poller stores
# EVERY PR from `gh pr list`, including operator- and bot-opened PRs, so the proof
# counter must restrict the PR counts to agent-authored rows: those carrying the
# ``agent:authored`` provenance label (set on PR open) OR pushed from an agent
# branch prefix. These are used only by the LIST FALLBACK path here; the real
# brain filters in SQL via count_github_items(authored_only=True).
_AGENT_AUTHORED_LABEL = "agent:authored"
_AGENT_LABEL_PREFIX = "agent:"
_AGENT_BRANCH_PREFIXES = (
    "alfred/",
    "alfred-nightly/",
    "automerge/",
    "bane/",
    "batman/",
    "damian/",
    "lucius/",
    "nightwing/",
    "rasalghul/",
    "robin/",
)


def _row_is_agent_authored(row: Any) -> bool:
    """True when a github_items row looks agent-authored.

    Used only by the list-fallback path (brains/test-doubles without the SQL
    ``authored_only`` count). Matches the framework provenance label
    ``agent:authored`` in the row's labels, or an agent branch prefix on its head
    ref. Conservative: a row with neither signal is NOT counted, so the public
    counter never claims a PR Alfred did not open.
    """
    labels = getattr(row, "labels", None) or []
    try:
        if _AGENT_AUTHORED_LABEL in labels:
            return True
    except TypeError:
        pass
    head_ref = (getattr(row, "head_ref", None) or "").strip()
    return any(head_ref.startswith(prefix) for prefix in _AGENT_BRANCH_PREFIXES)


def _row_is_agent_labeled(row: Any) -> bool:
    """True when a github_items row carries any ``agent:*`` label."""

    labels = getattr(row, "labels", None) or []
    try:
        return any(str(label).startswith(_AGENT_LABEL_PREFIX) for label in labels)
    except TypeError:
        return False


def _count_github_items(
    brain: Any,
    *,
    authored_only: bool = False,
    agent_labeled_only: bool = False,
    **filters: Any,
) -> int:
    """Exact count of github_items, preferring the brain's COUNT(*) method.

    Uses ``brain.count_github_items(**filters)`` when available (a SQL
    ``COUNT(*)`` that is NOT bounded by the list 500-row clamp, so a busy install
    with thousands of PRs is counted honestly). Falls back to the paginating
    ``_count_rows`` over ``list_github_items`` only for brains/test-doubles that
    do not expose the count method. The result is bounded by ``_COUNT_HARD_LIMIT``
    so a runaway brain can never blow the field past the wire clamp.

    When ``authored_only`` is set, the count is restricted to agent-authored
    rows. The real brain does this in SQL (``count_github_items(authored_only=
    True)``); brains/test-doubles that lack the flag are filtered row-by-row via
    ``_row_is_agent_authored`` on the list-fallback path, so the public counter
    never reports PRs the fleet did not open regardless of brain vintage.

    When ``agent_labeled_only`` is set, the count is restricted to rows carrying
    any ``agent:*`` label. This is the issue-count signal; issues do not have a
    PR head branch, and they normally carry role labels rather than
    ``agent:authored``.
    """
    counter = getattr(brain, "count_github_items", None)
    predicate = _row_is_agent_authored if authored_only else None
    if agent_labeled_only:
        predicate = _row_is_agent_labeled
    if callable(counter):
        try:
            total = int(
                counter(
                    authored_only=authored_only,
                    agent_labeled_only=agent_labeled_only,
                    **filters,
                )
            )
        except TypeError:
            # Older brain whose count_github_items predates authored_only or
            # agent_labeled_only. Fall through to the list-based, row-filtered
            # fallback rather than overcounting.
            if predicate is not None:
                return _count_rows(
                    lambda n: brain.list_github_items(limit=n, **filters),
                    predicate,
                )
            total = int(counter(**filters))
        return total if total < _COUNT_HARD_LIMIT else _COUNT_HARD_LIMIT
    if predicate is not None:
        # No SQL COUNT path on this brain: page the list and filter via the
        # predicate. Continuation is decided on the raw fetched count, so the
        # discarded operator rows cannot make a full page look short and stop the
        # count before every authored row is seen.
        return _count_rows(
            lambda n: brain.list_github_items(limit=n, **filters),
            predicate,
        )
    return _count_rows(lambda n: brain.list_github_items(limit=n, **filters))


def _count_file_touches(brain: Any, **filters: Any) -> int:
    """Exact count of file_touches, preferring the brain's COUNT(*) method.

    Same contract as ``_count_github_items``: prefer ``count_file_touches`` (a
    SQL ``COUNT(*)`` unbounded by the list cap), fall back to pagination for
    brains that lack it, bounded by ``_COUNT_HARD_LIMIT``.
    """
    counter = getattr(brain, "count_file_touches", None)
    if callable(counter):
        total = int(counter(**filters))
        return total if total < _COUNT_HARD_LIMIT else _COUNT_HARD_LIMIT
    return _count_rows(lambda n: brain.list_file_touches(limit=n, **filters))


def derive_counts(brain: Any) -> TelemetryCounts:
    """Roll the local fleet-brain rows up into anonymous aggregate counts.

    Pure read: queries the brain, returns counts, touches nothing else. Counts
    are CUMULATIVE LIFETIME totals (the Worker treats them as such, latest-wins
    per install, see the module docstring), so they must reflect every row the
    brain holds, not a truncated page. Counting uses the brain's exact
    ``count_*`` methods (a SQL ``COUNT(*)``) via ``_count_github_items`` /
    ``_count_file_touches`` rather than ``len()`` of a ``list_*`` fetch: the list
    methods CLAMP ``limit`` to 500, so a busy install with thousands of PRs would
    otherwise freeze every total at 500. Brains that predate the count methods
    fall back to paginating ``list_*`` (honest up to the list clamp).

    Derivation, with an honest mapping to what the brain actually stores:

      prs_opened   distinct AGENT-AUTHORED PRs the brain has cached (github_items
                   where kind == "pr" AND the row is agent-authored: it carries
                   the ``agent:authored`` provenance label or an agent branch
                   prefix). The poller caches EVERY PR from ``gh pr list``, not
                   just Alfred's, so this filter is what keeps the public counter
                   from claiming PRs the fleet did not open.
      prs_merged   that subset whose state == "merged" (counted with a server-
                   side state filter, so it never undercounts behind a page cap).
      prs_reviewed that subset whose state is terminal (merged or closed); a PR
                   reaching a terminal state went through review in Alfred's
                   flow. Conservative: never exceeds prs_opened.
      issues_opened rows in github_items where kind == "issue" and the labels
                   include any ``agent:*`` label.
      issues_closed that subset whose state == "closed".
      files_changed a file-delta proxy: the count of file_touches rows (one per
                   repo file an agent added/modified). The brain stores file
                   touches, not true line counts.
      lines_changed true changed-line totals when available. Today the local
                   brain does not store per-line additions/deletions, so this is
                   reported as 0 until that signal exists locally.
      loc_added    legacy wire alias for files_changed.

    Any query failure yields zeroes for the affected fields and marks the read
    incomplete rather than raising. ``report_once`` skips posting incomplete
    reads so a temporary local brain failure cannot overwrite a previous
    non-zero public contribution with fallback zeroes.

    The base ``prs_opened`` query is load-bearing: ``prs_merged`` and
    ``prs_reviewed`` are defined as subsets of it, so a FAILED base query (which
    leaves ``prs_opened`` at 0) must suppress the dependent counts. Otherwise the
    state-filtered queries, which can still succeed, would yield the impossible
    ``prs_opened:0, prs_merged:N``. We track that failure explicitly (a real zero
    is "the brain holds no PRs"; a failure is "we could not read PRs at all") and
    zero the dependents on failure rather than emitting contradictory data.
    """
    prs_opened = 0
    prs_merged = 0
    prs_reviewed = 0
    issues_opened = 0
    issues_closed = 0
    files_changed = 0
    lines_changed = 0
    loc_added = 0
    read_complete = True

    # Distinguish "the brain genuinely has 0 PRs" from "the base PR query
    # failed". On failure, prs_opened stays 0 AND we suppress the dependent
    # merged/reviewed counts so we never report 0 opened with N merged.
    prs_opened_failed = False
    try:
        prs_opened = _count_github_items(brain, kind="pr", authored_only=True)
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        prs_opened_failed = True
        read_complete = False
        logger.debug("telemetry: PR-opened count derivation failed: %s", exc)

    # Count merged and terminal (merged|closed) PRs with a state filter so each
    # is an accurate total (an exact COUNT(*) past the 500-row list cap) rather
    # than a sample of the first page. Fall back to an in-memory tally of a
    # single page if the brain does not accept `state`. Skipped entirely when the
    # base query failed: dependent counts are meaningless without a trustworthy
    # opened total.
    if not prs_opened_failed:
        try:
            prs_merged = _count_github_items(brain, kind="pr", state="merged", authored_only=True)
            closed = _count_github_items(brain, kind="pr", state="closed", authored_only=True)
            prs_reviewed = prs_merged + closed
        except TypeError:
            # Brain's list/count github items has no `state` kwarg: derive from
            # one page (bounded by the hard limit), still restricted to
            # agent-authored rows so the dependents match the opened total.
            try:
                prs = [
                    p
                    for p in brain.list_github_items(kind="pr", limit=_COUNT_HARD_LIMIT)
                    if _row_is_agent_authored(p)
                ]
                prs_merged = sum(1 for p in prs if getattr(p, "state", None) == "merged")
                prs_reviewed = sum(
                    1 for p in prs if getattr(p, "state", None) in ("merged", "closed")
                )
            except Exception as exc:  # fail-soft by contract
                logger.debug("telemetry: PR-state count derivation failed: %s", exc)
                read_complete = False
                prs_merged = 0
                prs_reviewed = 0
        except Exception as exc:  # fail-soft by contract: never raise on a bad read
            logger.debug("telemetry: PR-state count derivation failed: %s", exc)
            read_complete = False
            prs_merged = 0
            prs_reviewed = 0

    # prs_merged and prs_reviewed are defined as SUBSETS of prs_opened, so neither
    # may exceed it. Clamp unconditionally, INCLUDING when prs_opened == 0: a
    # zero opened with a non-zero dependent is exactly the invariant violation we
    # must not store. That state is reachable without the base query failing,
    # e.g. the base count races a GitHub poller mid-write and returns 0 while the
    # state-filtered counts still see rows, or an older brain returns
    # state-filtered counts after the base count came back 0. A truthy
    # `prs_opened` guard would skip the clamp precisely in that opened==0 case and
    # let prs_opened:0 ship alongside prs_merged>0, so the guard is dropped.
    if prs_reviewed > prs_opened:
        prs_reviewed = prs_opened
    if prs_merged > prs_opened:
        prs_merged = prs_opened

    try:
        issues_opened = _count_github_items(
            brain,
            kind="issue",
            agent_labeled_only=True,
        )
        issues_closed = _count_github_items(
            brain,
            kind="issue",
            state="closed",
            agent_labeled_only=True,
        )
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        read_complete = False
        logger.debug("telemetry: issue count derivation failed: %s", exc)

    if issues_closed > issues_opened:
        issues_closed = issues_opened

    try:
        files_changed = _count_file_touches(brain)
        loc_added = files_changed
    except Exception as exc:  # fail-soft by contract: never raise on a bad read
        read_complete = False
        logger.debug("telemetry: file-touch count derivation failed: %s", exc)

    return TelemetryCounts(
        prs_opened=_clamp(prs_opened),
        prs_merged=_clamp(prs_merged),
        prs_reviewed=_clamp(prs_reviewed),
        issues_opened=_clamp(issues_opened),
        issues_closed=_clamp(issues_closed),
        files_changed=_clamp(files_changed),
        lines_changed=_clamp(lines_changed),
        loc_added=_clamp(loc_added),
        read_complete=read_complete,
    )


def build_payload(install_id: str, counts: TelemetryCounts, period: str) -> dict[str, Any]:
    """Assemble the exact JSON body that goes on the wire. No extra keys."""
    files_changed = counts.files_changed if counts.files_changed is not None else counts.loc_added
    return {
        "install_id": install_id,
        "period": period,
        "prs_opened": _clamp(counts.prs_opened),
        "prs_merged": _clamp(counts.prs_merged),
        "prs_reviewed": _clamp(counts.prs_reviewed),
        "issues_opened": _clamp(counts.issues_opened),
        "issues_closed": _clamp(counts.issues_closed),
        "files_changed": _clamp(files_changed),
        "lines_changed": _clamp(counts.lines_changed),
        "loc_added": _clamp(counts.loc_added),
    }


def build_tombstone_payload(install_id: str, period: str | None = None) -> dict[str, Any]:
    """Build a request asking the collector to remove this install's record."""
    return {
        "install_id": install_id,
        "period": period or _LIFETIME_PERIOD,
        "tombstone": True,
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
      ``disabled``        the master switch is explicitly off. Nothing happened.
      ``no_url``          enabled but ``ALFRED_TELEMETRY_URL`` is unset. No-op.
      ``no_install_id``   enabled, but the install id could not be persisted, so
                          we skip the report rather than POST with an ephemeral
                          id that would inflate the distinct-install count.
      ``stale_counts``    enabled, but local count reads were incomplete, so the
                          previous accepted report is left in place.
      ``sent``            payload posted and the server accepted it.
      ``failed``          enabled and attempted, but the post did not succeed.
      ``error``           an unexpected internal error, swallowed.

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
        install_id = load_or_create_persisted_install_id()
        if install_id is None:
            # The install id is not durable (state dir unwritable, read-only FS,
            # etc.). Reporting with an ephemeral id minted per run would make
            # this host look like a new install on every report and inflate the
            # public install count, so skip the report. Next run retries.
            return {"status": "no_install_id", "sent": False}
        period = current_period(now)
        counts = derive_counts(brain)
        if not counts.read_complete:
            return {"status": "stale_counts", "sent": False}
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
                "issues_opened": payload["issues_opened"],
                "issues_closed": payload["issues_closed"],
                "files_changed": payload["files_changed"],
                "lines_changed": payload["lines_changed"],
                "loc_added": payload["loc_added"],
            },
        }
    except Exception as exc:  # fail-soft by contract: a telemetry hiccup is silent
        logger.debug("telemetry: report_once swallowed error: %s", exc)
        return {"status": "error", "sent": False}


def clear_report(
    *,
    env: Mapping[str, str] | None = None,
    install_id: str | None = None,
    poster: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Ask the collector to remove this install's previous contribution.

    This is used by ``alfred telemetry off`` before the local switch is written.
    It never creates an install id: if no durable id exists, there is no known
    remote record to clear.
    """
    source = env if env is not None else os.environ
    url = telemetry_url(source)
    if not url:
        return {"status": "no_url", "sent": False}
    resolved_install_id = install_id or load_persisted_install_id()
    if not resolved_install_id:
        return {"status": "no_install_id", "sent": False}
    try:
        payload = build_tombstone_payload(resolved_install_id)
        token = telemetry_token(source)
        send = poster or (lambda u, p: _post(u, p, token=token))
        ok = bool(send(url, payload))
        return {"status": "sent" if ok else "failed", "sent": ok}
    except Exception as exc:  # fail-soft by contract
        logger.debug("telemetry: clear_report swallowed error: %s", exc)
        return {"status": "error", "sent": False}
