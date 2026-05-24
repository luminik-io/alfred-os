"""Path and binary-name resolution for the alfred-os runtime.

This module owns the operator-facing filesystem and binary contract:

* ``ALFRED_HOME`` and ``WORKSPACE_ROOT`` resolution (env override + sane
  defaults under the user home directory).
* Derived state and workspace paths (``STATE_ROOT``, ``WORKTREE_ROOT``,
  ``TRANSCRIPTS_ROOT``, ``CODEX_TRANSCRIPTS_ROOT``, ``LIB_DIR``,
  ``BIN_DIR``, ``PROMPTS_ROOT``, ``SHARED_AGENT``).
* Resolution for the load-bearing external CLIs (``CLAUDE_BIN``,
  ``CODEX_BIN``) and the codex defaults sourced from env.
* Two stdlib datetime helpers (``now_iso``, ``today_str``) used widely
  enough that they belong with the path constants rather than in a
  hidden corner of ``process.py``.

What this module does NOT own:

* Subprocess invocation -> ``process.py``.
* The ``.alfredrc`` loader -> ``config.py``.
* GitHub state-machine label constants -> ``github.py``.

The public surface is intentionally values (not classes): import the
constant you need, do not subclass anything here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Operator home + workspace
# --------------------------------------------------------------------------
HOME: Path = Path(os.path.expanduser("~"))

ALFRED_HOME: Path = Path(os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred"))
"""Public runtime root. State, worktrees, transcripts, lib, bin live here."""

WORKSPACE_ROOT: Path = Path(os.environ.get("WORKSPACE_ROOT") or os.path.expanduser("~/code"))
"""Root of per-repo product checkouts. Every <repo> resolves under here."""

# Back-compat alias: many bin/*.py scripts use WORKSPACE as the parent of the
# per-repo checkouts under product/. Keep that shape; new consumers can ignore
# it and reference WORKSPACE_ROOT directly.
WORKSPACE: Path = WORKSPACE_ROOT / "product"

# --------------------------------------------------------------------------
# GitHub org/user slug for repo-targeting helpers.
#
# Required only when those helpers are used; agents that don't touch gh
# can leave it unset. Set once in the launchd plist EnvironmentVariables
# block as the canonical configuration site.
# --------------------------------------------------------------------------
GH_ORG: str = os.environ.get("GH_ORG", "").strip()

# --------------------------------------------------------------------------
# State + worktree + transcript subdirectories.
# All derived from ALFRED_HOME so a clean clone "just works".
# --------------------------------------------------------------------------
STATE_ROOT: Path = ALFRED_HOME / "state"
WORKTREE_ROOT: Path = ALFRED_HOME / "worktrees"
WORKTREES_ROOT: Path = WORKTREE_ROOT  # plural alias matching docs / launchd discussion
LIB_DIR: Path = ALFRED_HOME / "lib"
BIN_DIR: Path = ALFRED_HOME / "bin"
TRANSCRIPTS_ROOT: Path = STATE_ROOT / "transcripts"
PROMPTS_ROOT: Path = ALFRED_HOME / "prompts"
CODEX_TRANSCRIPTS_ROOT: Path = STATE_ROOT / "codex"
SHARED_AGENT: Path = ALFRED_HOME / "shared" / ".agent"

# Fleet + lifecycle state files
GLOBAL_BLOCKED_FILE: Path = STATE_ROOT / "global-blocked-until.json"
SLACK_WEBHOOK_CACHE: Path = STATE_ROOT / "slack-webhook.cache"
SLACK_WEBHOOK_CACHE_TTL: int = 30 * 24 * 3600  # 30 days; the webhook URL is stable

FLEET_DIR: Path = STATE_ROOT / "fleet"
FLEET_ENABLED_FILE: Path = FLEET_DIR / "enabled.txt"
PAUSED_REPOS_FILE: Path = STATE_ROOT / "paused-repos.json"

# --------------------------------------------------------------------------
# External CLI binaries
#
# Defaults assume the binary is on PATH; override with the env var on
# hosts where it isn't (notably launchd plists that don't inherit a
# login shell PATH).
# --------------------------------------------------------------------------
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CODEX_DEFAULT_MODEL: str | None = os.environ.get("CODEX_MODEL", "").strip() or None
CODEX_DEFAULT_SANDBOX: str = os.environ.get("CODEX_SANDBOX", "read-only").strip() or "read-only"
CODEX_APPROVAL_POLICY: str = os.environ.get("CODEX_APPROVAL_POLICY", "never").strip() or "never"

# --------------------------------------------------------------------------
# Small datetime helpers (used everywhere, belong with the constants)
# --------------------------------------------------------------------------


def now_iso() -> str:
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    """Return the UTC date as ``YYYY-MM-DD`` for per-day ledger filenames.

    UTC (not local) so the daily spend ledger rotates at the same moment
    as every other timestamp the runner records via ``now_iso``. Using
    local time meant a firing crossing local midnight read the freshly
    rotated empty ledger and could burn an extra cap's worth of turns
    before the cap-check loop caught up: the hard spend cap quietly
    leaked one cap per day for every operator outside UTC.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d")
