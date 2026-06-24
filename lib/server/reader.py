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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .agent_profiles import profile_payload, sort_codenames
from .firing_timeline import FiringTimeline, derive_timeline
from .plan_approvals import (
    DECISION_APPROVE,
    decision_for_issue,
    issue_num_from_plan_id,
)

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
    # Render-ready distillation of ``raw_events``: a one-line ``headline``, a
    # ``severity`` (ok/idle/error), an honest classified ``error`` cause, and an
    # ordered ``steps`` timeline. Derived server-side so the desktop client and
    # any future surface share one honest source of truth. Optional so legacy
    # callers that construct a record by hand keep working.
    timeline: FiringTimeline | None = None


@dataclass(frozen=True)
class AgentSummary:
    """One codename's at-a-glance fleet-view row.

    ``paused``/``paused_since`` are read from the operator-managed pause
    marker (``state/_paused/<codename>``) that ``alfred pause <codename>``
    writes. ``loaded`` is a best-effort scheduler hint: the read-only server
    cannot probe launchctl/systemctl, so it reports the inverse of ``paused``
    (``alfred pause`` unloads the unit, ``alfred resume`` reloads it). The
    desktop client uses these so its Fleet Control panel no longer has to
    shell ``alfred status --json`` just to read paused/running state.
    """

    codename: str
    last_firing_id: str | None
    last_run_at: str | None
    status: str  # "live" | "idle" | "error"
    last_summary: str
    firings_today: int
    paused: bool = False
    paused_since: str | None = None
    loaded: bool = True
    display_name: str | None = None
    role_title: str | None = None
    purpose: str | None = None
    theme: str | None = None
    theme_label: str | None = None
    theme_accent: str | None = None


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
    source: str = "batman"
    readiness_score: int | None = None
    readiness_ok: bool | None = None
    revision_count: int = 0


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
    """Resolve the runtime state path. The path is not required to exist."""
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
        "followups",
        "_paused",
    }
)


