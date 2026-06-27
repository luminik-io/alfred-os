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

import collections
import contextlib
import os
import re
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
# check_disk=False: the janitor must run *because* the disk is low, not
# skip when it is. Every other agent inherits the disk-pressure gate; the
# cleanup agent is the one that reclaims space, so it opts out.
PREFLIGHT = PreflightSpec(
    agent=AGENT,
    env_vars=[],
    bins=["git"],
    check_disk=False,
)

USAGE = """usage: agent-cleanup.py [--emergency] [--scheduled]

Sweep stale Alfred runtime files:
  - old agent temp files
  - abandoned clean worktrees
  - expired spend, event, and transcript files
  - stale /tmp agent locks
  - stale GitHub in-flight claims when configured

Options:
  --emergency   Aggressive reclamation for disk-pressure recovery. Lowers
                the abandoned-worktree age threshold, auto-discovers and
                sweeps every .worktrees pool under WORKSPACE, shortens
                transcript/event retention to an emergency floor, and
                clears Alfred's own /tmp debug dirs regardless of the
                1-day age gate. Still 100% Alfred-owned with the same
                dirty-skip + recovery-ref safety as a normal sweep.
  --scheduled   Opt-in proactive reclaim of REGENERABLE build output on the
                normal daily pass instead of only when a firing already hit
                the disk floor: Xcode DerivedData, npm cache, Docker build
                cache, dangling images, and anonymous orphaned volumes when
                Docker server version and effective API can be verified as
                anonymous-only for bare volume prune. It does not change the
                age gates or retention floors of the other sweep categories,
                and unlike --emergency it never prunes named Docker volumes
                (which may hold dev data). Disable per category with
                ALFRED_EMERGENCY_SKIP_DOCKER=1 or
                ALFRED_EMERGENCY_SKIP_DEV_CACHES=1. Also enabled by
                ALFRED_CLEANUP_SCHEDULED_RECLAIM=1 so a launchd entry that
                cannot pass flags can still opt in via config.

Configuration is via ALFRED_* environment variables.
"""


if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
    print(USAGE.rstrip())
    sys.exit(0)

