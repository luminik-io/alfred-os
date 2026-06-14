"""On-disk state: locks, spend ledger, event log, fleet flags.

This module owns everything that survives across firings on disk under
``${ALFRED_HOME}/state/``:

* :class:`AgentLock` and :func:`with_lock` — mkdir-atomic per-agent mutex
  with PID identity verification.
* :class:`SpendState` — per-agent per-day firings / turns / cost /
  failure ledger.
* :class:`EventLog` — append-only JSONL per-firing event stream.
* :func:`is_globally_blocked` / :func:`set_global_block` — fleet-wide
  rate-limit block recorded under ``${STATE_ROOT}/global-blocked-until.json``.
* Fleet enable/disable: :func:`enable_agent`, :func:`disable_agent`,
  :func:`is_agent_enabled`, :func:`list_enabled_agents`.

What this module does NOT own:

* gh-mediated issue claim/release state machine -> ``github.py``.
* The ``ClaudeResult`` shape consumed by :func:`maybe_set_global_block_for_result`
  -> ``result.py``.
* Subprocess invocation -> ``process.py``.

All state-mutating functions are stateless processes' best friend:
write-then-rename for atomicity, ``contextlib.suppress`` around cleanup
so a partial write never wedges a firing.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import process as _process
from .agent_events import (  # noqa: F401  (re-exported for callers)
    Event,
    EventType,
    UnknownEventType,
)
from .config import PROVIDER_LIMIT_SUBTYPES, dry_run_log, is_dry_run
from .paths import (
    FLEET_ENABLED_FILE,
    GLOBAL_BLOCKED_FILE,
    STATE_ROOT,
    today_str,
)


def _resolve_pid_start_key(pid: int) -> str:
    """Call :func:`process.pid_start_key`, honouring any test patch.

    Tests monkeypatch ``agent_runner.pid_start_key``; the package's
    ``__setattr__`` hook propagates that to ``process.pid_start_key``,
    so a fresh dotted lookup picks the override up at call time.
    """
    return _process.pid_start_key(pid)


# --------------------------------------------------------------------------
# Global block (fleet-wide rate-limit pause)
# --------------------------------------------------------------------------


def is_globally_blocked() -> str | None:
    """Return reason string if a global rate-limit block is active, else None."""
    if not GLOBAL_BLOCKED_FILE.exists():
        return None
    try:
        data = json.loads(GLOBAL_BLOCKED_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return None
    until = data.get("until", "")
    try:
        exp = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    if datetime.now(UTC) >= exp:
        with contextlib.suppress(OSError):
            GLOBAL_BLOCKED_FILE.unlink()
        return None
    return f"global rate-limit block until {until} (reason: {data.get('reason', 'unknown')})"


def set_global_block(hours: int, reason: str) -> str:
    """Set a global rate-limit block. Returns the until-iso string.

    Dry-run never writes the fleet-wide block; the operator still gets
    the expected until-string so happy-path messaging renders.
    """
    until = (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if is_dry_run():
        dry_run_log(
            "block",
            f"would set fleet-wide global block until {until} (reason: {reason}); skipped",
        )
        return until
    GLOBAL_BLOCKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_BLOCKED_FILE.write_text(json.dumps({"until": until, "reason": reason}))
    return until


def maybe_set_global_block_for_result(
    agent: str,
    result: Any,
    *,
    hours: int = 1,
    engine_used: str | None = "claude",
) -> str | None:
    """Trip the fleet-wide block when ``result`` indicates a provider limit.

    Only fires for engines whose limit headers we know how to interpret
    (currently ``claude``); Codex limits are silent fallbacks instead.
    Returns the ``until`` string when a block was set, else ``None``.
    """
    if engine_used != "claude":
        return None
    subtype = getattr(result, "subtype", "")
    if subtype not in PROVIDER_LIMIT_SUBTYPES:
        return None
    return set_global_block(hours=hours, reason=f"{agent}-{subtype}")


# --------------------------------------------------------------------------
# Event log
# --------------------------------------------------------------------------


class EventLog:
    """Append-only JSONL log for a single firing - typed + sequenced.

    Every record is a typed :class:`agent_events.Event` envelope carrying a
    monotonic per-firing ``seq`` (1, 2, 3, ...), a UTC-ISO ``ts``, a closed
    ``type`` (rejected at write time if it is not in
    :class:`agent_events.EventType`), stable ``agent`` / ``firing_id`` identity,
    an optional ``stage``, and a validated payload. The serialized line keeps
    the legacy top-level ``event`` field (== ``type``) so existing consumers
    keep rendering.

    Usage::

        events = EventLog(agent="lucius", firing_id="2026-04-29-1647-bf3a")
        events.emit("preflight_passed")
        events.emit("issue_picked", repo="myorg/backend", number=275)
        events.emit("pr_opened", url=pr_url, files_changed=12)

    ``append()`` is the typed primitive; ``emit()`` is the thin string-keyed
    wrapper the existing call sites use. Both validate the event type against
    the closed set and stamp ``seq`` monotonically. ``seq`` survives a process
    restart: on init the log reads the max ``seq`` already on disk for the
    firing and continues from there.
    """

    def __init__(
        self,
        agent: str,
        firing_id: str | None = None,
        path: Path | None = None,
        *,
        stage: str | None = None,
    ) -> None:
        self.agent = agent
        if firing_id is None:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            firing_id = f"{stamp}-{secrets.token_hex(2)}"
        self.firing_id = firing_id
        # Default stage/node identity stamped onto every event unless an emit
        # call overrides it. ``None`` means "no stage" and is omitted on disk.
        self.stage = stage
        if path is None:
            d = STATE_ROOT / agent / "events"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{firing_id}.jsonl"
        self.path = path
        # Restart-safe monotonic counter: seed from the max ``seq`` already on
        # disk so a second EventLog for the same firing (e.g. after a process
        # restart) continues the sequence instead of restarting at 1.
        self._seq = self._read_max_seq()

    def _read_max_seq(self) -> int:
        """Return the highest ``seq`` already recorded for this firing, or 0.

        Best-effort: a missing file, an unreadable file, or torn / legacy lines
        without a ``seq`` are tolerated (legacy lines simply do not advance the
        counter). This is what makes the counter survive a process restart.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return 0
        max_seq = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            seq = obj.get("seq")
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
        return max_seq

    def append(
        self,
        event_type: str | EventType,
        *,
        stage: str | None = None,
        **payload: Any,
    ) -> None:
        """Append one typed, sequenced event. Stamps ``seq`` monotonically,
        validates the type against the closed set, serializes the envelope and
        fsyncs so a crash mid-firing cannot lose an acknowledged event.

        Raises :class:`agent_events.UnknownEventType` for an out-of-set type and
        :class:`agent_events.EventPayloadError` for a missing required payload
        key. These are programmer errors (a typo or a wrong call site), so they
        are surfaced loudly rather than swallowed - that is the closed-set
        guarantee. I/O errors, by contrast, never kill a firing: they print to
        stderr and continue.
        """
        # Build + validate the envelope at the NEXT seq value, but only commit
        # the counter after validation succeeds, so a rejected event (unknown
        # type, missing required key) does not burn a seq number or leave a gap.
        event = Event.create(
            seq=self._seq + 1,
            agent=self.agent,
            firing_id=self.firing_id,
            event_type=event_type,
            payload=payload,
            stage=stage if stage is not None else self.stage,
        )
        self._seq += 1
        record = event.to_record()
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            print(f"[event-log] write failed: {e}", file=sys.stderr)

    def emit(self, event: str | EventType, **fields: Any) -> None:
        """Thin typed wrapper over :meth:`append` for the string-keyed call
        sites. Pulls an optional ``stage`` out of the freeform kwargs so a call
        site can stamp stage identity without changing the positional API.

        Unknown event types still raise (so a typo is caught), but a broken
        event-log *write* never kills a firing - that contract is preserved
        inside :meth:`append`.
        """
        stage = fields.pop("stage", None)
        self.append(event, stage=stage, **fields)


