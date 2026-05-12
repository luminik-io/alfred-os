#!/usr/bin/env python3
"""``fleet-doctor`` — daily fleet-health snapshot agent.

Read-only health checks across the on-disk state files. Posts a single
Slack thread (Block Kit when a bot token is configured, webhook
fallback otherwise) summarising findings as green / yellow / red.

Checks (each is a small pure function returning a ``Finding`` tuple so
unit tests can target it in isolation):

1. ``check_paused_repos``     — ``$HERMES_HOME/state/paused-repos.json``;
                                yellow if any repo is paused.
2. ``check_global_block``     — fleet-wide rate-limit poison pill;
                                red when active.
3. ``check_stale_worktrees``  — ``$HERMES_HOME/worktrees/`` entries
                                with mtime >24h ago (heuristic for
                                stuck firings).
4. ``check_enabled_agents``   — ``$HERMES_HOME/state/fleet/enabled.txt``
                                contents; surfaces the configured fleet
                                so the operator sees the gating state.

The skeleton intentionally ships these four — paused / global-block /
stale-worktrees / enabled-agents — because they're the ones whose
inputs are already shipped by alfred-os's agent_runner today. Port
operators can extend with additional checks (oauth expiry, daily
spend caps, failure streaks, bundle-queue depth) without changing the
``Finding`` contract.

Health snapshot runner for local fleets.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (_HERE.parent / "lib", Path(os.environ.get("HERMES_HOME", "")) / "lib"):
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
    """Surface the configured runner gate list. Always green — purely
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


CHECKS = [
    check_paused_repos,
    check_global_block,
    check_stale_worktrees,
    check_enabled_agents,
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
    from datetime import UTC, datetime

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
