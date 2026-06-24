"""Self-healing core for the autonomous fleet: classify, retry, break, detect.

This module is the single source of truth for turning a raw invocation
outcome into a recovery decision. It owns four cooperating pieces, each
config-driven (env-overridable) and side-effect-free except where noted:

* :class:`FailureClass` + :func:`classify_result` / :func:`classify_exception`:
  map any invocation failure to one of ``TRANSIENT`` / ``FATAL`` /
  ``CAPABILITY`` / ``NONE``. This REUSES the subtype classification that
  :mod:`lib.agent_runner.result` already produces (``error_rate_limit``,
  ``error_overloaded``, ``error_authentication``, ...) rather than
  re-deriving it from raw text.

* :func:`retry_with_backoff` + :func:`compute_backoff_delay`: a small,
  self-contained exponential-backoff helper with MANDATORY full jitter
  that honours a server ``Retry-After`` hint (waits
  ``max(retry_after, backoff)``). Used to retry the SAME engine on a
  TRANSIENT failure a bounded number of times. ``tenacity`` is not a
  dependency, so this stays dependency-free.

* :class:`CircuitBreaker`: a per-engine breaker backed by the on-disk
  state ledger. It trips after N consecutive TRANSIENT failures on an
  engine and pauses calls to THAT engine for a cooldown, so lockstep
  retry loops across parallel workers cannot deepen an active
  rate-limit on the shared provider quota.

* :class:`LoopDetector` + :func:`step_fingerprint`: fingerprint each
  agent step as a stable hash of ``(tool/action, result-preview)``;
  ``N`` identical fingerprints in a row means the agent is stuck in a
  loop and should be stopped and escalated rather than left to spin.

What this module does NOT own:

* Constructing :class:`~lib.agent_runner.result.ClaudeResult` or the
  envelope regexes -> ``result.py``.
* Shelling out to an engine CLI -> ``process.py``.
* The fleet-wide global block / per-agent spend ledger primitives ->
  ``state.py`` (the breaker stores its counters under the same
  ``STATE_ROOT`` tree but is a separate, narrow concern).

All tunables are read at call time from the environment via
``config.env_int`` so a launchd plist or deployment config can retune
behaviour without a redeploy, per the config-driven-tunables rule. When
nothing is failing, none of these paths change existing behaviour: the
classifier returns ``NONE`` for a healthy result, the breaker stays
closed, and the loop detector never trips.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .config import env_int
from .paths import STATE_ROOT

try:  # POSIX-only; the breaker degrades to best-effort on platforms without it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows) fallback
    fcntl = None  # type: ignore[assignment]

# --------------------------------------------------------------------------
# Failure classification: the single source of truth
# --------------------------------------------------------------------------


class FailureClass(StrEnum):
    """How an invocation failed, and therefore how to recover from it.

    * ``NONE``: the call succeeded (or has no failure signal). No
      recovery action.
    * ``TRANSIENT``: a temporary provider/transport problem that the
      same engine is likely to clear on retry (429, 5xx, timeouts,
      connection reset, overload, context-overflow). Retry the SAME
      engine with backoff; never burn the fallback.
    * ``FATAL``: a problem retrying cannot fix (401/403 auth, 422
      bad-request/schema). Surface honestly; never retry, never fall
      back.
    * ``CAPABILITY``: the engine ran and returned cleanly but produced
      nothing useful (no output, parse-failed, max-turns with no
      result). This is the ONLY class that justifies a claude->codex
      fallback: a different engine may have the capability this one
      lacked.
    """

    NONE = "none"
    TRANSIENT = "transient"
    FATAL = "fatal"
    CAPABILITY = "capability"


# The subtype -> class table. Keyed on the subtypes ``result.py`` already
# produces, so classification has exactly one source of truth: the
# envelope classifier decides the subtype, this table decides the
# recovery policy. Any subtype not listed falls through to the
# stop_reason / text heuristics in ``classify_result``.
_SUBTYPE_CLASS: dict[str, FailureClass] = {
    # Provider quota / load / transport: the same engine clears these.
    "error_rate_limit": FailureClass.TRANSIENT,
    "error_overloaded": FailureClass.TRANSIENT,
    "error_timeout": FailureClass.TRANSIENT,
    "error_api": FailureClass.TRANSIENT,
    # Auth is fatal here: the one-shot stale-credential repair already
    # ran upstream in result.py before we ever classify, so a surviving
    # error_authentication means real bad credentials. Surface, do not
    # burn the fallback.
    "error_authentication": FailureClass.FATAL,
    # Budget is a hard wall for THIS provider, not a capability gap.
    # Treat as transient-for-fallback-purposes? No: a daily budget cap
    # will not clear on a short backoff and is not the fallback's job to
    # paper over. It is surfaced like a provider limit (FATAL for this
    # engine's retry loop) and the existing global-block path handles the
    # fleet pause.
    "error_budget": FailureClass.FATAL,
    # The engine ran but gave us nothing usable. Different engine, please.
    "error_max_turns": FailureClass.CAPABILITY,
    "parse-failed": FailureClass.CAPABILITY,
    # The engine got stuck repeating the same step. Retrying the same
    # engine would just spin again; a different engine might not, so this
    # is a capability gap, not a transient transport fault.
    "error_loop_detected": FailureClass.CAPABILITY,
}

# Substrings that, in an error message or result tail, signal a transient
# transport-layer fault even when the subtype is generic. Lower-cased
# before matching. Kept deliberately small and specific.
_TRANSIENT_TEXT_MARKERS: tuple[str, ...] = (
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection error",
    "read timeout",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    " 500 ",
    " 502 ",
    " 503 ",
    " 504 ",
    # Context-window overflow only. A bare ``too long`` is deliberately NOT
    # listed: it also matches non-recoverable env errors like ``argument
    # list too long`` (E2BIG), ``filename too long`` (ENAMETOOLONG), or
    # ``path too long``, which a same-engine retry cannot fix and would just
    # burn the retry budget before surfacing the real cause. The specific
    # context markers below cover the actually-transient overflow case.
    "context length",
    "context window",
    "prompt is too long",
    "maximum context",
)

# Substrings that signal a fatal request/schema problem.
_FATAL_TEXT_MARKERS: tuple[str, ...] = (
    "http 401",
    "http 403",
    "http 422",
    "unprocessable entity",
    "invalid_request_error",
    "permission denied",
    "forbidden",
)


def classify_result(result: Any) -> FailureClass:
    """Classify a :class:`ClaudeResult`-shaped object into a FailureClass.

    Reuses the already-computed ``subtype`` as the primary signal (the
    envelope classifier in ``result.py`` is the one source of truth for
    what kind of error a response carries). Falls back to ``stop_reason``
    and a small text-marker scan only for subtypes the table does not
    cover.

    Args:
        result: any object exposing ``success``, ``subtype``,
            ``stop_reason``, ``error_message``, and ``result_text``
            (i.e. a ``ClaudeResult``).

    Returns:
        The :class:`FailureClass`. ``NONE`` when the result succeeded.
    """
    if getattr(result, "success", False):
        return FailureClass.NONE

    subtype = str(getattr(result, "subtype", "") or "")
    mapped = _SUBTYPE_CLASS.get(subtype)
    if mapped is not None:
        return mapped

    stop_reason = getattr(result, "stop_reason", None)
    if stop_reason == "aborted":
        # Cancelled / killed / wall-clock timeout: a fresh attempt on the
        # same engine is the right move.
        return FailureClass.TRANSIENT

    haystack = " ".join(
        str(getattr(result, attr, "") or "") for attr in ("error_message", "result_text")
    ).lower()
    text_class = _classify_text(haystack)
    if text_class is not FailureClass.NONE:
        return text_class

    # A failed result we cannot place is treated as a capability gap:
    # better to try the other engine once than to hammer the same one.
    return FailureClass.CAPABILITY


def classify_exception(exc: BaseException) -> FailureClass:
    """Classify a raised exception from an invocation into a FailureClass.

    Network/transport exceptions raised before a ``ClaudeResult`` is
    built (e.g. an httpx transport error, a socket reset) are TRANSIENT;
    everything else we cannot place defaults to TRANSIENT as well, since
    an unexpected raise on one attempt is usually worth one more try on
    the same engine before giving up. Callers that want a hard stop on a
    specific exception type should not route it through here.
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    fatal = _classify_text(text)
    if fatal is FailureClass.FATAL:
        return FailureClass.FATAL
    return FailureClass.TRANSIENT