# --emergency: aggressive (but still Alfred-owned, dirty-skip-preserving)
# reclamation, fired by the disk-pressure preflight gate when free space
# is critical. Lowers age gates and retention floors so a full disk can
# recover before the next firing crash-loops on ENOSPC.
EMERGENCY = "--emergency" in sys.argv[1:]
# --scheduled (or ALFRED_CLEANUP_SCHEDULED_RECLAIM=1) opts the daily pass
# into the dev-cache + Docker reclaim that otherwise runs only reactively
# under --emergency. The reclaim targets REGENERABLE build output only (Xcode
# DerivedData, npm cache, Docker build cache, dangling images, ANONYMOUS
# volumes when Docker server version and effective API can be verified as
# anonymous-only for bare volume prune); the next build or run recreates whatever
# it needs. It does NOT lower the age gates or retention floors of the OTHER
# sweep categories (temp files, worktrees), and unlike --emergency it never
# prunes named Docker volumes (which may hold dev data). Disable per category with
# ALFRED_EMERGENCY_SKIP_DOCKER=1 / ALFRED_EMERGENCY_SKIP_DEV_CACHES=1. An
# --emergency run already does this reclaim (plus the named-volume prune), so
# the flag is redundant there but harmless. The env var lets a launchd entry
# (which passes no flags to the script) turn the scheduled reclaim on.
_SCHEDULED_RECLAIM_ENV = os.environ.get("ALFRED_CLEANUP_SCHEDULED_RECLAIM", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SCHEDULED_RECLAIM = "--scheduled" in sys.argv[1:] or _SCHEDULED_RECLAIM_ENV
# Run the regenerable dev-cache + Docker reclaim when EITHER an emergency
# pass demands it or the operator opted the scheduled pass in.
RECLAIM_DEV_CACHES = EMERGENCY or SCHEDULED_RECLAIM
# Reject unknown flags only when run as the actual CLI. When the test
# suite imports this script as a module, sys.argv belongs to pytest and
# must not trip the parser (the procedural body would exit(2) before its
# helper functions are defined).
if __name__ == "__main__":
    for _unknown in (a for a in sys.argv[1:] if a not in {"--emergency", "--scheduled"}):
        print(f"agent-cleanup.py: unknown argument: {_unknown} (see --help)", file=sys.stderr)
        sys.exit(2)

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


# In emergency mode the 1-day age gate on Alfred's own /tmp debug dirs is
# dropped: those dirs are 100% Alfred-owned scratch space, so clearing
# them regardless of age is safe and reclaims space immediately.
TMP_MIN_AGE_DAYS = 0.0 if EMERGENCY else 1.0

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
                if age_days < TMP_MIN_AGE_DAYS:
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


# Sweep abandoned clean worktrees (>2h old, or >15min in emergency mode).
# Dirty or unknown directories are kept so cleanup never destroys
# in-progress agent work - the dirty-skip + recovery-ref path is identical
# in both modes; emergency only lowers the *age* gate.
FLEET_WT_MIN_AGE_SECONDS = 900 if EMERGENCY else 7200  # 15min vs 2h
wt_root = WORKTREE_ROOT
wt_removed = 0
wt_skipped = 0
wt_freed_mb = 0.0
wt_recovery_refs: list[str] = []
if wt_root.exists():
    for wt in wt_root.iterdir():
        try:
            age = NOW - wt.stat().st_mtime
            if age < FLEET_WT_MIN_AGE_SECONDS:  # too fresh, leave alone
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
            # Measure before removal so the reported total reflects the
            # fleet-pool reclamation too (mirrors sweep_extra_paths).
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
            wt_removed += 1
            wt_freed_mb += size_mb
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


def discover_worktree_pools(
    *,
    root: Path,
    max_depth: int = 3,
) -> list[str]:
    """Find ``.worktrees`` pool directories under ``root`` (bounded depth).

    Returns the directories as a colon-separated-ready list of absolute
    path strings. Any directory literally named ``.worktrees`` found at
    depth ``<= max_depth`` below ``root`` is treated as an extra worktree
    pool and swept with the same dirty-skip + recovery-ref rules as the
    fleet pool.

    This closes the real incident class: operators run manual Claude Code
    sessions that leave per-project ``product/<repo>/.worktrees/`` pools
    full of ``node_modules`` (tens of GB) that ``ALFRED_CLEANUP_EXTRA_PATHS``
    only swept if set by hand. Auto-discovery makes the janitor find them.

    Opt out with ``ALFRED_CLEANUP_AUTODISCOVER=0``.
    """
    if not root.is_dir():
        return []
    found: list[str] = []
    # BFS with an explicit depth bound so a deep tree can't make this walk
    # unbounded. ``deque.popleft`` makes this a genuine FIFO breadth-first
    # walk (a plain ``list.pop`` would be LIFO/DFS). We do not descend into
    # a discovered ``.worktrees`` pool (its children are worktrees, not
    # more pools). ``root`` itself is depth 0, so a child is depth 1; a
    # ``.worktrees`` directory counts as discovered only when its own depth
    # is ``<= max_depth`` (matching the docstring).
    frontier: collections.deque[tuple[Path, int]] = collections.deque([(root, 0)])
    while frontier:
        current, depth = frontier.popleft()
        # No node beyond max_depth is ever enqueued, but guard defensively.
        if depth >= max_depth:
            continue
        try:
            children = [c for c in current.iterdir() if c.is_dir()]
        except OSError:
            continue
        for child in children:
            child_depth = depth + 1
            if child.name == ".worktrees":
                found.append(str(child))
                continue  # do not recurse into a pool
            # Skip the fleet pool (handled separately) and noisy package
            # dirs we should never walk into looking for pools.
            if child.name in {"node_modules", ".git"}:
                continue
            if child_depth < max_depth:
                frontier.append((child, child_depth))
    return sorted(set(found))


def _parse_docker_reclaimed_mb(stdout: str) -> float:
    """Parse ``Total reclaimed space: <num><unit>B`` from docker prune output.

    Docker reports the unit-suffixed total (e.g. ``1.5GB``, ``512MB``, ``0B``).
    Units are normalized to MB; an empty/bare-byte unit is treated as bytes.
    Returns 0.0 when the line is absent.
    """
    match = re.search(r"Total reclaimed space:\s*([\d.]+)\s*([kKMGT]?)B", stdout)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2)
    factors = {
        "": 1.0 / (1024 * 1024),  # bytes -> MB
        "k": 1.0 / 1024,  # kB (Docker lowercase) -> MB
        "K": 1.0 / 1024,  # KB -> MB
        "M": 1.0,  # MB
        "G": 1024.0,  # GB -> MB
        "T": 1024.0 * 1024.0,  # TB -> MB
    }
    return value * factors[unit]


