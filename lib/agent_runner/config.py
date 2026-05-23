"""Environment-variable and engine-selection configuration.

This module owns the 12-factor env-var contract:

* ``env_int`` / ``optional_env_int`` for clamped integer knobs.
* ``_truthy_env``, ``_env_value_enabled``, ``_env_present`` for the
  three flavours of boolean env-var test.
* Engine-selection helpers (``normalize_engine``, ``agent_engine``,
  ``engine_preflight_bins``) and the engine-mode constants
  (``ENGINE_CHOICES``, ``PROVIDER_LIMIT_SUBTYPES``,
  ``HYBRID_FALLBACK_SUBTYPES``).
* Codex sandbox resolution per agent (``codex_sandbox_for_agent``).
* Doctor + dry-run mode flags (``doctor_mode``, ``is_dry_run``,
  ``set_dry_run``, ``dry_run_log``).

What this module does NOT own:

* The Slack webhook URL resolution (env + cache + AWS Secrets) -> ``notify.py``.
* The ``.alfredrc`` loader: alfred-os reads config exclusively from env vars
  for 12-factor compliance; there is no ``.alfredrc`` parse path here.
* Constructing ``ClaudeResult`` objects -> ``result.py``.

All values are computed at call time (no module-level caches), so tests can
``monkeypatch.setenv`` then call any function and see the new value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .paths import CLAUDE_BIN, CODEX_BIN, STATE_ROOT

# --------------------------------------------------------------------------
# Engine vocabulary
# --------------------------------------------------------------------------
ENGINE_CHOICES: frozenset[str] = frozenset({"claude", "codex", "hybrid"})

PROVIDER_LIMIT_SUBTYPES: frozenset[str] = frozenset({"error_budget", "error_rate_limit"})
"""Subtypes that mean we hit a provider's quota / rate-limit wall."""

HYBRID_FALLBACK_SUBTYPES: frozenset[str] = PROVIDER_LIMIT_SUBTYPES | frozenset(
    {"error_authentication"}
)
"""Subtypes that should trigger a Claude->Codex fallback in hybrid mode."""


# --------------------------------------------------------------------------
# Env-var primitives
# --------------------------------------------------------------------------


def _truthy_env(name: str) -> bool:
    """Standard ``1 / true / yes / on`` env-truthiness check."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_value_enabled(name: str) -> bool:
    """True when env var is set to a non-falsy value (broader than _truthy_env)."""
    value = os.environ.get(name)
    return bool(value and value.strip().lower() not in {"", "0", "false", "no", "off"})


def _env_present(name: str) -> bool:
    """True when env var is set to any non-empty string."""
    return bool(os.environ.get(name))


def _agent_env_slug(agent: str) -> str:
    """Translate a codename to the env-var convention (UPPER, hyphens -> underscores)."""
    return agent.strip().upper().replace("-", "_")


def env_int(
    name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    """Read a small integer knob from env, clamped to ``[minimum, maximum]``.

    Missing or non-integer values fall back to ``default``. The result is
    always clamped, including the fallback path, so a typo in the launchd
    plist can never kneecap or unbound a per-firing budget.

    Args:
        name: env var name.
        default: value used when the env var is unset or unparseable.
        minimum: floor (inclusive).
        maximum: optional ceiling (inclusive); ``None`` means uncapped.

    Returns:
        The clamped integer.
    """
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = default
    else:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def optional_env_int(
    name: str, *, minimum: int = 1, maximum: int | None = None
) -> int | None:
    """Read an optional integer knob; return ``None`` when unset or unparseable.

    Designed for "no default ceiling but allow temporary debugging via env"
    knobs, most prominently the per-firing ``max_turns`` budget on agents
    where a hard cap can produce no-output runs. The wall-clock ``timeout``
    on the invoke call remains the real bound.

    Args:
        name: env var name.
        minimum: floor when a value parses.
        maximum: optional ceiling when a value parses.

    Returns:
        The clamped integer, or ``None`` when no value is configured.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# --------------------------------------------------------------------------
# Engine selection
# --------------------------------------------------------------------------


def normalize_engine(raw: str | None, *, default: str = "hybrid") -> str:
    """Coerce an engine-mode string to one of ``ENGINE_CHOICES``.

    The legacy alias ``both`` maps to ``hybrid``. Anything outside the
    allow-list falls back to ``default`` (and that is itself normalized).
    """
    value = (raw or "").strip().lower()
    if value == "both":
        return "hybrid"
    if value in ENGINE_CHOICES:
        return value
    fallback = (default or "hybrid").strip().lower()
    if fallback == "both":
        return "hybrid"
    return fallback if fallback in ENGINE_CHOICES else "hybrid"


