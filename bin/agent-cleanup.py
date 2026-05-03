#!/usr/bin/env python3
"""Daily cleanup: sweep stale debug files, abandoned worktrees, old spend
files, expired transcripts, expired event logs, and stuck agent-locks.

Retention policy (lower-bounds, configurable via env vars):
  spend-YYYY-MM-DD.json     90 days  (metrics needs the history)
  transcripts/.../*.jsonl   30 days  (per-firing stream-json)
  events/<id>.jsonl         30 days  (per-firing structured event log)
  /tmp/<agent>-debug-*       1 day
  worktrees/<name>           2 hours after mtime + git worktree remove
  /tmp/agent-lock-<name>     4 hours (force-unlocked if older - matches
                                     AgentLock._LOCK_MAX_AGE_SECONDS)

Stale claim sweep: scans every repo in ALFRED_CLAIM_SWEEP_REPOS for
agent:in-flight claims older than ALFRED_CLAIM_MAX_AGE_HOURS (default 4)
and force-releases them via the framework's force_release_stale_claim().
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")) + "/lib")
from agent_runner import (
    STATE_ROOT,
    WORKSPACE,
    WORKTREE_ROOT,
    PreflightFailed,
    PreflightSpec,
    doctor_mode,
    find_stale_claims,
    force_release_stale_claim,
    preflight,
    slack_post,
)

AGENT = os.environ.get("AGENT_CODENAME", "cleanup")
PREFLIGHT = PreflightSpec(agent=AGENT, bins=["git"])

# Transcripts dir is optional in the framework; default to STATE_ROOT/transcripts.
try:
    from agent_runner import TRANSCRIPTS_ROOT  # type: ignore
except ImportError:
    TRANSCRIPTS_ROOT = STATE_ROOT / "transcripts"

try:
    preflight(PREFLIGHT)
except PreflightFailed:
    sys.exit(0)

if doctor_mode():
    print(f"[{AGENT.upper()}-DOCTOR-OK]")
    sys.exit(0)

NOW = time.time()
ONE_DAY = 86400

# Scan ALL agent-prefixed scratch files in /tmp; the scrubbed runners use
# `<agent-codename>-debug-*`, `<agent-codename>-prbody-*`, etc.
TMP_PATTERNS = [
    "*-debug-*",
    "*-prompt-*",
    "*-prbody-*",
    "*-wip-*",
    "*-out-*",
    "*-run-*",
]

removed = 0
freed_mb = 0.0
for pattern in TMP_PATTERNS:
    for p in Path("/tmp").glob(pattern):
        try:
            age_days = (NOW - p.stat().st_mtime) / ONE_DAY
            if age_days < 1:
                continue
            size_mb = (
                sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                if p.is_dir()
                else p.stat().st_size
            ) / (1024 * 1024)
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
            removed += 1
            freed_mb += size_mb
        except OSError:
            pass

# Sweep abandoned worktrees (>2h old, not tracked by any git worktree list)
wt_root = WORKTREE_ROOT
wt_removed = 0
if wt_root.exists():
    for wt in wt_root.iterdir():
        try:
            age = NOW - wt.stat().st_mtime
            if age < 7200:  # < 2h, leave alone
                continue
            for repo_dir in WORKSPACE.iterdir():
                if not (repo_dir / ".git").exists():
                    continue
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt)],
                    cwd=str(repo_dir),
                    capture_output=True,
                    timeout=15,
                )
            shutil.rmtree(wt, ignore_errors=True)
            wt_removed += 1
        except OSError:
            pass

SPEND_RETENTION_DAYS = int(os.environ.get("ALFRED_SPEND_RETENTION_DAYS", "90"))
TRANSCRIPT_RETENTION_DAYS = int(os.environ.get("ALFRED_TRANSCRIPT_RETENTION_DAYS", "30"))
EVENTS_RETENTION_DAYS = int(os.environ.get("ALFRED_EVENTS_RETENTION_DAYS", "30"))

state_root = STATE_ROOT
spend_removed = 0
events_removed = 0
if state_root.exists():
    for agent_dir in state_root.iterdir():
        if not agent_dir.is_dir():
            continue
        for f in agent_dir.glob("spend-*.json"):
            age_days = (NOW - f.stat().st_mtime) / ONE_DAY
            if age_days > SPEND_RETENTION_DAYS:
                f.unlink(missing_ok=True)
                spend_removed += 1
        events_dir = agent_dir / "events"
        if events_dir.is_dir():
            for f in events_dir.glob("*.jsonl"):
                age_days = (NOW - f.stat().st_mtime) / ONE_DAY
                if age_days > EVENTS_RETENTION_DAYS:
                    f.unlink(missing_ok=True)
                    events_removed += 1

transcript_removed = 0
transcript_freed_mb = 0.0
if TRANSCRIPTS_ROOT.exists():
    for agent_dir in TRANSCRIPTS_ROOT.iterdir():
        if not agent_dir.is_dir():
            continue
        for month_dir in agent_dir.iterdir():
            if not month_dir.is_dir():
                continue
            for f in month_dir.glob("*.jsonl"):
                try:
                    age_days = (NOW - f.stat().st_mtime) / ONE_DAY
                    if age_days <= TRANSCRIPT_RETENTION_DAYS:
                        continue
                    transcript_freed_mb += f.stat().st_size / (1024 * 1024)
                    f.unlink(missing_ok=True)
                    transcript_removed += 1
                except OSError:
                    pass
            try:
                if not any(month_dir.iterdir()):
                    month_dir.rmdir()
            except OSError:
                pass

# Force-unlock agents whose lock is older than 4h.
LOCK_MAX_AGE = 4 * 3600
locks_unlocked = 0
for lock_dir in Path("/tmp").glob("agent-lock-*"):
    if not lock_dir.is_dir():
        continue
    try:
        age = NOW - lock_dir.stat().st_mtime
    except OSError:
        continue
    if age <= LOCK_MAX_AGE:
        continue
    pid_file = lock_dir / "pid"
    pid_alive = False
    old_pid = 0
    try:
        old_pid = int(pid_file.read_text().strip())
        os.kill(old_pid, 0)
        pid_alive = True
    except (OSError, ValueError, ProcessLookupError):
        pid_alive = False
    if pid_alive and old_pid:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["pkill", "-TERM", "-P", str(old_pid)],
                capture_output=True,
                timeout=5,
            )
    shutil.rmtree(lock_dir, ignore_errors=True)
    locks_unlocked += 1

print(f"[cleanup] /tmp: {removed} files/dirs removed ({freed_mb:.1f} MB freed)")
print(f"[cleanup] worktrees: {wt_removed} abandoned removed")
print(f"[cleanup] spend files: {spend_removed} removed (>{SPEND_RETENTION_DAYS}d)")
print(f"[cleanup] event logs: {events_removed} removed (>{EVENTS_RETENTION_DAYS}d)")
print(
    f"[cleanup] transcripts: {transcript_removed} removed ({transcript_freed_mb:.1f} MB freed, >{TRANSCRIPT_RETENTION_DAYS}d)"
)
print(f"[cleanup] stuck locks: {locks_unlocked} force-released (>{LOCK_MAX_AGE // 3600}h)")

# Sweep stale agent:in-flight claims across configured repos.
CLAIM_MAX_AGE_HOURS = int(os.environ.get("ALFRED_CLAIM_MAX_AGE_HOURS", "4"))
CLAIM_SWEEP_REPOS = [
    r.strip() for r in os.environ.get("ALFRED_CLAIM_SWEEP_REPOS", "").split(",") if r.strip()
]
import datetime as _dt  # noqa: E402  (local; cleanup is single-file procedural)

sweep_id = _dt.datetime.now(_dt.UTC).strftime("%Y%m%d-%H%M%S-cleanup")
stale_total = 0
swept_total = 0
for repo in CLAIM_SWEEP_REPOS:
    try:
        stale = find_stale_claims(repo, max_age_hours=CLAIM_MAX_AGE_HOURS)
    except Exception as e:
        print(f"[cleanup] {repo}: stale-claim probe failed: {e}", file=sys.stderr)
        continue
    if not stale:
        continue
    stale_total += len(stale)
    for entry in stale:
        try:
            force_release_stale_claim(repo, entry["number"], sweep_id=sweep_id)
            swept_total += 1
            print(
                f"[cleanup] stale-claim swept: {repo}#{entry['number']} "
                f"(codename={entry['codename']} firing_id={entry['firing_id']} "
                f"age={entry.get('age_hours', 0):.1f}h)"
            )
        except Exception as e:
            print(f"[cleanup] {repo}#{entry['number']}: sweep failed: {e}", file=sys.stderr)

if swept_total:
    slack_post(
        f"🧹 cleanup: swept {swept_total} stale agent:in-flight claim(s) "
        f"across {len(CLAIM_SWEEP_REPOS)} repos (>{CLAIM_MAX_AGE_HOURS}h old)."
    )
print(
    f"[cleanup] stale claims: {swept_total}/{stale_total} force-released (>{CLAIM_MAX_AGE_HOURS}h)"
)
