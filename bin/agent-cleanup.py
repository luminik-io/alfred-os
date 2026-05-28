#!/usr/bin/env python3
"""Daily cleanup: sweep stale debug files, abandoned worktrees, old spend
files, expired transcripts, expired event logs, and stuck agent-locks.

Retention policy (lower-bounds, configurable via env vars):
  spend-YYYY-MM-DD.json     90 days  (metrics needs the history)
  transcripts/.../*.jsonl   30 days  (per-firing stream-json)
  events/<id>.jsonl         30 days  (per-firing structured event log)
  /tmp/<agent>-debug-*       1 day
  clean worktrees/<name>     2 hours after mtime + git worktree remove
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
import signal
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for candidate in (
    Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")) / "lib",
    _HERE.parent / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
from agent_runner import (  # noqa: E402
    ALFRED_HOME,
    STATE_ROOT,
    WORKSPACE,
    WORKTREE_ROOT,
    PreflightFailed,
    PreflightSpec,
    create_recovery_ref,
    doctor_mode,
    find_stale_claims,
    force_release_stale_claim,
    lock_pid_identity_status,
    preflight,
    slack_post,
    worktree_risk_reason,
)

AGENT = os.environ.get("AGENT_CODENAME", "cleanup")
PREFLIGHT = PreflightSpec(agent=AGENT, bins=["git"])

USAGE = """usage: agent-cleanup.py

Sweep stale Alfred runtime files:
  - old agent temp files
  - abandoned clean worktrees
  - expired spend, event, and transcript files
  - stale /tmp agent locks
  - stale GitHub in-flight claims when configured