def agent_engine(
    agent: str,
    *,
    default: str = "hybrid",
    legacy_env: str | None = None,
    legacy_state_file: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the configured engine for one agent.

    Precedence:

    1. ``ALFRED_<AGENT>_ENGINE``
    2. an optional legacy env var, e.g. ``ALFRED_REVIEW_ENGINE``
    3. ``ALFRED_ENGINE`` for fleet-wide testing
    4. ``${ALFRED_HOME}/state/engines/<agent>``
    5. an optional legacy state file
    6. ``default``

    Args:
        agent: codename.
        default: fallback when nothing is configured.
        legacy_env: deprecated env-var name to consult after the canonical one.
        legacy_state_file: deprecated path to consult after the canonical one.
        environ: env mapping override (defaults to ``os.environ``).

    Returns:
        A value in ``ENGINE_CHOICES``.
    """
    env = environ if environ is not None else os.environ
    safe_agent = agent.strip().lower().replace("_", "-")
    env_name = f"ALFRED_{_agent_env_slug(safe_agent)}_ENGINE"
    for name in (env_name, legacy_env, "ALFRED_ENGINE"):
        if name and env.get(name, "").strip():
            return normalize_engine(env.get(name), default=default)

    state_file = STATE_ROOT / "engines" / safe_agent
    for path in (state_file, legacy_state_file):
        if not path:
            continue
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw:
            return normalize_engine(raw, default=default)
    return normalize_engine(None, default=default)


def engine_preflight_bins(engine: str, *, hybrid_requires_codex: bool = False) -> list[str]:
    """Return load-bearing binaries for an engine mode.

    Hybrid is Claude-first by default, so a missing optional Codex
    fallback does not stop ordinary scheduled work. Callers that require
    Codex even in hybrid mode pass ``hybrid_requires_codex=True``.
    """
    mode = normalize_engine(engine)
    if mode == "codex":
        return [CODEX_BIN]
    if mode == "hybrid" and hybrid_requires_codex:
        return [CLAUDE_BIN, CODEX_BIN]
    return [CLAUDE_BIN]


def codex_sandbox_for_agent(
    agent: str,
    *,
    default: str = "read-only",
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve the Codex sandbox mode for an agent.

    Precedence:

    1. ``ALFRED_<AGENT>_CODEX_SANDBOX``
    2. ``<AGENT>_CODEX_SANDBOX`` (legacy alias)
    3. ``ALFRED_<AGENT>_CODEX_WRITE=1`` -> ``workspace-write``
    4. ``default``
    """
    env = environ if environ is not None else os.environ
    slug = _agent_env_slug(agent)
    explicit = (
        env.get(f"ALFRED_{slug}_CODEX_SANDBOX")
        or env.get(f"{slug}_CODEX_SANDBOX")
        or ""
    ).strip()
    if explicit:
        return explicit
    if (env.get(f"ALFRED_{slug}_CODEX_WRITE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return "workspace-write"
    return default


# --------------------------------------------------------------------------
# Doctor + dry-run mode
# --------------------------------------------------------------------------


def doctor_mode() -> bool:
    """True when running under ``doctor.sh`` (``ALFRED_DOCTOR=1``).

    Agents check this after preflight passes and exit ``0`` with a
    ``[<AGENT>-DOCTOR-OK]`` sentinel instead of doing real work. Lets
    the operator verify a fresh setup without burning Claude turns.
    """
    return _env_value_enabled("ALFRED_DOCTOR")


# Dry-run step counter: process-local. Reset on import; bin scripts run in
# their own process so there is no shared-state risk.
_DRY_RUN_STEP = 0


def is_dry_run() -> bool:
    """True when the firing is a dry run (``ALFRED_DRY_RUN`` truthy).

    Checked at every side-effecting boundary as a single seam, not as
    scattered conditionals. Runners that accept a ``--dry-run`` CLI flag
    call ``set_dry_run()`` to flip this on.
    """
    return _env_value_enabled("ALFRED_DRY_RUN")


def set_dry_run(enabled: bool = True) -> None:
    """Enable (or disable) dry-run mode for the rest of this process.

    Writes ``ALFRED_DRY_RUN`` into ``os.environ`` so ``is_dry_run()`` and
    any subprocess-spawned children agree. Runners call this once after
    parsing a ``--dry-run`` CLI flag, before the lifecycle starts.
    """
    if enabled:
        os.environ["ALFRED_DRY_RUN"] = "1"
    else:
        os.environ.pop("ALFRED_DRY_RUN", None)


def dry_run_log(step: str, message: str) -> None:
    """Print one narrated ``[dry-run]`` trace line to stdout.

    ``step`` is a short lifecycle tag (``slack``, ``gh``, ``git``,
    ``llm``, ``spend``, ...). The output is deliberately legible and
    well-sequenced; a dry-run firing is meant to be recorded with
    asciinema.
    """
    global _DRY_RUN_STEP
    _DRY_RUN_STEP += 1
    print(f"[dry-run] {_DRY_RUN_STEP:>2}. ({step}) {message}", file=sys.stdout, flush=True)