# Matches the ISO-8601 UTC stamp ``write_agent_pause_marker`` records as the
# first token of a pause marker body (e.g. ``2026-05-30T09:00:00Z``).
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


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
        codenames = sort_codenames(list(self._iter_codenames()))
        out: list[AgentSummary] = []
        for codename in codenames:
            firings = self._iter_firings_for(codename, limit=1)
            last = firings[0] if firings else None
            firings_today = self._firings_today(codename)
            paused, paused_since = self._pause_state(codename)
            profile = profile_payload(codename)
            if last is None:
                out.append(
                    AgentSummary(
                        codename=codename,
                        last_firing_id=None,
                        last_run_at=None,
                        status="idle",
                        last_summary="no firings yet",
                        firings_today=firings_today,
                        paused=paused,
                        paused_since=paused_since,
                        loaded=not paused,
                        **profile,
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
                    paused=paused,
                    paused_since=paused_since,
                    loaded=not paused,
                    **profile,
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
        """Return a best-effort fleet-brain reliability report."""
        try:
            from fleet_brain import FleetBrain

            return FleetBrain.from_env().reliability_report(limit=6)
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
        """Return saved plan drafts, newest first."""
        candidates: list[Path] = []
        plan_root = self._plan_root()
        if plan_root.is_dir():
            candidates.extend(plan_root.glob("*.md"))
        draft_root = self._planning_draft_root()
        if draft_root.is_dir():
            candidates.extend(draft_root.glob("*.json"))
        followup_root = self._followup_root()
        if followup_root.is_dir():
            candidates.extend(followup_root.glob("*.md"))
        if not candidates:
            return []
        candidates.sort(
            key=lambda path: _safe_stat_mtime(path),
            reverse=True,
        )
        plans: list[PlanDraft] = []
        max_items = max(1, int(limit))
        for path in candidates:
            plan = self._read_json_plan(path) if path.suffix == ".json" else self._read_plan(path)
            if plan is not None:
                plans.append(plan)
            if len(plans) >= max_items:
                break
        return plans

    def get_plan(self, plan_id: str) -> PlanDraft | None:
        """Read one saved plan by filename stem."""
        if "/" in plan_id or "\\" in plan_id or plan_id.startswith("."):
            return None
        path = self._plan_root() / f"{plan_id}.md"
        if path.exists():
            return self._read_plan(path)
        followup_path = self._followup_root() / f"{plan_id}.md"
        if followup_path.exists():
            return self._read_plan(followup_path)
        json_path = self._planning_draft_root() / f"{plan_id}.json"
        if json_path.exists():
            return self._read_json_plan(json_path)
        return None

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
        candidates.sort(key=_safe_stat_mtime, reverse=True)
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

    def _pause_marker_dir(self) -> Path:
        """Resolve the operator-managed pause-marker directory.

        Mirrors ``agent_runner_state.PAUSE_MARKER_DIR`` (``state/_paused``).
        ``alfred pause <codename>`` and the fail-streak self-pause path both
        write ``state/_paused/<codename>``; reading the same directory lets the
        dashboard show paused state without shelling the CLI.
        """
        return self.state_root / "_paused"

    def _pause_state(self, codename: str) -> tuple[bool, str | None]:
        """Return ``(paused, paused_since)`` for ``codename``.

        ``paused`` is true iff the marker file exists. ``paused_since`` is the
        timestamp recorded in the marker body (``write_agent_pause_marker``
        writes ``<UTC-iso> [reason]``); a body without a parseable timestamp
        falls back to the file mtime. Any read error degrades to "not paused"
        so a transiently unreadable state dir never wedges the view.
        """
        marker = self._pause_marker_dir() / codename
        try:
            if not marker.is_file():
                return False, None
        except OSError:
            return False, None
        since: str | None = None
        try:
            body = marker.read_text(encoding="utf-8").strip()
        except OSError:
            body = ""
        if body:
            first = body.split()[0]
            if _TIMESTAMP_RE.match(first):
                since = first
        if since is None:
            since = _mtime_iso(marker)
        return True, since

    def _plan_root(self) -> Path:
        """Resolve the Batman plan directory next to ``state/``."""
        return self.state_root.parent / "batman-plans"

    def _planning_draft_root(self) -> Path:
        """Resolve the Slack/local planning draft directory."""
        return self.state_root / "planning-drafts"

    def _followup_root(self) -> Path:
        """Resolve the Slack follow-up context directory."""
        return self.state_root / "followups"

    def _read_plan(self, path: Path) -> PlanDraft | None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug("read plan %s: %s", path, exc)
            return None
        updated_at = _mtime_iso(path)
        source = "followup" if path.parent == self._followup_root() else "batman"
        plan = _plan_from_markdown(
            plan_id=path.stem,
            path=str(path),
            updated_at=updated_at,
            content=content,
            source=source,
        )
        return self._with_decision_status(plan)

    def _with_decision_status(self, plan: PlanDraft) -> PlanDraft:
        """Overlay a recorded go/no-go decision onto a Batman plan.

        A genuine Batman plan is decided out-of-band when the operator (Slack
        reaction, or in-app approve/decline) writes the marker file Batman's
        file poll watches. The plan markdown itself still says "Draft", so we
        read the marker here and reflect ``approved``/``declined`` so a decided
        plan reports its real state and drops out of the Needs-you queue.
        """
        if plan.source != "batman":
            return plan
        issue_num = issue_num_from_plan_id(plan.plan_id)
        if issue_num is None:
            return plan
        decision = decision_for_issue(self.state_root, issue_num)
        if decision is None:
            return plan
        status = "approved" if decision == DECISION_APPROVE else "declined"
        return replace(plan, status=status)

    def _read_json_plan(self, path: Path) -> PlanDraft | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("read planning draft %s: %s", path, exc)
            return None
        if not isinstance(payload, dict):
            return None
        updated_at = _json_time(payload.get("updated_at")) or _json_time(payload.get("created_at"))
        return _plan_from_json_payload(
            plan_id=path.stem,
            path=str(path),
            updated_at=updated_at or _mtime_iso(path),
            payload=payload,
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
        timeline=derive_timeline(events),
    )


def _plan_from_markdown(
    *,
    plan_id: str,
    path: str,
    updated_at: str | None,
    content: str,
    source: str = "batman",
) -> PlanDraft:
    title = "Slack follow-up" if source == "followup" else "Batman plan"
    status = "needs follow-up" if source == "followup" else "draft"
    parent = None
    affected_repos = None
    preview = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") and (
            title == "Batman plan" or (source == "followup" and title == "Slack follow-up")
        ):
            title = line.lstrip("#").strip() or title
            continue
        if line.lower().startswith("**status:**"):
            status = _strip_markdown_value(line)
            continue
        if line.lower().startswith("**issue url:**"):
            parent = _strip_markdown_value(line)
            continue
        if source == "followup" and line.lower().startswith("- parent:"):
            parent = _extract_markdown_link_url(_strip_markdown_value(line))
            continue
        if source == "followup" and line.lower().startswith(
            (
                "- captured:",
                "- thread:",
                "- firing:",
                "- bundle:",
                "- created:",
                "- failed repos:",
            )
        ):
            continue
        if line.lower().startswith("**affected repos:**"):
            affected_repos = _strip_markdown_value(line)
            continue
        if not preview and not line.startswith("#") and not line.startswith("**"):
            preview = line
    if not preview:
        preview = (
            "Follow-up context captured from Slack." if source == "followup" else "Awaiting review."
        )
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
        source=source,
    )


