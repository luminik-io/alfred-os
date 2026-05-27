"""Read-only state reader for ``alfred serve``.

Reads fleet state from ``$ALFRED_HOME/state`` (falling back to
``~/.alfred/state``). The reader is exposed as a :class:`typing.Protocol`
so tests can swap in a stub without touching disk; the default
implementation walks the filesystem layout written by ``lib.agent_runner``.

On-disk layout (canonical, as produced by ``lib/agent_runner.py``):

    $ALFRED_HOME/state/
      <codename>/
        events/<firing_id>.jsonl     # one JSONL per firing
        spend-<YYYY-MM-DD>.json      # per-day per-codename ledger
      transcripts/<codename>/<YYYY-MM>/<firing_id>.jsonl

Forward-compatible optional paths (used if present, never required):

    $ALFRED_HOME/state/codenames/<codename>/...
    $ALFRED_HOME/state/firings/<firing_id>.json
    $ALFRED_HOME/state/events.jsonl

Missing directories are not errors; the reader yields empty lists and the
views render an empty state.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiringRecord:
    """One firing's distilled record.

    ``raw_events`` carries the parsed JSONL lines as-is for the detail view;
    the summary fields are derived from them where possible.
    """

    firing_id: str
    codename: str
    started_at: str | None
    ended_at: str | None
    status: str  # "ok" | "error" | "running" | "unknown"
    summary: str
    transcript_path: str | None
    events_path: str
    raw_events: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class AgentSummary:
    """One codename's at-a-glance fleet-view row."""

    codename: str
    last_firing_id: str | None
    last_run_at: str | None
    status: str  # "live" | "idle" | "error"
    last_summary: str
    firings_today: int


@dataclass(frozen=True)
class PlanDraft:
    """One locally saved Batman plan."""

    plan_id: str
    title: str
    status: str
    parent: str | None
    affected_repos: str | None
    updated_at: str | None
    path: str
    preview: str
    content: str


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class FleetReader(Protocol):
    """Read-only view over fleet state. Tests stub this."""

    def list_agents(self) -> list[AgentSummary]: ...

    def list_recent_firings(
        self, *, limit: int = 50, codename: str | None = None
    ) -> list[FiringRecord]: ...

    def get_firing(self, firing_id: str) -> FiringRecord | None: ...

    def reliability_report(self) -> dict[str, Any]: ...

    def list_plans(self, *, limit: int = 20) -> list[PlanDraft]: ...

    def get_plan(self, plan_id: str) -> PlanDraft | None: ...


# ---------------------------------------------------------------------------
# Filesystem implementation
# ---------------------------------------------------------------------------


def default_state_root() -> Path:
    """Resolve ``$ALFRED_HOME/state`` with the documented fallback to
    ``~/.alfred/state``. The path is not required to exist."""
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base) / "state"


# Per-codename state subdirectories that exist for runtime bookkeeping
# rather than agents. Skip them when walking ``state/`` to enumerate
# codenames so the fleet view does not show "transcripts" or "fleet" as
# imaginary agents.
_RESERVED_STATE_SUBDIRS: frozenset[str] = frozenset(
    {
        "transcripts",
        "codex",
        "fleet",
        "engines",
        "codenames",
        "firings",
        "events",
        "_paused",
    }
)