Configuration is via ALFRED_* environment variables.
"""


if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    print(USAGE.rstrip())
    sys.exit(0)

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

TMP_SUFFIXES = [
    "debug-*",
    "prompt-*",
    "prbody-*",
    "wip-*",
    "out-*",
    "run-*",
]


def configured_tmp_prefixes() -> list[str]:
    """Return agent-owned /tmp prefixes cleanup is allowed to sweep."""
    prefixes: set[str] = set()
    raw = os.environ.get("ALFRED_CLEANUP_TMP_PREFIXES", "")
    prefixes.update(t.strip() for t in raw.split(",") if t.strip())

    conf_candidates = [
        ALFRED_HOME / "launchd" / "agents.conf",
        Path(__file__).resolve().parent.parent / "launchd" / "agents.conf",
    ]
    for conf in conf_candidates:
        if not conf.exists():
            continue
        with contextlib.suppress(OSError):
            for raw_line in conf.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("# "):
                    continue
                if line.startswith("#"):
                    line = line.lstrip("#").lstrip()
                if "\t" not in line:
                    continue
                label = line.split("\t", 1)[0].strip()
                if label:
                    prefixes.add(label.rsplit(".", 1)[-1])

    for script in Path(__file__).resolve().parent.glob("*.py"):
        prefixes.add(script.stem)

    return sorted(prefixes)


removed = 0
freed_mb = 0.0
for prefix in configured_tmp_prefixes():
    patterns = [f"{prefix}-{suffix}" for suffix in TMP_SUFFIXES]
    if prefix == "rasalghul":
        patterns.append("rasalghul-*")
    for pattern in patterns:
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

for p in Path("/tmp").glob(f"{AGENT}-*.json"):
    try:
        age_days = (NOW - p.stat().st_mtime) / ONE_DAY
        if age_days < 1:
            continue
        freed_mb += p.stat().st_size / (1024 * 1024)
        p.unlink(missing_ok=True)
        removed += 1
    except OSError:
        pass


def dirty_worktree_reason(wt: Path) -> str | None:
    """Return why a stale worktree must be preserved, or None when clean."""
    return worktree_risk_reason(wt)


# Sweep abandoned clean worktrees (>2h old). Dirty or unknown directories are
# kept so cleanup never destroys in-progress agent work.
wt_root = WORKTREE_ROOT
wt_removed = 0
wt_skipped = 0
wt_recovery_refs: list[str] = []
if wt_root.exists():
    for wt in wt_root.iterdir():
        try:
            age = NOW - wt.stat().st_mtime
            if age < 7200:  # < 2h, leave alone
                continue
            dirty_reason = dirty_worktree_reason(wt)
            if dirty_reason:
                wt_skipped += 1
                recovery_ref = create_recovery_ref(wt)
                if recovery_ref:
                    wt_recovery_refs.append(f"{wt} -> {recovery_ref}")
                    print(
                        f"[cleanup] worktree skipped: {wt} ({dirty_reason}; "
                        f"recovery={recovery_ref})",
                        file=sys.stderr,
                    )
                else:
                    print(f"[cleanup] worktree skipped: {wt} ({dirty_reason})", file=sys.stderr)
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


def sweep_extra_paths(
    *,
    paths: str,
    max_age_hours: int,
    now: float | None = None,
) -> dict[str, float]:
    """Sweep abandoned worktrees in operator-configured extra paths.

    ``paths`` is a colon-separated list (typically from the
    ``ALFRED_CLEANUP_EXTRA_PATHS`` env var). Each entry's children are
    treated as worktrees; entries older than ``max_age_hours`` and not
    dirty are removed via ``git worktree remove --force`` (against
    every repo in ``WORKSPACE`` that claims them) plus a defensive
    ``shutil.rmtree``. Dirty worktrees are skipped so cleanup never
    destroys in-progress work.

    Returns ``{"removed": int, "skipped": int, "freed_mb": float}``.

    Background: the fleet pool at ``$ALFRED_HOME/worktrees`` is swept
    by the block above; this helper extends that to operator-owned
    pools outside the fleet pool (e.g. a per-project ``.worktrees``
    directory where manual Claude Code sessions accumulate). Without
    this, those pools grow unbounded.
    """
    now_ts = time.time() if now is None else now
    max_age_seconds = max_age_hours * 3600
    removed = 0
    skipped = 0
    freed_mb = 0.0

    for raw in (paths or "").split(":"):
        path_str = raw.strip()
        if not path_str:
            continue
        root = Path(os.path.expanduser(path_str))
        if not root.is_dir():
            print(
                f"[cleanup] extra path skipped: {root} (not a directory)",
                file=sys.stderr,
            )
            continue
        for wt in root.iterdir():
            try:
                age = now_ts - wt.stat().st_mtime
                if age < max_age_seconds:
                    continue
                dirty_reason = dirty_worktree_reason(wt)
                if dirty_reason:
                    skipped += 1
                    recovery_ref = create_recovery_ref(wt)
                    if recovery_ref:
                        wt_recovery_refs.append(f"{wt} -> {recovery_ref}")
                        print(
                            f"[cleanup] extra worktree skipped: {wt} ({dirty_reason}; "
                            f"recovery={recovery_ref})",
                            file=sys.stderr,
                        )
                        continue
                    print(
                        f"[cleanup] extra worktree skipped: {wt} ({dirty_reason})",
                        file=sys.stderr,
                    )
                    continue
                try:
                    size_mb = sum(f.stat().st_size for f in wt.rglob("*") if f.is_file()) / (
                        1024 * 1024
                    )
                except OSError:
                    size_mb = 0.0
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
                removed += 1
                freed_mb += size_mb
            except OSError:
                pass

    return {"removed": removed, "skipped": skipped, "freed_mb": freed_mb}


# Operator-managed extra worktree pools (outside ALFRED_HOME).
# ``ALFRED_CLEANUP_EXTRA_PATHS`` is a colon-separated list; each entry
# is swept using the same dirty-skip rules as the fleet pool but with a
# configurable age threshold via ``ALFRED_CLEANUP_MAX_AGE_HOURS``
# (default 48h).
extra_paths_raw = os.environ.get("ALFRED_CLEANUP_EXTRA_PATHS", "").strip()
extra_max_age_hours = int(os.environ.get("ALFRED_CLEANUP_MAX_AGE_HOURS", "48"))
extra_stats = sweep_extra_paths(
    paths=extra_paths_raw,
    max_age_hours=extra_max_age_hours,
    now=NOW,
)

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

# Force-unlock dead or stale-identity agent locks. Keep healthy locks under
# 4h, and give freshly-created locks a brief mkdir-before-pid-write grace.
LOCK_MAX_AGE = 4 * 3600
LOCK_GRACE_SECONDS = 60
locks_unlocked = 0
for lock_dir in Path("/tmp").glob("agent-lock-*"):
    if not lock_dir.is_dir():
        continue
    try:
        age = NOW - lock_dir.stat().st_mtime
    except OSError:
        continue
    pid_file = lock_dir / "pid"
    pid_alive = False
    identity_status = False
    old_pid = 0
    try:
        old_pid = int(pid_file.read_text().strip())
        os.kill(old_pid, 0)
        pid_alive = True
        expected_agent = lock_dir.name.removeprefix("agent-lock-")
        identity_status = lock_pid_identity_status(
            lock_dir,
            old_pid,
            expected_agent=expected_agent,
        )
    except (ValueError, ProcessLookupError):
        pid_alive = False
    except OSError:
        pid_alive = False
    if old_pid == 0 and age <= LOCK_GRACE_SECONDS:
        continue
    if pid_alive and identity_status is None and age <= LOCK_MAX_AGE:
        continue
    if pid_alive and identity_status is False and age <= LOCK_GRACE_SECONDS:
        continue
    if pid_alive and identity_status is True and age <= LOCK_MAX_AGE:
        continue
    if pid_alive and old_pid and identity_status is True:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["pkill", "-TERM", "-P", str(old_pid)],
                capture_output=True,
                timeout=5,
            )
        with contextlib.suppress(OSError):
            os.kill(old_pid, signal.SIGTERM)
    shutil.rmtree(lock_dir, ignore_errors=True)
    locks_unlocked += 1

print(f"[cleanup] /tmp: {removed} files/dirs removed ({freed_mb:.1f} MB freed)")
print(f"[cleanup] worktrees: {wt_removed} abandoned removed, {wt_skipped} dirty/unknown skipped")
if extra_paths_raw:
    print(
        f"[cleanup] extra worktrees: {extra_stats['removed']} removed "
        f"({extra_stats['freed_mb']:.1f} MB freed, >{extra_max_age_hours}h), "
        f"{extra_stats['skipped']} dirty/unknown skipped"
    )
print(f"[cleanup] spend files: {spend_removed} removed (>{SPEND_RETENTION_DAYS}d)")
print(f"[cleanup] event logs: {events_removed} removed (>{EVENTS_RETENTION_DAYS}d)")
print(
    f"[cleanup] transcripts: {transcript_removed} removed ({transcript_freed_mb:.1f} MB freed, >{TRANSCRIPT_RETENTION_DAYS}d)"
)
print(f"[cleanup] stuck locks: {locks_unlocked} force-released (>{LOCK_MAX_AGE // 3600}h)")
if wt_skipped:
    recovery_note = ""
    if wt_recovery_refs:
        shown = "\n".join(f"- {line}" for line in wt_recovery_refs[:5])
        extra = "" if len(wt_recovery_refs) <= 5 else f"\n- ... {len(wt_recovery_refs) - 5} more"
        recovery_note = f"\nRecovery refs created:\n{shown}{extra}"
    slack_post(
        f"cleanup skipped {wt_skipped} stale worktree(s) because they were dirty "
        "or could not be proven safe to remove."
        f"{recovery_note}",
        severity="warn",
    )

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
            released = force_release_stale_claim(
                repo,
                entry["number"],
                sweep_id=sweep_id,
                released_codename=entry.get("codename"),
                released_firing_id=entry.get("firing_id"),
                label_drift=bool(entry.get("label_drift")),
            )
            if not released:
                raise RuntimeError("GitHub label/comment update returned false")
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
