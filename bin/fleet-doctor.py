#!/usr/bin/env python3
"""``fleet-doctor``, daily fleet-health snapshot agent.

Read-only health checks across the on-disk state files. Posts a single
Slack thread (Block Kit when a bot token is configured, webhook
fallback otherwise) summarising findings as green / yellow / red.

Checks (each is a small pure function returning a ``Finding`` tuple so
unit tests can target it in isolation):

1. ``check_paused_repos``:    ``$ALFRED_HOME/state/paused-repos.json``;
                                yellow if any repo is paused.
2. ``check_global_block``:    fleet-wide rate-limit poison pill;
                                red when active.
3. ``check_stale_worktrees`` , ``$ALFRED_HOME/worktrees/`` entries
                                with mtime >24h ago (heuristic for
                                stuck firings).
4. ``check_enabled_agents``:  ``$ALFRED_HOME/state/fleet/enabled.txt``
                                contents; surfaces the configured fleet
                                so the operator sees the gating state.
5. ``check_paused_agents``:   pause markers under
                                ``$ALFRED_HOME/state/_paused``.
6. ``check_spend_state``:     today's spend and failure-streak files.

The checks use only local state already written by alfred-os. Port operators
can extend with network checks (OAuth expiry, queue depth, deploy drift)
without changing the ``Finding`` contract.

Health snapshot runner for local fleets.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agent_runner import (  # noqa: E402
    STATE_ROOT,
    WORKTREE_ROOT,
    PreflightSpec,
    doctor_mode,
    is_globally_blocked,
    list_enabled_agents,
    list_paused_repos,
    preflight,
    slack_post,
    with_lock,
)
from slack_format import firing_thread_root  # noqa: E402

AGENT = "fleet-doctor"

STALE_WORKTREE_SECONDS = 24 * 3600

# Severity rank for picking the post-level severity (worst wins).
SEVERITY_RANK = {"green": 0, "yellow": 1, "alert": 2}
SEVERITY_TO_SLACK = {"green": "info", "yellow": "warn", "alert": "alert"}


@dataclass
class Finding:
    name: str
    severity: str  # "green" | "yellow" | "alert"
    message: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.name, self.severity, self.message)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_paused_repos() -> Finding:
    """Yellow when any repo is paused; green otherwise."""
    paused = list_paused_repos()
    if not paused:
        return Finding("paused-repos", "green", "no repos paused")
    return Finding("paused-repos", "yellow", f"{len(paused)} repo(s) paused: {', '.join(paused)}")


def check_global_block() -> Finding:
    """Red when a fleet-wide rate-limit block is active."""
    blocked = is_globally_blocked()
    if not blocked:
        return Finding("global-block", "green", "no fleet-wide block")
    return Finding("global-block", "alert", f"fleet-wide block active: {blocked}")


def check_stale_worktrees() -> Finding:
    """Yellow when worktrees exist that haven't been touched in 24h+."""
    if not WORKTREE_ROOT.exists():
        return Finding("stale-worktrees", "green", "no worktrees")
    import time

    now = time.time()
    stale: list[Path] = []
    for p in WORKTREE_ROOT.iterdir():
        if not p.is_dir():
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age > STALE_WORKTREE_SECONDS:
            stale.append(p)
    if not stale:
        return Finding("stale-worktrees", "green", "no stale worktrees")
    sample = ", ".join(p.name for p in stale[:3])
    return Finding(
        "stale-worktrees",
        "yellow",
        f"{len(stale)} stale worktree(s) (>{STALE_WORKTREE_SECONDS // 3600}h): {sample}"
        + ("…" if len(stale) > 3 else ""),
    )


def check_enabled_agents() -> Finding:
    """Surface the configured runner gate list. Always green, purely
    informational so the operator can confirm the gating state."""
    if not (STATE_ROOT / "fleet" / "enabled.txt").exists():
        return Finding(
            "enabled-agents",
            "green",
            "fleet gate file missing → runners fall back to their own defaults",
        )
    enabled = list_enabled_agents()
    if not enabled:
        return Finding("enabled-agents", "yellow", "fleet gate file present but empty")
    return Finding(
        "enabled-agents",
        "green",
        f"{len(enabled)} agent(s) listed in runner gate: {', '.join(enabled)}",
    )