def _plan_from_json_payload(
    *,
    plan_id: str,
    path: str,
    updated_at: str | None,
    payload: dict[str, Any],
) -> PlanDraft:
    raw_draft = payload.get("draft")
    draft = raw_draft if isinstance(raw_draft, dict) else {}
    raw_readiness = payload.get("readiness")
    readiness = raw_readiness if isinstance(raw_readiness, dict) else {}
    title = str(draft.get("title") or payload.get("title") or "Alfred planning draft").strip()
    repos = _string_list(draft.get("repos"))
    readiness_score = _optional_int(readiness.get("score"))
    readiness_ok = readiness.get("ok") if isinstance(readiness.get("ok"), bool) else None
    status = "ready" if readiness_ok else "needs scope"
    if readiness_ok is None:
        status = str(payload.get("status") or "draft").strip() or "draft"
    problem = str(draft.get("problem") or "").strip()
    desired = str(draft.get("desired_behavior") or "").strip()
    preview = problem or desired or "Conversation-backed planning draft."
    content = str(payload.get("spec_body") or payload.get("issue_body") or "").strip()
    if not content:
        content = preview or "Conversation-backed planning draft."
    parent = _json_bridge_issue_url(payload)
    return PlanDraft(
        plan_id=plan_id,
        title=title,
        status=status,
        parent=parent,
        affected_repos=", ".join(repos) if repos else None,
        updated_at=updated_at,
        path=path,
        preview=preview,
        content=content,
        source=str(payload.get("source") or "planning").strip() or "planning",
        readiness_score=readiness_score,
        readiness_ok=readiness_ok,
        revision_count=_optional_int(payload.get("revision_count")) or 0,
    )


def _json_bridge_issue_url(payload: dict[str, Any]) -> str | None:
    bridge = payload.get("bridge")
    if not isinstance(bridge, dict):
        return None
    issue_url = str(bridge.get("issue_url") or "").strip()
    if issue_url:
        return issue_url
    issue_urls = bridge.get("issue_urls")
    if isinstance(issue_urls, list):
        for item in issue_urls:
            text = str(item or "").strip()
            if text:
                return text
    issues_by_repo = bridge.get("issues_by_repo")
    if isinstance(issues_by_repo, dict):
        for item in issues_by_repo.values():
            text = str(item or "").strip()
            if text:
                return text
    return None


def _strip_markdown_value(line: str) -> str:
    return line.split(":", 1)[-1].strip().strip("*").strip()


def _extract_markdown_link_url(value: str) -> str:
    match = re.search(r"\]\((https?://[^)]+)\)", value)
    if match:
        return match.group(1)
    return value


def _safe_stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _json_time(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
