"""Operator-defined runtime agents.

The shipped Alfred roster is a stable engineering fleet. The roster-theme
layer can rename that fleet for humans, but some operators need one more thing:
a local role that is scheduled and executable without hand-editing
``launchd/agents.conf`` or writing a bespoke ``bin/<agent>.py`` wrapper.

This module is the small contract for that path. A custom agent is persisted
under ``$ALFRED_HOME/state/custom-agents/custom-agents.json`` and compiles to
the same six-column scheduler row the built-in fleet already uses:

``label, script, schedule, needs_java, log_stem, role``.

The executable script is ``bin/custom-agent.py``. It reads this same manifest by
``AGENT_CODENAME`` and invokes the configured engine through ``agent_runner`` so
custom roles get normal locks, event logs, spend ledgers, runtime memory, and
provider circuit breakers.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
CUSTOM_AGENT_SCRIPT = "custom-agent.py"
CUSTOM_AGENT_STATE_DIR = "custom-agents"
CUSTOM_AGENT_STATE_FILE = "custom-agents.json"
ENGINE_CHOICES = frozenset({"claude", "codex", "hybrid"})
MAX_LABEL_LEN = 80
MAX_PROMPT_LEN = 20_000
MAX_REPOS = 50
MIN_INTERVAL_SECONDS = 60

_CODENAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SCHEDULE_RE = re.compile(r"^(?P<num>[1-9][0-9]*)(?P<unit>[smhd])$")
_TIME_RE = re.compile(r"^(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})$")
_WEEKDAYS = {
    "sun": 0,
    "sunday": 0,
    "mon": 1,
    "monday": 1,
    "tue": 2,
    "tues": 2,
    "tuesday": 2,
    "wed": 3,
    "wednesday": 3,
    "thu": 4,
    "thur": 4,
    "thurs": 4,
    "thursday": 4,
    "fri": 5,
    "friday": 5,
    "sat": 6,
    "saturday": 6,
}

# Existing runtime identities. Operators can still rename them visibly through
# the roster-theme layer; custom runtime agents must be additive so logs,
# worktrees, scheduler labels, and state directories never collide.
RESERVED_CODENAMES = frozenset(
    {
        "agent-cleanup",
        "agent-morning-brief",
        "automerge",
        "bane",
        "batman",
        "code-map-refresh",
        "connector-sync",
        "curator",
        "damian",
        "drake",
        "fleet-github-poll",
        "fleet-doctor",
        "fleet-ingest",
        "fleet-recap",
        "fleet-recap-evening",
        "fleet-recap-morning",
        "gordon",
        "huntress",
        "lucius",
        "memory-auto-promote",
        "memory-harvest",
        "nightwing",
        "proof-telemetry",
        "rasalghul",
        "robin",
        "custom-agent",
        "shipped-summary",
        "shipped-summary-daily",
        "shipped-summary-weekly",
        "alfred-slack-thread-sync",
        "slack-thread-sync",
    }
)


class CustomAgentError(ValueError):
    """Raised when a custom-agent payload is invalid."""


@dataclass(frozen=True)
class CustomAgent:
    codename: str
    display_name: str
    role_title: str
    purpose: str
    prompt: str
    engine: str
    schedule: str
    repos: tuple[str, ...] = ()
    enabled: bool = True
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def label(self) -> str:
        return f"alfred.{self.codename}"

    @property
    def log_stem(self) -> str:
        return self.label

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["repos"] = list(self.repos)
        return data

    def to_conf_row(self) -> str:
        return "\t".join(
            (
                self.label,
                CUSTOM_AGENT_SCRIPT,
                self.schedule,
                "no",
                self.log_stem,
                self.role_title,
            )
        )

    def profile_payload(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "role_title": self.role_title,
            "purpose": self.purpose,
            "theme": "custom",
            "theme_label": "Custom",
            "theme_accent": "#00E5C7",
        }


class CustomAgentStore:
    """Atomic JSON store for operator-defined runtime agents."""

    def __init__(self, state_root: Path) -> None:
        self.state_root = Path(state_root)
        self.root = self.state_root / CUSTOM_AGENT_STATE_DIR
        self.path = self.root / CUSTOM_AGENT_STATE_FILE

    @classmethod
    def from_env(cls) -> CustomAgentStore:
        return cls(default_state_root())

    @classmethod
    def from_state_root(cls, state_root: Path) -> CustomAgentStore:
        return cls(state_root)

    def load(self, *, strict: bool = False) -> list[CustomAgent]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except OSError as exc:
            if strict:
                raise CustomAgentError("could not read custom agent manifest") from exc
            return []
        except json.JSONDecodeError as exc:
            if strict:
                raise CustomAgentError("custom agent manifest is not valid JSON") from exc
            return []
        if not isinstance(payload, Mapping):
            if strict:
                raise CustomAgentError("custom agent manifest must be a JSON object")
            return []
        raw_agents = payload.get("agents")
        if not isinstance(raw_agents, list):
            if strict:
                raise CustomAgentError("custom agent manifest agents must be a list")
            return []
        agents: list[CustomAgent] = []
        seen: set[str] = set()
        for raw in raw_agents:
            if not isinstance(raw, Mapping):
                if strict:
                    raise CustomAgentError("custom agent manifest entries must be objects")
                continue
            try:
                agent = _coerce_agent(raw, strict=strict)
            except CustomAgentError as exc:
                if strict:
                    raise CustomAgentError(
                        f"custom agent manifest contains an invalid agent: {exc}"
                    ) from exc
                continue
            if agent.codename in seen:
                if strict:
                    raise CustomAgentError(
                        f"custom agent manifest contains duplicate codename {agent.codename!r}"
                    )
                continue
            seen.add(agent.codename)
            agents.append(agent)
        return agents

    def get(self, codename: str) -> CustomAgent | None:
        target = normalize_codename(codename)
        if target is None:
            return None
        for agent in self.load():
            if agent.codename == target:
                return agent
        return None

    def upsert(self, payload: Mapping[str, Any]) -> CustomAgent:
        now = _now()
        next_agent = _coerce_agent(payload, strict=True, timestamp=now)
        if next_agent.codename in scheduler_config_codenames(self.state_root):
            raise CustomAgentError(
                f"{next_agent.codename!r} already exists in the scheduler config"
            )
        agents = self.load(strict=True)
        out: list[CustomAgent] = []
        replaced = False
        for agent in agents:
            if agent.codename == next_agent.codename:
                created = agent.created_at or now
                next_agent = CustomAgent(
                    codename=next_agent.codename,
                    display_name=next_agent.display_name,
                    role_title=next_agent.role_title,
                    purpose=next_agent.purpose,
                    prompt=next_agent.prompt,
                    engine=next_agent.engine,
                    schedule=next_agent.schedule,
                    repos=next_agent.repos,
                    enabled=next_agent.enabled,
                    created_at=created,
                    updated_at=now,
                )
                out.append(next_agent)
                replaced = True
            else:
                out.append(agent)
        if not replaced:
            out.append(next_agent)
        self._write(out)
        return next_agent

    def delete(self, codename: str) -> bool:
        target = normalize_codename(codename)
        if target is None:
            raise CustomAgentError("invalid codename")
        agents = self.load(strict=True)
        out = [agent for agent in agents if agent.codename != target]
        removed = len(out) != len(agents)
        if removed:
            self._write(out)
        return removed

    def snapshot(self, *, include_prompt: bool = True) -> dict[str, Any]:
        agents = self.load()
        updated_at = None
        with suppress(OSError):
            updated_at = datetime.fromtimestamp(self.path.stat().st_mtime, tz=UTC).isoformat()
        return {
            "version": MANIFEST_VERSION,
            "path": str(self.path),
            "agents": [_agent_payload(agent, include_prompt=include_prompt) for agent in agents],
            "count": len(agents),
            "enabled_count": sum(1 for agent in agents if agent.enabled),
            "disabled_count": sum(1 for agent in agents if not agent.enabled),
            "updated_at": updated_at,
        }

    def conf_rows(self, *, enabled_only: bool = True, strict: bool = False) -> list[str]:
        return [
            agent.to_conf_row()
            for agent in self.load(strict=strict)
            if agent.enabled or not enabled_only
        ]

    def _write(self, agents: list[CustomAgent]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "agents": [agent.to_dict() for agent in agents],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=f"{CUSTOM_AGENT_STATE_FILE}.",
            suffix=".tmp",
            dir=str(self.root),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass


def default_state_root() -> Path:
    home = os.environ.get("ALFRED_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "state"
    return Path.home() / ".alfred" / "state"


def normalize_codename(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("_", "-")
    return text if _CODENAME_RE.fullmatch(text) else None


def validate_new_codename(value: Any) -> str:
    codename = normalize_codename(value)
    if codename is None:
        raise CustomAgentError(
            "codename must be lowercase letters, numbers, or hyphens and start with a letter"
        )
    if codename in RESERVED_CODENAMES:
        raise CustomAgentError(f"{codename!r} is a built-in runtime codename")
    return codename


def scheduler_config_codenames(state_root: Path | None = None) -> set[str]:
    codenames: set[str] = set()
    for conf in _scheduler_config_candidates(state_root):
        codenames.update(_codenames_from_agents_conf(conf))
    return codenames


def _scheduler_config_candidates(state_root: Path | None) -> list[Path]:
    candidates: list[Path] = []
    if state_root is not None:
        home = Path(state_root).expanduser().parent
        candidates.append(home / "launchd" / "agents.conf")
        source_file = home / "launchd" / "source-repo.txt"
        source = ""
        with suppress(OSError):
            source = source_file.read_text(encoding="utf-8").strip()
        if source:
            candidates.append(Path(source).expanduser() / "launchd" / "agents.conf")

    env_home = os.environ.get("ALFRED_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser() / "launchd" / "agents.conf")

    candidates.append(Path(__file__).resolve().parents[1] / "launchd" / "agents.conf")

    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _codenames_from_agents_conf(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    codenames: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# ") or stripped == "#":
            continue
        if stripped.startswith("#"):
            line = line.replace("#", "", 1)
        parts = line.split("\t")
        if len(parts) < 2 or not parts[1].strip().endswith(".py"):
            continue
        suffix = parts[0].strip().lstrip("#").strip().rsplit(".", 1)[-1]
        codename = normalize_codename(suffix)
        if codename is not None:
            codenames.add(codename)
    return codenames


def canonical_schedule(raw: Any) -> str:
    value = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    if not value:
        raise CustomAgentError("schedule is required")
    if value.startswith("every "):
        value = value[len("every ") :].strip()

    short = _SCHEDULE_RE.match(value)
    if short:
        number = int(short.group("num"))
        unit = short.group("unit")
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return _interval(number * multiplier)

    if value.startswith("interval:"):
        return _interval(_int_part(value[len("interval:") :], "interval seconds"))

    if value.startswith(("daily@", "daily:")):
        time_part = value.split("@", 1)[1] if "@" in value else value.split(":", 1)[1]
        hour, minute = _parse_time(time_part)
        return f"cron:{hour}:{minute:02d}"

    if value.startswith(("weekly@", "weekly:")):
        rest = value.split("@", 1)[1] if "@" in value else value.split(":", 1)[1]
        weekday_part, sep, time_part = rest.partition(":")
        if not sep:
            raise CustomAgentError("weekly schedule needs a weekday and HH:MM")
        weekday = _parse_weekday(weekday_part)
        hour, minute = _parse_time(time_part)
        return f"cron:{weekday}:{hour}:{minute:02d}"

    if value.startswith("cron:"):
        return _canonical_cron(value)

    raise CustomAgentError(
        "schedule must be interval:<seconds>, 10m, 2h, daily@09:00, weekly@mon:09:00, or cron:<...>"
    )


def _coerce_agent(
    payload: Mapping[str, Any],
    *,
    strict: bool,
    timestamp: str | None = None,
) -> CustomAgent:
    if strict:
        codename = validate_new_codename(payload.get("codename"))
    else:
        normalized = normalize_codename(payload.get("codename"))
        if normalized is None:
            raise CustomAgentError(
                "codename must be lowercase letters, numbers, or hyphens and start with a letter"
            )
        if normalized in RESERVED_CODENAMES:
            raise CustomAgentError(f"{normalized!r} is a built-in runtime codename")
        codename = normalized
    display_name = _label(payload.get("display_name") or codename.replace("-", " ").title())
    role_title = _label(payload.get("role_title") or payload.get("role") or "Custom agent")
    purpose = _label(payload.get("purpose") or role_title, max_len=160)
    prompt = str(payload.get("prompt") or "").strip()
    if strict and len(prompt) < 20:
        raise CustomAgentError("prompt must describe the agent's job in at least 20 characters")
    if len(prompt) > MAX_PROMPT_LEN:
        raise CustomAgentError(f"prompt must be {MAX_PROMPT_LEN} characters or less")
    engine = str(payload.get("engine") or "hybrid").strip().lower()
    if engine not in ENGINE_CHOICES:
        raise CustomAgentError("engine must be claude, codex, or hybrid")
    schedule = canonical_schedule(payload.get("schedule") or "1h")
    repos = _repos(payload.get("repos"))
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        if strict:
            raise CustomAgentError("enabled must be a boolean")
        enabled = True
    created_at = _string_or_none(payload.get("created_at")) or timestamp or _now()
    updated_at = _string_or_none(payload.get("updated_at")) or timestamp or created_at
    return CustomAgent(
        codename=codename,
        display_name=display_name,
        role_title=role_title,
        purpose=purpose,
        prompt=prompt,
        engine=engine,
        schedule=schedule,
        repos=tuple(repos),
        enabled=bool(enabled),
        created_at=created_at,
        updated_at=updated_at,
    )


def _label(value: Any, *, max_len: int = MAX_LABEL_LEN) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        raise CustomAgentError("label fields cannot be empty")
    if len(text) > max_len:
        raise CustomAgentError(f"label fields must be {max_len} characters or less")
    return text


def _repos(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CustomAgentError("repos must be a list of owner/repo slugs")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        repo = str(item or "").strip()
        if not repo:
            continue
        if not _REPO_RE.fullmatch(repo):
            raise CustomAgentError(f"invalid repo slug: {repo!r}")
        if any(segment in {".", ".."} for segment in repo.split("/")):
            raise CustomAgentError(f"invalid repo slug: {repo!r}")
        if repo not in seen:
            seen.add(repo)
            out.append(repo)
    if len(out) > MAX_REPOS:
        raise CustomAgentError(f"repos must contain {MAX_REPOS} or fewer entries")
    return out


def _canonical_cron(value: str) -> str:
    parts = value[len("cron:") :].split(":")
    if len(parts) == 2:
        hour = _int_part(parts[0], "hour")
        minute = _int_part(parts[1], "minute")
        _check_time(hour, minute)
        return f"cron:{hour}:{minute:02d}"
    if len(parts) == 3:
        weekday = _parse_weekday(parts[0])
        hour = _int_part(parts[1], "hour")
        minute = _int_part(parts[2], "minute")
        _check_time(hour, minute)
        return f"cron:{weekday}:{hour}:{minute:02d}"
    raise CustomAgentError("cron schedule must be cron:HH:MM or cron:W:HH:MM")


def _parse_time(value: str) -> tuple[int, int]:
    match = _TIME_RE.match(value.strip())
    if not match:
        raise CustomAgentError("time must be HH:MM")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    _check_time(hour, minute)
    return hour, minute


def _parse_weekday(value: str) -> int:
    raw = str(value).strip().lower()
    if raw in _WEEKDAYS:
        return _WEEKDAYS[raw]
    weekday = _int_part(raw, "weekday")
    if not 0 <= weekday <= 6:
        raise CustomAgentError("weekday must be 0-6, where 0 is Sunday")
    return weekday


def _check_time(hour: int, minute: int) -> None:
    if not 0 <= hour <= 23:
        raise CustomAgentError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise CustomAgentError("minute must be 0-59")


def _int_part(value: Any, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise CustomAgentError(f"{label} must be an integer") from exc


def _interval(seconds: int) -> str:
    if seconds <= 0:
        raise CustomAgentError("interval seconds must be positive")
    if seconds < MIN_INTERVAL_SECONDS:
        raise CustomAgentError(
            f"interval must be at least {MIN_INTERVAL_SECONDS} seconds "
            f"(got {seconds}; use 1m or interval:60)"
        )
    return f"interval:{seconds}"


def _agent_payload(agent: CustomAgent, *, include_prompt: bool) -> dict[str, Any]:
    data = agent.to_dict()
    if not include_prompt:
        data.pop("prompt", None)
    return data


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