# --------------------------------------------------------------------------
# Per-agent lock
# --------------------------------------------------------------------------

_LOCK_GRACE_SECONDS = 60


@dataclass
class AgentLock:
    """Mutex via ``mkdir(2)`` atomicity. Auto-released on process exit."""

    name: str
    _lock_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self._lock_dir = Path(f"/tmp/agent-lock-{self.name}")

    def _lock_age_seconds(self) -> float | None:
        try:
            return max(0.0, time.time() - self._lock_dir.stat().st_mtime)
        except OSError:
            return None

    def acquire(self) -> bool:
        """Try to acquire the lock. Returns True on success."""
        try:
            self._lock_dir.mkdir(exist_ok=False)
        except FileExistsError:
            pid_file = self._lock_dir / "pid"
            try:
                old_pid = int(pid_file.read_text().strip())
                os.kill(old_pid, 0)
                identity_status = lock_pid_identity_status(
                    self._lock_dir,
                    old_pid,
                    expected_agent=self.name,
                )
                if identity_status is True:
                    return False
                lock_age = self._lock_age_seconds()
                if identity_status is None:
                    return False
                if lock_age is not None and lock_age <= _LOCK_GRACE_SECONDS:
                    return False
            except (
                FileNotFoundError,
                ValueError,
                ProcessLookupError,
                PermissionError,
            ):
                pass
            else:
                print(
                    f"[{self.name}-lock] pid {old_pid} is alive but no longer "
                    "matches this lock; force-acquiring as stale.",
                    file=sys.stderr,
                )
            try:
                # Stale lock: clean and retry; ``exist_ok=False`` on the
                # retry so two concurrent processes can't both succeed.
                shutil.rmtree(self._lock_dir, ignore_errors=True)
                try:
                    self._lock_dir.mkdir(exist_ok=False)
                except FileExistsError:
                    return False
            except OSError:
                return False
        pid = os.getpid()
        (self._lock_dir / "pid").write_text(str(pid))
        metadata = {
            "pid": pid,
            "pid_start_key": _resolve_pid_start_key(pid),
            "cmdline": " ".join(sys.argv),
            "agent": self.name,
        }
        (self._lock_dir / "metadata.json").write_text(json.dumps(metadata, sort_keys=True))
        return True

    def release(self) -> None:
        """Release the lock by removing the lock directory."""
        shutil.rmtree(self._lock_dir, ignore_errors=True)


