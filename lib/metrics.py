"""Fleet metrics aggregator.

Rolls up per-agent spend, transcript, and Codex artifact files under
``$ALFRED_HOME/state`` into a list of :class:`AgentMetric` records.

Paths read (relative to a caller-provided ``state_dir``):

    <codename>/spend-YYYY-MM-DD.json     - per-day SpendState files
    transcripts/<codename>/<YYYY-MM>/*.jsonl  - stream-JSON firings
    codex/<codename>/<YYYY-MM>/*.stdout.txt   - Codex run stdout dumps

The aggregator is deliberately tolerant: missing files, missing keys,
and unparseable timestamps are skipped quietly. The operator runs this
to find out *what* burned cost, not to validate the disk layout.

Pure stdlib. Depends on :mod:`transcripts` for the transcript reader.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from transcripts import (
    extract_codex_tokens,
    transcript_summary,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class SpendTotals:
    """Roll-up of per-day SpendState files for one agent."""

    firings: int = 0
    successes: int = 0
    failures: int = 0
    turns: int = 0
    cost_usd: float = 0.0


@dataclass
class AgentMetric:
    """Aggregate stats for one agent over the requested window."""

    codename: str
    spend: SpendTotals = field(default_factory=SpendTotals)
    transcripts_seen: int = 0
    codex_runs: int = 0
    codex_tokens: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_calls_total: int = 0
    skills: dict[str, int] = field(default_factory=dict)
    files_edited: int = 0
    files_read: int = 0
    bash_commands: int = 0

    def is_empty(self) -> bool:
        return self.spend.firings == 0 and self.tool_calls_total == 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FleetReport:
    """Snapshot returned by :func:`fleet_metrics`."""

    days: int
    generated_at: datetime
    metrics: list[AgentMetric]

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "generated_at": self.generated_at.isoformat(),
            "metrics": [m.to_dict() for m in self.metrics],
        }


# --------------------------------------------------------------------------
# --since parsing
# --------------------------------------------------------------------------


_SINCE_RE = re.compile(r"^\s*(\d+)\s*([dhwm])?\s*$", re.IGNORECASE)


def parse_since(value: str | int | None, *, default_days: int = 7) -> int:
    """Parse a ``--since`` value into a number of days.

    Accepted forms: ``7``, ``7d``, ``2w``, ``48h``. ``h`` rounds up to
    at least 1 day; ``m`` is treated as months (30 days). Unknown
    values fall back to ``default_days`` with a warning.
    """
    if value is None:
        return default_days
    if isinstance(value, int):
        return max(1, value)
    m = _SINCE_RE.match(str(value))
    if not m:
        logger.warning("could not parse --since=%r; falling back to %d days", value, default_days)
        return default_days
    n = int(m.group(1))
    unit = (m.group(2) or "d").lower()
    if unit == "d":
        return max(1, n)
    if unit == "h":
        return max(1, (n + 23) // 24)
    if unit == "w":
        return max(1, n * 7)
    if unit == "m":
        return max(1, n * 30)
    return default_days


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------


def discover_codenames(state_dir: Path) -> list[str]:
    """Find every codename with a spend file, transcript dir, or Codex dir.

    Sorted, deduped, lower-case-comparable.
    """
    found: set[str] = set()
    if state_dir.is_dir():
        for entry in state_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            # Spend files live directly under state/<codename>/.
            if any(entry.glob("spend-*.json")):
                found.add(entry.name)
        transcripts = state_dir / "transcripts"
        if transcripts.is_dir():
            for entry in transcripts.iterdir():
                if entry.is_dir():
                    found.add(entry.name)
        codex = state_dir / "codex"
        if codex.is_dir():
            for entry in codex.iterdir():
                if entry.is_dir():
                    found.add(entry.name)
    # Reserved internal dirs that aren't codenames.
    for reserved in ("transcripts", "codex", "fleet", "engines", "events"):
        found.discard(reserved)
    return sorted(found)


def _spend_files(state_dir: Path, codename: str, days: int) -> list[Path]:
    agent_root = state_dir / codename
    if not agent_root.is_dir():
        return []
    cutoff = datetime.now().date() - timedelta(days=max(0, days - 1))
    out: list[Path] = []
    for path in agent_root.glob("spend-*.json"):
        try:
            day = datetime.strptime(path.stem.replace("spend-", ""), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            out.append(path)
    return out


def _files_in_window(root: Path, days: int, suffix: str) -> list[Path]:
    if not root.is_dir():
        return []
    cutoff_ts = (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    out: list[Path] = []
    for month_dir in root.iterdir():
        if not month_dir.is_dir():
            continue
        for path in month_dir.glob(f"*{suffix}"):
            try:
                if path.stat().st_mtime >= cutoff_ts:
                    out.append(path)
            except OSError:
                continue
    return out


def _transcript_files(state_dir: Path, codename: str, days: int) -> list[Path]:
    return _files_in_window(state_dir / "transcripts" / codename, days, ".jsonl")


def _codex_stdout_files(state_dir: Path, codename: str, days: int) -> list[Path]:
    return _files_in_window(state_dir / "codex" / codename, days, ".stdout.txt")


# --------------------------------------------------------------------------
# Per-agent rollup
# --------------------------------------------------------------------------


def agent_metric(state_dir: Path, codename: str, days: int) -> AgentMetric:
    """Compute one agent's metrics over the last ``days`` days."""
    metric = AgentMetric(codename=codename)

    for path in _spend_files(state_dir, codename, days):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("skipping spend file %s: %s", path, exc)
            continue
        metric.spend.firings += _coerce_int(data.get("firings_today"))
        metric.spend.successes += _coerce_int(data.get("successes_today"))
        metric.spend.failures += _coerce_int(data.get("failures_today"))
        metric.spend.turns += _coerce_int(data.get("turns_today"))
        metric.spend.cost_usd += _coerce_float(data.get("cost_usd_today"))

    transcripts = _transcript_files(state_dir, codename, days)
    for path in transcripts:
        s = transcript_summary(path)
        for name, count in s.tool_calls_by_name.items():
            metric.tool_calls[name] = metric.tool_calls.get(name, 0) + count
        for skill in s.skills_invoked:
            metric.skills[skill] = metric.skills.get(skill, 0) + 1
        metric.files_edited += len(s.files_edited)
        metric.files_read += len(s.files_read)
        metric.bash_commands += len(s.bash_commands)
    metric.transcripts_seen = len(transcripts)
    metric.tool_calls_total = sum(metric.tool_calls.values())

    codex_files = _codex_stdout_files(state_dir, codename, days)
    metric.codex_runs = len(codex_files)
    for path in codex_files:
        try:
            metric.codex_tokens += extract_codex_tokens(path.read_text())
        except OSError:
            continue

    return metric


def fleet_metrics(
    state_dir: Path,
    days: int = 7,
    codenames: list[str] | None = None,
) -> FleetReport:
    """Compute metrics for every codename (or a subset) under ``state_dir``."""
    targets = codenames if codenames is not None else discover_codenames(state_dir)
    return FleetReport(
        days=days,
        generated_at=datetime.now(tz=UTC),
        metrics=[agent_metric(state_dir, c, days) for c in targets],
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _coerce_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