def _classify_text(haystack: str) -> FailureClass:
    """Scan a lower-cased haystack for transient/fatal transport markers."""
    for marker in _FATAL_TEXT_MARKERS:
        if marker in haystack:
            return FailureClass.FATAL
    for marker in _TRANSIENT_TEXT_MARKERS:
        if marker in haystack:
            return FailureClass.TRANSIENT
    return FailureClass.NONE


# --------------------------------------------------------------------------
# Retry-After extraction
# --------------------------------------------------------------------------


def retry_after_seconds(result: Any) -> float | None:
    """Best-effort extraction of a server ``Retry-After`` hint, in seconds.

    Looks in the result's ``raw`` dict for the common shapes a provider
    or CLI surfaces a retry hint in (a ``retry_after`` field, a
    ``Retry-After`` header echoed into the JSON, or an
    ``error.retry_after``). Returns ``None`` when no hint is present.
    The value is clamped to a sane ceiling so a hostile or buggy header
    cannot park a worker for hours.
    """
    raw = getattr(result, "raw", None)
    if not isinstance(raw, dict):
        return None

    candidates: list[Any] = []
    for key in ("retry_after", "retryAfter", "Retry-After", "retry-after"):
        if key in raw:
            candidates.append(raw[key])
    headers = raw.get("headers")
    if isinstance(headers, dict):
        for key in ("retry-after", "Retry-After"):
            if key in headers:
                candidates.append(headers[key])
    err = raw.get("error")
    if isinstance(err, dict):
        for key in ("retry_after", "retryAfter"):
            if key in err:
                candidates.append(err[key])

    ceiling = float(env_int("ALFRED_RETRY_AFTER_MAX_SECONDS", 300, minimum=1, maximum=86_400))
    for cand in candidates:
        secs = _coerce_seconds(cand)
        if secs is not None:
            return max(0.0, min(secs, ceiling))
    return None