def _parse_major_minor(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _docker_version_field(docker: str, template: str) -> tuple[int, int] | None:
    """Return one Docker version field as major/minor, or ``None`` if unknown."""
    try:
        proc = subprocess.run(
            [docker, "version", "--format", template],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _parse_major_minor(proc.stdout or "")


def _docker_server_version(docker: str) -> tuple[int, int] | None:
    """Return the Docker server major/minor version, or ``None`` if unknown."""
    return _docker_version_field(docker, "{{.Server.Version}}")


def _docker_client_api_version(docker: str) -> tuple[int, int] | None:
    """Return the Docker API version the CLI will use, or ``None`` if unknown."""
    return _docker_version_field(docker, "{{.Client.APIVersion}}")


def _docker_volume_prune_is_anonymous_only(docker: str) -> bool:
    """True when bare ``docker volume prune -f`` is safe for scheduled cleanup.

    Docker 23.0 / Engine API 1.42 changed the default bare volume prune behavior
    to anonymous-only and introduced ``--all`` for named volumes. Older Docker
    releases, or a client forced down to an older negotiated API via
    DOCKER_API_VERSION, prune every unused volume with the bare command,
    including named dev-data volumes. A scheduled daily pass must therefore skip
    volume pruning unless it can prove both the server and effective client API
    are new enough.
    """
    server_version = _docker_server_version(docker)
    api_version = _docker_client_api_version(docker)
    return (
        server_version is not None
        and server_version >= (23, 0)
        and api_version is not None
        and api_version >= (1, 42)
    )


def reclaim_emergency_docker() -> tuple[float, int]:
    """Reclaim regenerable Docker artifacts.

    Runs under --emergency and under the opt-in --scheduled daily reclaim.
    Like the dev-cache sweep, this targets machine-wide build output that can
    wedge the whole fleet off disk while every workspace-scoped pass reclaims
    0 MB. Docker's build cache, dangling images, and orphaned volumes are all
    regenerable: the next build or run recreates whatever it needs. We run only
    SAFE prunes that never touch a running or stopped container or its data:

    * ``docker builder prune -f``        -> build cache only
    * ``docker image prune -f``          -> dangling images only (NOT ``-a``)
    * ``docker volume prune -f``         -> anonymous orphaned volumes only,
      scheduled only when Docker >= 23.0 and API >= 1.42 verify that behavior
    * ``docker volume prune --all -f``   -> ALSO named orphaned volumes,
      EMERGENCY ONLY (see below)

    Since Docker 23.0 / Engine API 1.42, a bare ``docker volume prune -f``
    removes only anonymous volumes. ``--all`` also removes NAMED orphaned
    volumes, which can hold local dev data (a database, a cache) merely detached
    from a stopped container. Routinely deleting those on a scheduled daily pass
    could destroy data, so ``--all`` is gated to ``--emergency`` (the disk is
    already full and recovery outranks the risk). The scheduled pass prunes only
    anonymous volumes. Docker <23 or API <1.42 treats the bare volume prune as
    named-volume-capable, so the scheduled path skips volume pruning there rather
    than risking detached named dev volumes. The emergency path still uses
    explicit ``--all``; on older Docker that flag exits non-zero with empty
    stdout, so the parser reads 0.0 MB and the run is a no-op rather than a
    regression.

    We never run ``docker container prune`` and never pass ``-a`` to the image
    prune, so running containers and their data are always preserved. Opt out
    entirely with ALFRED_EMERGENCY_SKIP_DOCKER=1 (applies to scheduled too).

    Returns ``(freed_mb, prunes_reclaimed)`` where ``prunes_reclaimed`` counts
    the prune commands that freed more than 0 bytes.
    """
    if not RECLAIM_DEV_CACHES or os.environ.get("ALFRED_EMERGENCY_SKIP_DOCKER") == "1":
        return 0.0, 0
    docker = shutil.which("docker")
    if docker is None:
        return 0.0, 0
    # Named orphaned volumes (``--all``) may hold dev data; only an emergency
    # (disk already full) justifies removing them. A scheduled pass prunes
    # anonymous volumes only when Docker can prove bare prune is anonymous-only.
    volume_prune: list[str] | None = None
    if EMERGENCY:
        volume_prune = [docker, "volume", "prune", "--all", "-f"]
    elif _docker_volume_prune_is_anonymous_only(docker):
        volume_prune = [docker, "volume", "prune", "-f"]
    commands = [
        [docker, "builder", "prune", "-f"],
        [docker, "image", "prune", "-f"],
    ]
    if volume_prune is not None:
        commands.append(volume_prune)
    freed_mb = 0.0
    reclaimed = 0
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        mb = _parse_docker_reclaimed_mb(proc.stdout or "")
        if mb > 0:
            freed_mb += mb
            reclaimed += 1
    return freed_mb, reclaimed


# Operator-managed extra worktree pools (outside ALFRED_HOME).
# ``ALFRED_CLEANUP_EXTRA_PATHS`` is a colon-separated list; each entry
# is swept using the same dirty-skip rules as the fleet pool but with a
# configurable age threshold via ``ALFRED_CLEANUP_MAX_AGE_HOURS``
# (default 48h). In emergency mode the age threshold drops to the
# emergency floor so freshly-abandoned (but clean) pools are reclaimed.
extra_paths_raw = os.environ.get("ALFRED_CLEANUP_EXTRA_PATHS", "").strip()
extra_max_age_hours = int(os.environ.get("ALFRED_CLEANUP_MAX_AGE_HOURS", "48"))
EMERGENCY_MAX_AGE_HOURS = int(os.environ.get("ALFRED_CLEANUP_EMERGENCY_MAX_AGE_HOURS", "1"))
if EMERGENCY:
    extra_max_age_hours = min(extra_max_age_hours, EMERGENCY_MAX_AGE_HOURS)

# Auto-discover ``.worktrees`` pools under WORKSPACE (opt-out via
# ALFRED_CLEANUP_AUTODISCOVER=0). Merge them with the operator-configured
# extra paths so both are swept under the same dirty-skip rules.
autodiscover = os.environ.get("ALFRED_CLEANUP_AUTODISCOVER", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
discovered_pools: list[str] = []
if autodiscover:
    discovered_pools = discover_worktree_pools(root=WORKSPACE)

all_extra_paths = [p for p in extra_paths_raw.split(":") if p.strip()]
for pool in discovered_pools:
    if pool not in all_extra_paths:
        all_extra_paths.append(pool)
combined_extra_paths = ":".join(all_extra_paths)

extra_stats = sweep_extra_paths(
    paths=combined_extra_paths,
    max_age_hours=extra_max_age_hours,
    now=NOW,
)

SPEND_RETENTION_DAYS = int(os.environ.get("ALFRED_SPEND_RETENTION_DAYS", "90"))
TRANSCRIPT_RETENTION_DAYS = int(os.environ.get("ALFRED_TRANSCRIPT_RETENTION_DAYS", "30"))
EVENTS_RETENTION_DAYS = int(os.environ.get("ALFRED_EVENTS_RETENTION_DAYS", "30"))

# Emergency retention floors: shorten transcript/event retention so a
# disk-pressure pass reclaims their bulk. Spend ledgers are deliberately
# NOT shortened - metrics needs the 90-day history and they are tiny. The
# floors clamp the configured retention DOWN, never up, so an operator who
# already runs tighter retention keeps it.
if EMERGENCY:
    EMERGENCY_TRANSCRIPT_RETENTION_DAYS = int(
        os.environ.get("ALFRED_EMERGENCY_TRANSCRIPT_RETENTION_DAYS", "3")
    )
    EMERGENCY_EVENTS_RETENTION_DAYS = int(
        os.environ.get("ALFRED_EMERGENCY_EVENTS_RETENTION_DAYS", "3")
    )
    TRANSCRIPT_RETENTION_DAYS = min(TRANSCRIPT_RETENTION_DAYS, EMERGENCY_TRANSCRIPT_RETENTION_DAYS)
    EVENTS_RETENTION_DAYS = min(EVENTS_RETENTION_DAYS, EMERGENCY_EVENTS_RETENTION_DAYS)

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

if EMERGENCY:
    print("[cleanup] EMERGENCY mode: aggressive thresholds (disk-pressure recovery)")

# Reclaim well-known regenerable developer caches that live OUTSIDE the
# workspace. Every other pass here is workspace-scoped, so a host whose free
# space is eaten by Xcode DerivedData or the npm cache can wedge the whole
# fleet off disk while each sweep reclaims 0 MB and the preflight gate keeps
# skipping firings. These two are pure build/download output: Xcode recreates
# DerivedData on the next build, npm refetches its cache on the next install.
# Runs under --emergency and under the opt-in --scheduled daily reclaim so the
# host recovers regenerable build output before disk pressure forces an
# emergency pass. Opt out with ALFRED_EMERGENCY_SKIP_DEV_CACHES=1.
dev_cache_freed_mb = 0.0
dev_caches_cleared = 0
if RECLAIM_DEV_CACHES and os.environ.get("ALFRED_EMERGENCY_SKIP_DEV_CACHES") != "1":
    HOME = Path.home()
    DEV_CACHE_ROOTS = [
        HOME / "Library" / "Developer" / "Xcode" / "DerivedData",  # macOS Xcode
        HOME / ".npm" / "_cacache",  # npm content-addressable cache
    ]
    for cache_root in DEV_CACHE_ROOTS:
        if not cache_root.is_dir():
            continue
        # Size is best-effort and must NEVER gate deletion: a single locked or
        # permission-denied file inside DerivedData would otherwise skip the
        # whole rmtree and leave the disk full (the exact failure this fixes).
        # rmtree(ignore_errors=True) already tolerates per-file errors. Skip
        # symlinks so the count never includes bytes rmtree will not free.
        size_mb = 0.0
        for f in cache_root.rglob("*"):
            try:
                if f.is_symlink() or not f.is_file():
                    continue
                size_mb += f.lstat().st_size / (1024 * 1024)
            except OSError:
                continue
        shutil.rmtree(cache_root, ignore_errors=True)
        if not cache_root.exists():
            dev_cache_freed_mb += size_mb
            dev_caches_cleared += 1
# In EMERGENCY mode, also reclaim regenerable Docker build cache, dangling
# images, and orphaned volumes (SAFE prunes only - running and stopped
# containers and their data are never touched). Opt out with
# ALFRED_EMERGENCY_SKIP_DOCKER=1.
dock_freed_mb, dock_n = reclaim_emergency_docker()
print(f"[cleanup] /tmp: {removed} files/dirs removed ({freed_mb:.1f} MB freed)")
print(
    f"[cleanup] worktrees: {wt_removed} abandoned removed "
    f"({wt_freed_mb:.1f} MB freed), {wt_skipped} dirty/unknown skipped"
)
if discovered_pools:
    print(f"[cleanup] auto-discovered {len(discovered_pools)} .worktrees pool(s) under WORKSPACE")
if combined_extra_paths:
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
if RECLAIM_DEV_CACHES:
    print(
        f"[cleanup] dev caches: {dev_caches_cleared} reclaimed "
        f"({dev_cache_freed_mb:.1f} MB freed, Xcode DerivedData + npm cache)"
    )
    if os.environ.get("ALFRED_EMERGENCY_SKIP_DOCKER") == "1":
        print("[cleanup] docker: skipped (ALFRED_EMERGENCY_SKIP_DOCKER=1)")
    else:
        print(
            f"[cleanup] docker: {dock_n} prune(s) reclaimed "
            f"({dock_freed_mb:.1f} MB freed, build cache + dangling images "
            f"+ orphaned volumes)"
        )
total_freed_mb = (
    freed_mb
    + wt_freed_mb
    + extra_stats["freed_mb"]
    + transcript_freed_mb
    + dev_cache_freed_mb
    + dock_freed_mb
)
print(f"[cleanup] total reclaimed: {total_freed_mb:.1f} MB")
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
                max_age_hours=int(entry.get("max_age_hours") or CLAIM_MAX_AGE_HOURS),
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
