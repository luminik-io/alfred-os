#!/usr/bin/env python3
"""One-screen health snapshot for an alfred-os host.

The reporter is deliberately generic: it reads the operator's
``launchd/agents.conf`` when present, falls back to the sample fleet, and
aggregates local state from ``$ALFRED_HOME/state`` plus per-agent locks under
``/tmp``.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE.parent / "lib",
    Path(os.environ.get("ALFRED_HOME", "")) / "lib",
):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import agent_runner  # noqa: E402

ALFRED_HOME = agent_runner.ALFRED_HOME
STATE_ROOT = agent_runner.STATE_ROOT
PAUSE_DIR = STATE_ROOT / "_paused"

IS_LINUX = sys.platform.startswith("linux")
# Linux uses systemd --user timers instead of launchd plists. The roster
# source is kept distinct so status reports the actually-deployed fleet
# regardless of which scheduler installed the units.
SYSTEMD_USER_DIR = Path(
    os.environ.get("ALFRED_SYSTEMD_USER_DIR", os.path.expanduser("~/.config/systemd/user"))
)

ENGINE_AWARE_AGENTS = {
    "bane",
    "batman",
    "drake",
    "lucius",
    "nightwing",
    "rasalghul",
    "robin",
}

DEFAULT_AGENT_NAMES = [
    "agent-cleanup",
    "agent-morning-brief",
    "automerge",
    "bane",
    "batman",
    "code-map-refresh",
    "drake",
    "fleet-doctor",
    "fleet-recap-morning",
    "fleet-recap-evening",
    "lucius",
    "nightwing",
    "rasalghul",
    "robin",
]


@dataclass
class AgentRecord:
    label: str
    codename: str
    script: str
    schedule: str
    log_stem: str
    role: str
    disabled: bool


@dataclass
class AgentSnapshot:
    agent: str
    label: str
    role: str
    schedule: str
    loaded: bool
    disabled: bool
    engine: str | None
    locked: bool
    stale_lock: bool
    lock_pid: int | None
    lock_age_seconds: float | None
    paused: bool
    paused_since: str | None
    last_fired: str | None
    last_event: str | None
    today_firings: int
    today_successes: int
    today_failures: int
    today_consecutive_failures: int
    today_turns: int
    today_cost_usd: float
    blocked_until: str | None
    last_stderr_tail: str | None
    approval_wait_firing_id: str | None
    approval_wait_issue_numbers: list[int]
    approval_wait_created_at: str | None
    approval_wait_age_seconds: float | None
    approval_wait_pid: int | None
    approval_wait_pid_alive: bool | None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_z(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _today_str() -> str:
    # Match SpendState's UTC day key (see agent_runner/spend.today_str()).
    # Local-time readers caused `no spend today` false negatives on
    # non-UTC hosts during local/UTC date-skew windows (PR #99 follow-up).
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _event_firings_for_day(events_dir: Path, day: str) -> int:
    if not events_dir.is_dir():
        return 0
    day_prefix = day.replace("-", "")
    total = 0
    for path in events_dir.glob(f"{day_prefix}-*.jsonl"):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "firing_started":
                total += 1
                break
    return total


def _codename_from_label(label: str) -> str:
    return label.rsplit(".", 1)[-1] if "." in label else label


def _agents_conf_candidates() -> list[Path]:
    return [
        ALFRED_HOME / "launchd" / "agents.conf",
        _HERE.parent / "launchd" / "agents.conf",
        _HERE.parent / "launchd" / "agents.conf.example",
    ]


def _parse_agents_conf(path: Path) -> list[AgentRecord]:
    if not path.exists():
        return []
    out: list[AgentRecord] = []
    for raw in path.read_text().splitlines():
        stripped = raw.lstrip()
        if not stripped:
            continue
        if stripped.startswith("# ") or stripped == "#":
            continue
        disabled = False
        if stripped.startswith("#"):
            if "\t" not in stripped:
                continue
            disabled = True
            stripped = stripped.lstrip("#").lstrip()
        cols = stripped.split("\t")
        cols = cols + [""] * (6 - len(cols))
        label, script, schedule, _needs_java, log_stem, role = cols[:6]
        if not label:
            continue
        codename = _codename_from_label(label)
        out.append(
            AgentRecord(
                label=label,
                codename=codename,
                script=script,
                schedule=schedule or "-",
                log_stem=log_stem or label,
                role=role or "-",
                disabled=disabled,
            )
        )
    return out


def configured_agents() -> list[AgentRecord]:
    for path in _agents_conf_candidates():
        records = _parse_agents_conf(path)
        if records:
            return records
    return [
        AgentRecord(
            label=f"alfred.{name}",
            codename=name,
            script=f"{name}.py",
            schedule="-",
            log_stem=f"alfred.{name}",
            role="-",
            disabled=False,
        )
        for name in DEFAULT_AGENT_NAMES
    ]


def _loaded_label_set() -> set[str]:
    """Labels currently loaded into the host scheduler.

    macOS: ``launchctl list`` (the label is the last column).
    Linux: ``systemctl --user list-units`` for active ``*.timer`` units; the
    label is the timer unit name with the ``.timer`` suffix stripped.

    Returns an empty set on hosts where the scheduler binary is unavailable
    rather than crashing, the table then renders every row as "not loaded".
    """
    if IS_LINUX:
        try:
            res = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "--type=timer",
                    "--state=active",
                    "--no-legend",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return set()
        if res.returncode != 0:
            return set()
        labels: set[str] = set()
        for line in res.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit = parts[0]
            if unit.endswith(".timer"):
                labels.add(unit[: -len(".timer")])
        return labels

    if platform.system() != "Darwin":
        return set()
    try:
        res = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return set()
    if res.returncode != 0:
        return set()
    labels = set()
    for line in res.stdout.splitlines():
        parts = line.split()
        if parts:
            labels.add(parts[-1])
    return labels


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_status(agent: str) -> tuple[bool, bool, int | None, float | None]:
    lock_dir = Path("/tmp") / f"agent-lock-{agent}"
    if not lock_dir.exists():
        return False, False, None, None
    try:
        age = max(0.0, datetime.now().timestamp() - lock_dir.stat().st_mtime)
    except OSError:
        age = None
    try:
        pid = int((lock_dir / "pid").read_text().strip())
    except (OSError, ValueError):
        return False, True, None, age
    if not _pid_alive(pid):
        return False, True, pid, age
    identity = agent_runner.lock_pid_identity_status(lock_dir, pid, expected_agent=agent)
    if identity is False:
        return False, True, pid, age
    return True, False, pid, age


def _approval_wait_status(agent: str) -> dict[str, Any]:
    if agent != "batman":
        return {
            "firing_id": None,
            "issue_numbers": [],
            "created_at": None,
            "age_seconds": None,
            "pid": None,
            "pid_alive": None,
        }
    wait_dir = STATE_ROOT / "batman" / "approval-waits"
    if not wait_dir.is_dir():
        return {
            "firing_id": None,
            "issue_numbers": [],
            "created_at": None,
            "age_seconds": None,
            "pid": None,
            "pid_alive": None,
        }
    markers = sorted(wait_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for marker in markers:
        payload = _read_json(marker)
        if not payload:
            continue
        issue_numbers: list[int] = []
        for issue in payload.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            try:
                issue_numbers.append(int(issue.get("number")))
            except (TypeError, ValueError):
                continue
        created_at = payload.get("created_at")
        created_dt = _parse_iso_z(created_at)
        age_seconds = None
        if created_dt is not None:
            age_seconds = (datetime.now(UTC) - created_dt).total_seconds()
        try:
            pid = int(payload.get("pid"))
        except (TypeError, ValueError):
            pid = None
        return {
            "firing_id": payload.get("firing_id"),
            "issue_numbers": issue_numbers,
            "created_at": created_at,
            "age_seconds": age_seconds,
            "pid": pid,
            "pid_alive": _pid_alive(pid) if pid is not None else None,
        }
    return {
        "firing_id": None,
        "issue_numbers": [],
        "created_at": None,
        "age_seconds": None,
        "pid": None,
        "pid_alive": None,
    }


def _engine(agent: str) -> str | None:
    if agent not in ENGINE_AWARE_AGENTS:
        return None
    return agent_runner.agent_engine(
        agent,
        legacy_env="ALFRED_REVIEW_ENGINE" if agent == "rasalghul" else None,
        legacy_state_file=STATE_ROOT / "review-engine" if agent == "rasalghul" else None,
    )


def snapshot_agent(record: AgentRecord, *, loaded_labels: set[str]) -> AgentSnapshot:
    locked, stale_lock, lock_pid, lock_age_seconds = _lock_status(record.codename)
    pause_marker = PAUSE_DIR / record.codename
    paused = pause_marker.exists()
    paused_since = None
    if paused:
        try:
            paused_since = pause_marker.read_text().strip() or _iso(pause_marker.stat().st_mtime)
        except OSError:
            paused_since = None

    stdout_path = Path(f"/tmp/{record.log_stem}.stdout")
    stderr_path = Path(f"/tmp/{record.log_stem}.stderr")
    last_fired = _iso(stdout_path.stat().st_mtime) if stdout_path.exists() else None

    events_dir = STATE_ROOT / record.codename / "events"
    last_event = None
    if events_dir.is_dir():
        events = sorted(events_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if events:
            last_event = _iso(events[0].stat().st_mtime)

    spend = _read_json(STATE_ROOT / record.codename / f"spend-{_today_str()}.json")
    today_firings = int(spend.get("firings_today") or 0)
    if today_firings == 0:
        today_firings = _event_firings_for_day(events_dir, _today_str())
    approval_wait = _approval_wait_status(record.codename)

    last_stderr_tail = None
    if (
        stderr_path.exists()
        and stdout_path.exists()
        and stderr_path.stat().st_mtime > stdout_path.stat().st_mtime
    ):
        try:
            lines = stderr_path.read_text(errors="replace").splitlines()
            last_stderr_tail = "\n".join(lines[-3:]) if lines else None
        except OSError:
            pass

    return AgentSnapshot(
        agent=record.codename,
        label=record.label,
        role=record.role,
        schedule=record.schedule,
        loaded=record.label in loaded_labels,
        disabled=record.disabled,
        engine=_engine(record.codename),
        locked=locked,
        stale_lock=stale_lock,
        lock_pid=lock_pid,
        lock_age_seconds=lock_age_seconds,
        paused=paused,
        paused_since=paused_since,
        last_fired=last_fired,
        last_event=last_event,
        today_firings=today_firings,
        today_successes=spend.get("successes_today", 0),
        today_failures=spend.get("failures_today", 0),
        today_consecutive_failures=spend.get("consecutive_failures", 0),
        today_turns=spend.get("turns_today", 0),
        today_cost_usd=float(spend.get("cost_usd_today", 0.0)),
        blocked_until=spend.get("blocked_until"),
        last_stderr_tail=last_stderr_tail,
        approval_wait_firing_id=approval_wait["firing_id"],
        approval_wait_issue_numbers=approval_wait["issue_numbers"],
        approval_wait_created_at=approval_wait["created_at"],
        approval_wait_age_seconds=approval_wait["age_seconds"],
        approval_wait_pid=approval_wait["pid"],
        approval_wait_pid_alive=approval_wait["pid_alive"],
    )


def global_state() -> dict[str, Any]:
    blocked = STATE_ROOT / "global-blocked-until.json"
    webhook = STATE_ROOT / "slack-webhook.cache"
    webhook_age = None
    if webhook.exists():
        webhook_age = round((datetime.now().timestamp() - webhook.stat().st_mtime) / 3600, 1)
    return {
        "global_block": _read_json(blocked) if blocked.exists() else None,
        "slack_webhook_cache_age_hours": webhook_age,
        "host_scheduler": (
            "launchd"
            if platform.system() == "Darwin"
            else "systemd --user"
            if IS_LINUX
            else "manual/non-macOS"
        ),
    }


def render_table(snapshots: list[AgentSnapshot], globals_: dict[str, Any]) -> str:
    lines = [
        f"alfred-status @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"alfred_home={ALFRED_HOME}",
        "",
        "global:",
        f"  scheduler: {globals_['host_scheduler']}",
    ]
    if globals_["global_block"]:
        lines.append(f"  global block: {globals_['global_block']}")
    else:
        lines.append("  global block: none")
    cache_age = globals_["slack_webhook_cache_age_hours"]
    if cache_age is None:
        lines.append("  slack webhook cache: missing")
    else:
        lines.append(f"  slack webhook cache: {cache_age:.1f}h old")
    lines.append("")

    header = f"{'agent':<22} {'load':<5} {'eng':<6} {'fired':<8} {'fires':<5} {'ok':<3} {'fail':<4} {'streak':<6} {'turns':<6} state"
    lines.append(header)
    lines.append("-" * len(header))
    for s in snapshots:
        if s.paused:
            loaded = "pause"
        elif s.disabled:
            loaded = "off"
        else:
            loaded = "yes" if s.loaded else "no"
        if s.last_fired:
            ts = datetime.strptime(s.last_fired, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            delta = datetime.now(UTC) - ts
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            fired = f"{hours}h{mins:02d}m" if hours else f"{mins}m"
        else:
            fired = "never"
        state_chunks: list[str] = []
        if s.paused:
            state_chunks.append(f"paused since {s.paused_since}" if s.paused_since else "paused")
        if s.approval_wait_issue_numbers:
            issues = ", ".join(f"#{n}" for n in s.approval_wait_issue_numbers)
            if s.approval_wait_pid_alive is False:
                state_chunks.append(f"approval wait dead {issues} pid={s.approval_wait_pid}")
            else:
                state_chunks.append(
                    f"awaiting approval {issues} ({_duration(s.approval_wait_age_seconds)})"
                )
        elif s.locked:
            state_chunks.append("in-flight")
        if s.stale_lock:
            state_chunks.append(f"stale lock pid={s.lock_pid}" if s.lock_pid else "stale lock")
        if s.today_consecutive_failures >= 3:
            state_chunks.append(f"{s.today_consecutive_failures} fails in a row")
        if s.blocked_until:
            state_chunks.append(f"blocked until {s.blocked_until}")
        if s.last_stderr_tail and not s.locked:
            state_chunks.append("stderr fresh")
        state = ", ".join(state_chunks) if state_chunks else "ok"
        lines.append(
            f"{s.agent:<22} {loaded:<5} {(s.engine or '-'):<6} {fired:<8} "
            f"{s.today_firings:<5} {s.today_successes:<3} {s.today_failures:<4} "
            f"{s.today_consecutive_failures:<6} {s.today_turns:<6} {state}"
        )
    return "\n".join(lines)


def render_slack(snapshots: list[AgentSnapshot], globals_: dict[str, Any]) -> str:
    total_firings = sum(s.today_firings for s in snapshots)
    total_successes = sum(s.today_successes for s in snapshots)
    total_failures = sum(s.today_failures for s in snapshots)
    total_turns = sum(s.today_turns for s in snapshots)
    flagged = [
        s
        for s in snapshots
        if s.today_consecutive_failures >= 3
        or (
            not s.loaded
            and not s.disabled
            and not s.paused
            and (platform.system() == "Darwin" or IS_LINUX)
        )
        or s.blocked_until
        or s.stale_lock
        or (s.approval_wait_issue_numbers and s.approval_wait_pid_alive is False)
    ]
    approval_waits = [
        s
        for s in snapshots
        if s.approval_wait_issue_numbers and s.approval_wait_pid_alive is not False
    ]
    lines = [f"*Fleet recap - {_today_str()}*"]
    lines.append(
        f"`{total_firings} firings, {total_successes} ok, {total_failures} fail | {total_turns} turns`"
    )
    if globals_["global_block"]:
        lines.append(f"global block: {globals_['global_block']}")
    if flagged:
        lines.append("")
        lines.append("*Flagged:*")
        for s in flagged:
            why: list[str] = []
            if (
                not s.loaded
                and not s.disabled
                and not s.paused
                and (platform.system() == "Darwin" or IS_LINUX)
            ):
                why.append("not loaded")
            if s.today_consecutive_failures >= 3:
                why.append(f"{s.today_consecutive_failures} consecutive fails")
            if s.blocked_until:
                why.append(f"blocked until {s.blocked_until}")
            if s.stale_lock:
                why.append("stale lock")
            if s.approval_wait_issue_numbers and s.approval_wait_pid_alive is False:
                issues = ", ".join(f"#{n}" for n in s.approval_wait_issue_numbers)
                why.append(f"dead approval wait {issues}")
            lines.append(f"  - {s.agent}: {', '.join(why)}")
    else:
        lines.append("No active agents flagged.")
    if approval_waits:
        lines.append("")
        lines.append("*Approval waits:*")
        for s in approval_waits:
            issues = ", ".join(f"#{n}" for n in s.approval_wait_issue_numbers)
            lines.append(
                f"  - {s.agent}: waiting on {issues} for {_duration(s.approval_wait_age_seconds)}"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="alfred-os fleet health snapshot")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--slack", action="store_true")
    args = parser.parse_args()

    records = configured_agents()
    snapshots = [snapshot_agent(record, loaded_labels=_loaded_label_set()) for record in records]
    globals_ = global_state()

    if args.json:
        print(
            json.dumps(
                {
                    "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "global": globals_,
                    "agents": [asdict(s) for s in snapshots],
                },
                indent=2,
            )
        )
        return 0

    print(render_table(snapshots, globals_))
    if args.slack:
        ok = agent_runner.slack_post(render_slack(snapshots, globals_))
        print()
        print(f"slack_post: {'ok' if ok else 'failed'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