def lock_pid_identity_status(
    lock_dir: Path,
    pid: int,
    *,
    expected_agent: str | None = None,
) -> bool | None:
    """Compare the lock's recorded identity against the live process.

    Returns:
        * ``True``  — lock metadata matches the live PID + agent + start key.
        * ``False`` — metadata exists but disagrees with the live process.
        * ``None``  — metadata is missing or unparseable (we can't decide).
    """
    metadata_file = lock_dir / "metadata.json"
    if not metadata_file.exists():
        return None
    try:
        metadata = json.loads(metadata_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if metadata.get("pid") != pid:
        return False
    if expected_agent is not None and metadata.get("agent") != expected_agent:
        return False
    expected_start = metadata.get("pid_start_key")
    if not expected_start:
        return None
    actual_start = _resolve_pid_start_key(pid)
    if not actual_start:
        return None
    return actual_start == expected_start


def lock_pid_identity_matches(
    lock_dir: Path,
    pid: int,
    *,
    expected_agent: str | None = None,
) -> bool:
    """Convenience wrapper: ``True`` iff identity status is unambiguously True."""
    return lock_pid_identity_status(lock_dir, pid, expected_agent=expected_agent) is True


def with_lock(name: str) -> AgentLock:
    """Acquire the per-agent lock; exit cleanly if another live PID holds it.

    Also honors the operator-managed pause marker at
    ``$ALFRED_HOME/state/_paused/<name>``. The ``alfred run`` CLI
    enforces that gate, but launchd-spawned firings invoke
    ``agent-launch`` -> ``<agent>.py`` directly and previously
    bypassed it. Embedding the check here means every entrypoint
    respects ``alfred pause <agent>`` without touching the per-codename
    runner scripts.
    """
    import atexit

    if is_agent_paused(name):
        marker = agent_pause_marker_path(name)
        try:
            body = marker.read_text(errors="replace").strip()
        except OSError:
            body = ""
        print(f"[{name.upper()}-PAUSED] marker present: {marker} ({body}). Skipping firing.")
        sys.exit(0)
    lock = AgentLock(name)
    if not lock.acquire():
        print(f"[{name}-LOCKED] previous run still active. Skipping firing.")
        sys.exit(0)
    atexit.register(lock.release)
    return lock


# --------------------------------------------------------------------------
# Per-agent pause marker honoring (launchd bypass fix)
#
# ``alfred pause <agent>`` writes ``$ALFRED_HOME/state/_paused/<codename>``.
# The bash CLI honors that marker before kicking a one-shot run, but
# launchd-spawned firings invoke ``agent-launch`` -> ``<agent>.py``
# directly and bypass the gate. The ``with_lock`` helper above calls
# ``is_agent_paused`` so every runner respects the marker without
# touching per-agent scripts.
# --------------------------------------------------------------------------
PAUSE_MARKER_DIR = STATE_ROOT / "_paused"


def agent_pause_marker_path(codename: str) -> Path:
    """Resolve the operator-managed pause marker file for ``codename``."""
    return PAUSE_MARKER_DIR / codename


def is_agent_paused(codename: str) -> bool:
    """Return True iff a pause marker exists for ``codename``."""
    try:
        return agent_pause_marker_path(codename).is_file()
    except OSError:
        return False


def write_agent_pause_marker(codename: str, reason: str = "") -> Path:
    """Write the pause marker for ``codename``. Idempotent.

    Used by self-pause paths (fail-streak, daily cap) so the operator's
    ``alfred agents`` view shows a paused state matching the runner's
    blocking behaviour. ``reason`` is recorded inside the marker for
    forensics; the body's exact shape is not load-bearing.
    """
    marker = agent_pause_marker_path(codename)
    marker.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = stamp if not reason else f"{stamp} {reason}"
    marker.write_text(body + "\n")
    return marker


def clear_agent_pause_marker(codename: str) -> bool:
    """Remove the pause marker for ``codename``. Returns True iff present."""
    marker = agent_pause_marker_path(codename)
    try:
        marker.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def reset_consecutive_failures(codename: str) -> None:
    """Reset today's ``consecutive_failures`` counter for ``codename``.

    Called from ``alfred resume`` paths so an operator-driven resume
    clears both the pause marker AND the in-runner streak gate.
    Without this, an agent that hit the fail-streak cap would re-pause
    one tick after resume because the spend file's streak counter
    still has the elevated value.
    """
    spend = SpendState(codename)
    if spend.state.get("consecutive_failures", 0):
        spend.set(consecutive_failures=0)


# --------------------------------------------------------------------------
# Per-agent per-day spend ledger
# --------------------------------------------------------------------------


@dataclass
class SpendState:
    """Per-agent per-day spend tracking.

    Auto-resets at midnight via the per-day filename. Increment-only
    counters (``firings_today``, ``turns_today``, ``cost_usd_today``,
    ``successes_today``, ``failures_today``, ``consecutive_failures``)
    plus a per-target session cache for resume.
    """

    agent: str
    state: dict = field(default_factory=dict)
    _path: Path = field(init=False)

    def __post_init__(self) -> None:
        d = STATE_ROOT / self.agent
        d.mkdir(parents=True, exist_ok=True)
        self._path = d / f"spend-{today_str()}.json"
        if self._path.exists():
            try:
                self.state = json.loads(self._path.read_text())
            except json.JSONDecodeError:
                self.state = {}
        self.state.setdefault("firings_today", 0)
        self.state.setdefault("turns_today", 0)
        self.state.setdefault("cost_usd_today", 0.0)
        self.state.setdefault("successes_today", 0)
        self.state.setdefault("failures_today", 0)
        self.state.setdefault("blocked_until", None)
        self.state.setdefault("last_session_id_per_target", {})
        self.state.setdefault("consecutive_failures", 0)

    def save(self) -> None:
        """Persist state atomically; dry-run uses a sibling ledger filename."""
        path = self._path
        if is_dry_run():
            path = self._path.with_name(f"spend-dryrun-{today_str()}.json")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.rename(path)

    def increment(self, **kwargs: float | int) -> None:
        """Increment one or more counters by the given deltas."""
        for k, v in kwargs.items():
            self.state[k] = self.state.get(k, 0) + v
        if is_dry_run():
            deltas = ", ".join(f"{k}+={v}" for k, v in kwargs.items())
            dry_run_log(
                "spend",
                f"would increment real ledger ({deltas}); dry-run ledger only",
            )
        self.save()

    def set(self, **kwargs: Any) -> None:
        """Overwrite one or more counters with explicit values."""
        self.state.update(kwargs)
        if is_dry_run():
            fields = ", ".join(f"{k}={v}" for k, v in kwargs.items())
            dry_run_log("spend", f"would set real ledger ({fields}); dry-run ledger only")
        self.save()

    def is_blocked(self) -> str | None:
        """Return reason if this agent is paused by ``blocked_until``, else ``None``."""
        until = self.state.get("blocked_until")
        if until:
            try:
                exp = datetime.strptime(until, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                if datetime.now(UTC) < exp:
                    return f"blocked until {until}"
                self.state["blocked_until"] = None
                self.save()
            except ValueError:
                self.state["blocked_until"] = None
                self.save()
        return None


# --------------------------------------------------------------------------
# Fleet enable/disable file
# --------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    """tmp+rename atomic write. Leaves no half-written file on crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def _read_enabled_codenames() -> list[str]:
    """Parse ``FLEET_ENABLED_FILE`` into the list of enabled codenames.

    Skips blank lines and ``#``-prefixed comments. Inline comments are
    also stripped (``batman # MVP burn-in``). Returns ``[]`` when the
    file is missing or unreadable.
    """
    if not FLEET_ENABLED_FILE.exists():
        return []
    try:
        text = FLEET_ENABLED_FILE.read_text()
    except OSError:
        return []
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    seen: set[str] = set()
    deduped: list[str] = []
    for c in out:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def is_agent_enabled(codename: str, *, default: bool = True) -> bool:
    """Return ``True`` iff ``codename`` is enabled via the fleet state file.

    File missing -> ``default``. File present and codename listed
    -> ``True``. File present and codename not listed -> ``default``.
    """
    if not FLEET_ENABLED_FILE.exists():
        return default
    return codename in _read_enabled_codenames() or default


def list_enabled_agents() -> list[str]:
    """Return the parsed list of codenames in ``FLEET_ENABLED_FILE``."""
    return _read_enabled_codenames()


def _write_enabled_codenames(codenames: list[str]) -> None:
    """Persist a list of codenames to ``FLEET_ENABLED_FILE`` atomically."""
    deduped = sorted({c.strip() for c in codenames if c and c.strip()})
    header = (
        "# Fleet enable list, managed by `alfred enable/disable <agent>`.\n"
        "# One codename per line. Blank lines and `#`-comments are ignored.\n"
        "# Edit by hand at your own risk; the CLI is the supported path.\n"
    )
    body = "\n".join(deduped)
    _atomic_write(FLEET_ENABLED_FILE, header + body + ("\n" if body else ""))


def enable_agent(codename: str) -> list[str]:
    """Add ``codename`` to ``FLEET_ENABLED_FILE``. Idempotent.

    Returns the new sorted list of enabled codenames.
    """
    codename = codename.strip()
    if not codename:
        raise ValueError("enable_agent: codename must be non-empty")
    current = set(_read_enabled_codenames())
    current.add(codename)
    out = sorted(current)
    _write_enabled_codenames(out)
    return out


def disable_agent(codename: str) -> list[str]:
    """Remove ``codename`` from ``FLEET_ENABLED_FILE``. Idempotent.

    Returns the new sorted list of enabled codenames.
    """
    codename = codename.strip()
    if not codename:
        raise ValueError("disable_agent: codename must be non-empty")
    current = set(_read_enabled_codenames())
    current.discard(codename)
    out = sorted(current)
    _write_enabled_codenames(out)
    return out