def check_paused_agents() -> Finding:
    pause_dir = STATE_ROOT / "_paused"
    if not pause_dir.is_dir():
        return Finding("paused-agents", "green", "no paused agents")
    markers = sorted(path for path in pause_dir.iterdir() if path.is_file())
    if not markers:
        return Finding("paused-agents", "green", "no paused agents")

    import time

    now = time.time()
    parts: list[str] = []
    old = 0
    for marker in markers[:8]:
        try:
            hours = int((now - marker.stat().st_mtime) // 3600)
        except OSError:
            hours = 0
        if hours >= 24:
            old += 1
        parts.append(f"{marker.name} ({hours}h)")
    suffix = " (some >24h)" if old else ""
    more = f", +{len(markers) - 8} more" if len(markers) > 8 else ""
    return Finding("paused-agents", "yellow", f"Paused agents{suffix}: {', '.join(parts)}{more}")


def _today_spend_files() -> list[Path]:
    # SpendState writes under a UTC day key (`agent_runner/spend.today_str()`);
    # match here so non-UTC hosts during local/UTC date-skew windows don't
    # report `no spend today` while writes are still landing on the UTC day.
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return sorted(STATE_ROOT.glob(f"*/spend-{today}.json"))


def check_spend_state() -> Finding:
    files = [path for path in _today_spend_files() if not path.parent.name.startswith("_")]
    if not files:
        return Finding("spend-state", "green", "no spend files for today yet")

    yellow: list[str] = []
    alerts: list[str] = []
    for path in files:
        agent = path.parent.name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            yellow.append(f"{agent}: unreadable spend file")
            continue
        consecutive = int(data.get("consecutive_failures") or 0)
        failures = int(data.get("failures_today") or 0)
        successes = int(data.get("successes_today") or 0)
        blocked_until = str(data.get("blocked_until") or "").strip()
        if consecutive >= 8:
            alerts.append(f"{agent}: {consecutive} consecutive failures")
        elif consecutive or failures:
            yellow.append(f"{agent}: {failures} fail / {successes} ok")
        if blocked_until:
            try:
                parsed = datetime.fromisoformat(blocked_until.replace("Z", "+00:00"))
                if parsed.astimezone(UTC) > datetime.now(UTC):
                    yellow.append(f"{agent}: blocked until {blocked_until}")
            except ValueError:
                yellow.append(f"{agent}: invalid blocked_until={blocked_until}")

    if alerts:
        return Finding("spend-state", "alert", "; ".join(alerts[:6]))
    if yellow:
        return Finding("spend-state", "yellow", "; ".join(yellow[:6]))
    return Finding("spend-state", "green", f"{len(files)} spend file(s), no failure streaks")


ENGINE_AUTH_WINDOW_SECONDS = 3600  # last 1h
ENGINE_AUTH_MIN_AGENTS = 3


def _recent_event_jsonl_paths(
    *,
    window_seconds: int = ENGINE_AUTH_WINDOW_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Return event-log JSONL files modified within ``window_seconds``."""
    import time as _time

    now_ts = _time.time() if now is None else now
    cutoff = now_ts - window_seconds
    paths: list[Path] = []
    if not STATE_ROOT.is_dir():
        return paths
    for agent_dir in STATE_ROOT.iterdir():
        if not agent_dir.is_dir():
            continue
        events_dir = agent_dir / "events"
        if not events_dir.is_dir():
            continue
        for f in events_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime >= cutoff:
                    paths.append(f)
            except OSError:
                continue
    return paths


def _file_has_engine_auth_failure(path: Path, *, cutoff_ts: float) -> bool:
    """Return True iff ``path`` contains at least one event with
    ``subtype: error_authentication`` AND ``engine: claude`` within
    the window. Best-effort parser: malformed records are skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                ts_str = rec.get("ts", "")
                if isinstance(ts_str, str) and ts_str:
                    try:
                        rec_ts = (
                            datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%fZ")
                            .replace(tzinfo=UTC)
                            .timestamp()
                        )
                        if rec_ts < cutoff_ts:
                            continue
                    except ValueError:
                        pass
                if rec.get("subtype") == "error_authentication" and rec.get("engine") == "claude":
                    return True
    except OSError:
        return False
    return False


def check_engine_auth_streak(
    *,
    window_seconds: int = ENGINE_AUTH_WINDOW_SECONDS,
    min_agents: int = ENGINE_AUTH_MIN_AGENTS,
    now: float | None = None,
) -> Finding:
    """Concurrent Anthropic auth failures across the fleet.

    Walks per-firing event JSONL files in the last ``window_seconds``
    and counts distinct agents emitting ``subtype: error_authentication``
    with ``engine: claude``. Red when ``min_agents`` or more concurrent
    agents hit the same failure mode within the window — the root cause
    is the operator's Anthropic session or Keychain ACL, not any
    individual agent's prompt.
    """
    import time as _time

    now_ts = _time.time() if now is None else now
    cutoff_ts = now_ts - window_seconds
    affected: set[str] = set()
    for path in _recent_event_jsonl_paths(window_seconds=window_seconds, now=now_ts):
        if _file_has_engine_auth_failure(path, cutoff_ts=cutoff_ts):
            agent = path.parent.parent.name
            affected.add(agent)
    if len(affected) >= min_agents:
        listed = ", ".join(sorted(affected))
        return Finding(
            "engine-auth-streak",
            "alert",
            (
                f"🔴 Engine auth failing: {len(affected)} agents hitting "
                f"error_authentication on engine=claude in last "
                f"{window_seconds // 60}m ({listed}). Likely Keychain ACL "
                "or session expiry. Run `alfred claude probe` to diagnose."
            ),
        )
    return Finding(
        "engine-auth-streak",
        "green",
        "No concurrent Anthropic auth failures.",
    )


CHECKS = [
    check_paused_repos,
    check_global_block,
    check_stale_worktrees,
    check_enabled_agents,
    check_paused_agents,
    check_spend_state,
    check_engine_auth_streak,
]


def run_all_checks() -> list[Finding]:
    """Run every check, swallowing per-check exceptions so a single
    bug doesn't prevent the operator from seeing the rest of the
    snapshot."""
    findings: list[Finding] = []
    for fn in CHECKS:
        try:
            findings.append(fn())
        except Exception as e:
            findings.append(
                Finding(
                    fn.__name__.removeprefix("check_"),
                    "yellow",
                    f"check failed: {type(e).__name__}: {e}",
                )
            )
    return findings


def overall_severity(findings: list[Finding]) -> str:
    """Worst-wins. Empty findings → green."""
    rank = max((SEVERITY_RANK[f.severity] for f in findings), default=0)
    for sev, r in SEVERITY_RANK.items():
        if r == rank:
            return sev
    return "green"


def format_summary(findings: list[Finding]) -> str:
    """Markdown body grouped green / yellow / red. Empty buckets are
    dropped so a healthy day shows just the green section."""
    by_sev: dict[str, list[Finding]] = {"alert": [], "yellow": [], "green": []}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)
    sections: list[str] = []
    if by_sev["alert"]:
        sections.append("*ALERT*\n" + "\n".join(f"• {f.message}" for f in by_sev["alert"]))
    if by_sev["yellow"]:
        sections.append("*YELLOW*\n" + "\n".join(f"• {f.message}" for f in by_sev["yellow"]))
    if by_sev["green"]:
        sections.append("*GREEN*\n" + "\n".join(f"• {f.message}" for f in by_sev["green"]))
    return "\n\n".join(sections)


def main() -> int:
    if doctor_mode():
        print("[FLEET-DOCTOR-OK]")
        return 0

    spec = PreflightSpec(agent=AGENT)
    try:
        preflight(spec)
    except Exception as e:
        print(f"[FLEET-DOCTOR-PREFLIGHT-FAIL] {e}", file=sys.stderr)
        return 0

    with_lock(AGENT)
    findings = run_all_checks()
    sev = overall_severity(findings)
    body = format_summary(findings)

    firing_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    summary = f"fleet snapshot · {sev}"
    handle = firing_thread_root(
        codename=AGENT,
        firing_id=firing_id,
        summary_one_liner=summary,
        severity=SEVERITY_TO_SLACK[sev],
        body=body,
    )
    if handle is None:
        slack_post(
            f"[FLEET-DOCTOR] {summary}\n{body}",
            severity=SEVERITY_TO_SLACK[sev],
        )
    print(f"[FLEET-DOCTOR-{sev.upper()}] {len(findings)} check(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