def _coerce_seconds(value: Any) -> float | None:
    """Coerce a Retry-After value (numeric seconds or HTTP-date) to seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    # HTTP-date form: compute the delta from now.
    for fmt in ("%a, %d %b %Y %H:%M:%S GMT", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            when = datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        delta = (when - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    return None


# --------------------------------------------------------------------------
# Backoff with mandatory full jitter
# --------------------------------------------------------------------------


def compute_backoff_delay(
    attempt: int,
    *,
    base: float | None = None,
    cap: float | None = None,
    retry_after: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Return the seconds to wait before retry ``attempt`` (1-indexed).

    Exponential backoff with FULL JITTER: the raw exponential window is
    ``min(cap, base * 2**(attempt-1))`` and the actual delay is drawn
    uniformly from ``[0, window]``. When a server ``Retry-After`` hint
    is supplied we honour it as a floor: the returned delay is
    ``max(retry_after, jittered_backoff)``.

    Jitter is mandatory and not optional: it is what stops parallel
    workers sharing one provider quota from retrying in lockstep and
    deepening a rate-limit.

    Args:
        attempt: the 1-indexed retry number (1 = first retry).
        base: base delay in seconds (defaults to ``ALFRED_RETRY_BASE_SECONDS``).
        cap: max exponential window (defaults to ``ALFRED_RETRY_CAP_SECONDS``).
        retry_after: optional server hint to honour as a floor.
        rng: optional ``random.Random`` for deterministic tests.

    Returns:
        A non-negative float number of seconds to sleep.
    """
    if base is None:
        base = float(env_int("ALFRED_RETRY_BASE_SECONDS", 2, minimum=1, maximum=600))
    if cap is None:
        cap = float(env_int("ALFRED_RETRY_CAP_SECONDS", 60, minimum=1, maximum=3_600))
    safe_attempt = max(1, attempt)
    # Guard the shift against absurd attempt counts.
    exp = min(cap, base * (2.0 ** min(safe_attempt - 1, 16)))
    draw = (rng or random).uniform(0.0, max(0.0, exp))
    if retry_after is not None and retry_after > 0:
        return max(float(retry_after), draw)
    return draw


T = TypeVar("T")