@dataclass
class FilesystemReader:
    """Default ``FleetReader`` reading ``$ALFRED_HOME/state`` from disk.

    All read failures (missing files, malformed JSON) degrade to empty
    results plus a debug-level log entry. The dashboard must never crash
    because state is briefly inconsistent.
    """

    state_root: Path = field(default_factory=default_state_root)

    # -- public API ---------------------------------------------------------

    def list_agents(self) -> list[AgentSummary]:
        codenames = sorted(self._iter_codenames())
        out: list[AgentSummary] = []
        for codename in codenames:
            firings = self._iter_firings_for(codename, limit=1)
            last = firings[0] if firings else None
            firings_today = self._firings_today(codename)
            if last is None:
                out.append(
                    AgentSummary(
                        codename=codename,
                        last_firing_id=None,
                        last_run_at=None,
                        status="idle",
                        last_summary="no firings yet",
                        firings_today=firings_today,
                    )
                )
                continue
            out.append(
                AgentSummary(
                    codename=codename,
                    last_firing_id=last.firing_id,
                    last_run_at=last.ended_at or last.started_at,
                    status=_status_dot(last),
                    last_summary=last.summary,
                    firings_today=firings_today,
                )
            )
        return out

    def list_recent_firings(
        self,
        *,
        limit: int = 50,
        codename: str | None = None,
    ) -> list[FiringRecord]:
        codenames: Iterable[str]
        if codename is not None:
            codenames = [codename]
        else:
            codenames = self._iter_codenames()
        pool: list[FiringRecord] = []
        for c in codenames:
            pool.extend(self._iter_firings_for(c, limit=limit))
        pool.sort(key=lambda f: (f.started_at or "", f.firing_id), reverse=True)
        return pool[:limit]

    def get_firing(self, firing_id: str) -> FiringRecord | None:
        # Walk codenames until we find a matching events file. The firing_id
        # is operator-supplied user input; ensure it cannot escape via path
        # traversal by rejecting separators.
        if "/" in firing_id or "\\" in firing_id or firing_id.startswith("."):
            return None
        for codename in self._iter_codenames():
            path = self.state_root / codename / "events" / f"{firing_id}.jsonl"
            if path.exists():
                return self._read_firing(codename, path)
        # Fall back to the optional ``state/firings/<firing_id>.json``
        # single-file convention. Treat it as one event record so the
        # detail view still renders something useful.
        opt = self.state_root / "firings" / f"{firing_id}.json"
        if opt.exists():
            try:
                data = json.loads(opt.read_text())
            except (OSError, json.JSONDecodeError):
                return None
            codename = str(data.get("agent") or data.get("codename") or "unknown")
            return _firing_from_events(codename, firing_id, [data], str(opt))
        return None

    def reliability_report(self) -> dict[str, Any]:
        """Return a best-effort fleet-brain reliability report.

        The dashboard is read-only and must keep rendering if the brain
        has not been initialized yet, so any memory-layer problem becomes
        a soft "unknown" report rather than an HTTP 500.
        """
        try:
            from fleet_brain import FleetBrain, default_db_path

            db_path = default_db_path()
            if not db_path.exists():
                return {
                    "status": "unknown",
                    "actions": [],
                    "failure_patterns": [],
                    "stale_workers": [],
                    "promotion_suggestions": [],
                    "error": f"fleet brain database not initialized: {db_path}",
                }

            return FleetBrain(db_path=db_path).reliability_report(limit=6)
        except Exception as exc:  # pragma: no cover - defensive UI path
            return {
                "status": "unknown",
                "actions": [],
                "failure_patterns": [],
                "stale_workers": [],
                "promotion_suggestions": [],
                "error": str(exc),
            }

    def list_plans(self, *, limit: int = 20) -> list[PlanDraft]:
        """Return saved Batman plan drafts, newest first."""
        plan_root = self._plan_root()
        if not plan_root.is_dir():
            return []
        candidates = sorted(
            plan_root.glob("*.md"),
            key=lambda path: _safe_stat_mtime(path),
            reverse=True,
        )
        plans: list[PlanDraft] = []
        for path in candidates[: max(1, int(limit))]:
            plan = self._read_plan(path)
            if plan is not None:
                plans.append(plan)
        return plans

    def get_plan(self, plan_id: str) -> PlanDraft | None:
        """Read one saved Batman plan by filename stem."""
        if "/" in plan_id or "\\" in plan_id or plan_id.startswith("."):
            return None
        path = self._plan_root() / f"{plan_id}.md"
        if not path.exists():
            return None
        return self._read_plan(path)

    # -- internals ----------------------------------------------------------

    def _iter_codenames(self) -> list[str]:
        """Enumerate codenames that have a state directory.

        Sources:
        * ``state/<codename>/`` (canonical, written by ``agent_runner``)
        * ``state/codenames/<codename>/`` (forward-compat, optional)

        Reserved subdirs (transcripts/, fleet/, engines/, ...) are filtered
        out. Returns a deduped sorted list.
        """
        found: set[str] = set()
        if self.state_root.is_dir():
            for entry in self.state_root.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name in _RESERVED_STATE_SUBDIRS:
                    continue
                if entry.name.startswith("."):
                    continue
                # Only count directories that actually have events or spend
                # files, otherwise unrelated state subdirs that we forgot
                # to reserve would leak in as fake agents.
                if (entry / "events").is_dir() or any(entry.glob("spend-*.json")):
                    found.add(entry.name)
        codenames_dir = self.state_root / "codenames"
        if codenames_dir.is_dir():
            for entry in codenames_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    found.add(entry.name)
        return sorted(found)

    def _iter_firings_for(self, codename: str, *, limit: int) -> list[FiringRecord]:
        records: list[FiringRecord] = []
        events_dir = self.state_root / codename / "events"
        candidates: list[Path] = []
        if events_dir.is_dir():
            candidates.extend(events_dir.glob("*.jsonl"))
        # Optional alt path under state/codenames/<name>/events/.
        alt_events = self.state_root / "codenames" / codename / "events"
        if alt_events.is_dir():
            candidates.extend(alt_events.glob("*.jsonl"))

        # Sort by mtime descending so we cap I/O even for large fleets.
        # Guard against OSError from race conditions (file deleted between
        # glob and stat), broken symlinks, or permission errors so a single
        # bad event file does not take down the /firings view. Unreadable
        # entries sort last (mtime=0) and are likely skipped by the limit
        # cap; if they squeak through, _read_firing handles its own errors.
        def _safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0

        candidates.sort(key=_safe_mtime, reverse=True)
        for path in candidates[: max(limit, 1) * 2]:
            rec = self._read_firing(codename, path)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda f: (f.started_at or "", f.firing_id), reverse=True)
        return records[:limit]

    def _read_firing(self, codename: str, path: Path) -> FiringRecord | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("read firing %s: %s", path, exc)
            return None
        events: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # One malformed line should not nuke the whole firing.
                continue
        firing_id = path.stem
        return _firing_from_events(codename, firing_id, events, str(path))

    def _firings_today(self, codename: str) -> int:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        ledger = self.state_root / codename / f"spend-{today}.json"
        if not ledger.exists():
            return 0
        try:
            data = json.loads(ledger.read_text())
        except (OSError, json.JSONDecodeError):
            return 0
        try:
            return int(data.get("firings_today", 0))
        except (TypeError, ValueError):
            return 0

    def _plan_root(self) -> Path:
        """Resolve the Batman plan directory next to ``state/``."""
        return self.state_root.parent / "batman-plans"

    def _read_plan(self, path: Path) -> PlanDraft | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("read plan %s: %s", path, exc)
            return None
        updated_at = _mtime_iso(path)
        return _plan_from_markdown(
            plan_id=path.stem,
            path=str(path),
            updated_at=updated_at,
            content=content,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _firing_from_events(
    codename: str,
    firing_id: str,
    events: list[dict],
    events_path: str,
) -> FiringRecord:
    """Distill a :class:`FiringRecord` from raw event dicts."""
    started = next(
        (
            e.get("ts")
            for e in events
            if e.get("event") in {"firing_started", "start", "preflight_passed"}
        ),
        None,
    )
    ended = None
    status = "unknown"
    summary_text = ""
    for event in events:
        name = str(event.get("event") or "")
        if name in {"firing_ended", "firing_complete", "end", "ok", "done"}:
            ended = event.get("ts") or ended
            status = "ok"
            summary_text = _short_summary(event) or summary_text
        elif name in {"firing_failed", "error", "failed"}:
            ended = event.get("ts") or ended
            status = "error"
            summary_text = _short_summary(event) or summary_text
    if status == "unknown" and events:
        # Pick a reasonable summary from whatever events we have.
        summary_text = summary_text or _short_summary(events[-1])
        if started and not ended:
            status = "running"
    if not summary_text and events:
        summary_text = _short_summary(events[-1])
    if not started and events:
        started = events[0].get("ts")
    transcript_path = next(
        (e.get("transcript_path") for e in events if e.get("transcript_path")),
        None,
    )
    return FiringRecord(
        firing_id=firing_id,
        codename=codename,
        started_at=started,
        ended_at=ended,
        status=status,
        summary=summary_text or "(no summary)",
        transcript_path=transcript_path,
        events_path=events_path,
        raw_events=events,
    )


def _plan_from_markdown(
    *,
    plan_id: str,
    path: str,
    updated_at: str | None,
    content: str,
) -> PlanDraft:
    title = "Batman plan"
    status = "draft"
    parent = None
    affected_repos = None
    preview = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") and title == "Batman plan":
            title = line.lstrip("#").strip() or title
            continue
        if line.lower().startswith("**status:**"):
            status = _strip_markdown_value(line)
            continue
        if line.lower().startswith("**issue url:**"):
            parent = _strip_markdown_value(line)
            continue
        if line.lower().startswith("**affected repos:**"):
            affected_repos = _strip_markdown_value(line)
            continue
        if not preview and not line.startswith("#") and not line.startswith("**"):
            preview = line
    if not preview:
        preview = "Awaiting review."
    return PlanDraft(
        plan_id=plan_id,
        title=title,
        status=status or "draft",
        parent=parent,
        affected_repos=affected_repos,
        updated_at=updated_at,
        path=path,
        preview=preview,
        content=content,
    )


def _strip_markdown_value(line: str) -> str:
    return line.split(":", 1)[-1].strip().strip("*").strip()


def _safe_stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return None


def _short_summary(event: dict) -> str:
    """One-line summary for an event dict."""
    name = event.get("event") or "event"
    interesting = {k: v for k, v in event.items() if k not in {"ts", "agent", "firing_id", "event"}}
    if not interesting:
        return str(name)
    parts = []
    for key, value in list(interesting.items())[:3]:
        text = str(value)
        if len(text) > 60:
            text = text[:57] + "..."
        parts.append(f"{key}={text}")
    return f"{name}: {' '.join(parts)}"


def _status_dot(firing: FiringRecord) -> str:
    """Map a firing's status to a fleet-view dot color name."""
    if firing.status == "error":
        return "error"
    if firing.status == "running":
        return "live"
    return "idle"