def retry_with_backoff(
    invoke: Callable[[], T],
    *,
    classify: Callable[[T], FailureClass],
    max_retries: int | None = None,
    retry_after_of: Callable[[T], float | None] | None = None,
    on_retry: Callable[[int, float, T], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Invoke ``invoke`` and retry the SAME call on TRANSIENT failures.

    Only :data:`FailureClass.TRANSIENT` outcomes are retried. ``FATAL``,
    ``CAPABILITY``, and ``NONE`` (success) return immediately so the
    caller can apply its own policy (surface, or fall back to codex).
    This is the seam that makes the fallback fire on capability failures
    only: transient failures are absorbed here, never handed to the
    fallback engine.

    Args:
        invoke: zero-arg callable performing one attempt.
        classify: maps an outcome to a :class:`FailureClass`.
        max_retries: extra attempts after the first (defaults to
            ``ALFRED_TRANSIENT_MAX_RETRIES``). ``0`` disables retry.
        retry_after_of: optional callable extracting a server retry hint
            from the outcome, honoured as a backoff floor.
        on_retry: optional callback ``(attempt, delay, outcome)`` fired
            before each sleep; useful for an event log line.
        sleep: injectable sleep (tests pass a no-op recorder).
        rng: injectable RNG for deterministic backoff in tests.

    Returns:
        The final outcome (success, or the last failure once retries are
        exhausted or the failure is non-transient).
    """
    if max_retries is None:
        max_retries = env_int("ALFRED_TRANSIENT_MAX_RETRIES", 3, minimum=0, maximum=20)

    outcome = invoke()
    attempt = 0
    while classify(outcome) is FailureClass.TRANSIENT and attempt < max_retries:
        attempt += 1
        hint = retry_after_of(outcome) if retry_after_of else None
        delay = compute_backoff_delay(attempt, retry_after=hint, rng=rng)
        if on_retry:
            on_retry(attempt, delay, outcome)
        sleep(delay)
        outcome = invoke()
    return outcome


# --------------------------------------------------------------------------
# Per-engine circuit breaker
# --------------------------------------------------------------------------


@dataclass
class BreakerStatus:
    """Snapshot of a breaker's decision for one engine."""

    open: bool
    engine: str
    consecutive: int
    until: str | None = None
    reason: str | None = None


class CircuitBreaker:
    """Per-engine breaker backed by the on-disk state ledger.

    Trips ``open`` after ``threshold`` consecutive TRANSIENT failures on
    an engine and pauses calls to THAT engine for ``cooldown`` seconds.
    A single success (or any non-transient outcome) resets the streak and
    closes the breaker. State lives under
    ``${STATE_ROOT}/_breaker/<engine>.json`` so it survives across
    firings and is shared by every worker on the host, which is exactly
    what lets the fleet auto-throttle a shared provider quota instead of
    needing a human to scale workers down.

    The breaker is deliberately conservative: it only counts TRANSIENT
    failures (a capability gap or a one-off fatal must not trip it), and
    it never blocks the FIRST call after a cooldown expires (half-open),
    so a recovered provider resumes immediately.
    """

    def __init__(
        self,
        engine: str,
        *,
        threshold: int | None = None,
        cooldown_seconds: int | None = None,
        root: Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.engine = engine.strip().lower() or "unknown"
        self._threshold = (
            threshold
            if threshold is not None
            else env_int("ALFRED_BREAKER_THRESHOLD", 5, minimum=1, maximum=100)
        )
        self._cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else env_int("ALFRED_BREAKER_COOLDOWN_SECONDS", 300, minimum=1, maximum=86_400)
        )
        base = root if root is not None else (STATE_ROOT / "_breaker")
        self._path = base / f"{self.engine}.json"
        self._now = now or (lambda: datetime.now(UTC))

    # -- persistence -------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self):
        """Hold an exclusive cross-process lock for a read-modify-write.

        The breaker counter is incremented from every worker on the host
        sharing one engine file, so the load -> increment -> store sequence
        must be serialized or concurrent writers race and collapse the
        count (each reads the same value, each writes value+1, so N
        failures advance the counter by 1 and the breaker trips far too
        late). We take an ``fcntl`` exclusive lock on a sidecar ``.lock``
        file for the duration of the update. On a platform without
        ``fcntl`` the breaker degrades to best-effort (the unique temp name
        still prevents corruption, just not a strict count).
        """
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _store(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a per-writer-unique temp file before the atomic rename.
        # Every worker on the host shares this engine's target path, so a
        # FIXED ``.tmp`` name would let two concurrent writers clobber each
        # other's bytes before either renames, silently dropping one of the
        # two increments and tripping the breaker later than intended. A
        # pid + uuid token makes each writer's temp file private, so the
        # only shared step is the atomic ``replace``.
        tmp = self._path.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        finally:
            # If the rename never happened (an error between write and
            # replace), do not leave a stray temp file behind.
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

    # -- queries -----------------------------------------------------------

    def status(self) -> BreakerStatus:
        """Return whether the breaker is currently open for this engine."""
        data = self._load()
        until_raw = data.get("open_until")
        consecutive = int(data.get("consecutive", 0) or 0)
        if until_raw:
            until = _parse_iso(until_raw)
            if until is not None and self._now() < until:
                return BreakerStatus(
                    open=True,
                    engine=self.engine,
                    consecutive=consecutive,
                    until=until_raw,
                    reason=data.get("reason"),
                )
        return BreakerStatus(open=False, engine=self.engine, consecutive=consecutive)

    def is_open(self) -> bool:
        """True when calls to this engine are currently paused."""
        return self.status().open

    # -- transitions -------------------------------------------------------

    def record_transient_failure(self, *, reason: str | None = None) -> BreakerStatus:
        """Count one transient failure; trip the breaker at the threshold.

        The whole load -> increment -> store runs under an exclusive
        cross-process lock so concurrent workers on the host each see the
        other's increment rather than racing on a stale read.
        """
        with self._locked():
            data = self._load()
            consecutive = int(data.get("consecutive", 0) or 0) + 1
            data["consecutive"] = consecutive
            data["last_failure"] = self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
            if reason:
                data["reason"] = reason
            opened = False
            if consecutive >= self._threshold:
                until = self._now() + timedelta(seconds=self._cooldown)
                data["open_until"] = until.strftime("%Y-%m-%dT%H:%M:%SZ")
                opened = True
            with contextlib.suppress(OSError):
                self._store(data)
        return BreakerStatus(
            open=opened,
            engine=self.engine,
            consecutive=consecutive,
            until=data.get("open_until") if opened else None,
            reason=reason,
        )

    def record_success(self) -> None:
        """Reset the streak and close the breaker after a clean call."""
        if not self._path.exists():
            return
        with self._locked(), contextlib.suppress(OSError):
            self._store({"consecutive": 0})

    def reset(self) -> None:
        """Forget all breaker state for this engine (operator resume path)."""
        with contextlib.suppress(OSError):
            if self._path.exists():
                self._path.unlink()
        with contextlib.suppress(OSError):
            lock_path = self._path.with_suffix(".lock")
            if lock_path.exists():
                lock_path.unlink()


# --------------------------------------------------------------------------
# Loop-fingerprint detection
# --------------------------------------------------------------------------


def step_fingerprint(action: str, result_preview: str, *, preview_chars: int = 120) -> str:
    """Return a stable short hash of one agent step.

    The fingerprint is over ``(action, normalized-result-preview)``.
    Whitespace is collapsed and the preview truncated so that two steps
    that did the same thing and got materially the same result hash
    identically even when surrounding text jitters slightly.

    Args:
        action: the tool / action name the step invoked.
        result_preview: a preview of the step's result text.
        preview_chars: how many chars of the preview to fold in.

    Returns:
        A 16-hex-char stable fingerprint.
    """
    norm_action = " ".join(str(action).split()).lower()
    norm_preview = " ".join(str(result_preview).split())[:preview_chars]
    digest = hashlib.sha256(f"{norm_action}\x00{norm_preview}".encode()).hexdigest()
    return digest[:16]


class LoopDetector:
    """Detect a stuck agent by spotting repeated identical step fingerprints.

    Feed each step's ``(action, result_preview)`` via :meth:`observe`.
    When the last ``window`` fingerprints are all identical, the agent is
    spinning on the same no-progress action and :meth:`observe` returns
    ``True`` so the caller can stop and escalate instead of burning the
    rest of the step budget.

    Independently tracks a hard per-task step ceiling: :meth:`observe`
    also returns ``True`` once ``max_steps`` observations have been made,
    so a task that never repeats but also never finishes still gets cut.
    """

    def __init__(
        self,
        *,
        window: int | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.window = (
            window
            if window is not None
            else env_int("ALFRED_LOOP_WINDOW", 3, minimum=2, maximum=50)
        )
        self.max_steps = (
            max_steps
            if max_steps is not None
            else env_int("ALFRED_MAX_STEPS", 200, minimum=1, maximum=100_000)
        )
        self._recent: deque[str] = deque(maxlen=self.window)
        self._count = 0
        self.tripped_reason: str | None = None

    def observe(self, action: str, result_preview: str) -> bool:
        """Record one step; return True if the task should stop now.

        Returns ``True`` on either trip condition (loop or step ceiling)
        and latches :attr:`tripped_reason` for the caller to surface.
        """
        self._count += 1
        fp = step_fingerprint(action, result_preview)
        self._recent.append(fp)

        if self._count >= self.max_steps:
            self.tripped_reason = f"step ceiling reached ({self.max_steps} steps)"
            return True

        if len(self._recent) == self.window and len(set(self._recent)) == 1:
            self.tripped_reason = (
                f"loop detected: {self.window} identical steps in a row "
                f"(fingerprint {self._recent[-1]})"
            )
            return True
        return False

    @property
    def steps(self) -> int:
        """Number of steps observed so far."""
        return self._count


def _parse_iso(value: str) -> datetime | None:
    """Parse a ``YYYY-MM-DDTHH:MM:SSZ`` timestamp, or return None."""
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
