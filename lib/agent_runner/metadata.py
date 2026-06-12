"""Firing metadata, role descriptors, prompt loader, hand-off table.

This module owns the small data-carrying surface that names firings and
agents:

* :func:`agent_role` / :func:`codename_with_role` for the
  ``codename (role)`` display string sourced from launchd plist env vars.
* :func:`commit_trailer` for the ``Agent-Codename: ... Agent-Firing-Id: ...``
  trailer block every agent appends to commits.
* :class:`HandoffTable` and the module singleton :data:`HANDOFFS` for
  declarative codename routing.
* :func:`load_prompt` for ``${VAR}``-templated prompt files.

What this module does NOT own:

* The event log itself -> ``state.py`` (lives next to the spend ledger).
* Actually calling the LLM with the loaded prompt -> ``process.py``.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from .paths import ALFRED_HOME, WORKSPACE_ROOT


@dataclass(frozen=True)
class AgentTheme:
    """Operator-facing visual naming theme for a fleet profile."""

    theme_id: str
    label: str
    accent: str


@dataclass(frozen=True)
class AgentProfile:
    """Shared display contract for one agent.

    ``codename`` remains the stable runtime identifier. The display name,
    role title, and purpose make that codename legible in Slack, CLI, and
    native UI surfaces.
    """

    codename: str
    display_name: str
    role_title: str
    purpose: str
    theme: AgentTheme

    @property
    def label(self) -> str:
        if self.role_title:
            return f"{self.display_name} · {self.role_title}"
        return self.display_name

    def to_dict(self) -> dict[str, str]:
        return {
            "codename": self.codename,
            "display_name": self.display_name,
            "role_title": self.role_title,
            "purpose": self.purpose,
            "theme": self.theme.theme_id,
            "theme_label": self.theme.label,
            "theme_accent": self.theme.accent,
        }


THEMES: dict[str, AgentTheme] = {
    "wayne": AgentTheme("wayne", "Gotham operations", "#3ad7c1"),
    "orbit": AgentTheme("orbit", "Orbital crew", "#74a7ff"),
    "atelier": AgentTheme("atelier", "Studio makers", "#ff8f70"),
    "mythic": AgentTheme("mythic", "Mythic council", "#8be28b"),
}


DEFAULT_PROFILES: dict[str, tuple[str, str, str, str]] = {
    "alfred": (
        "Alfred",
        "Concierge",
        "Routes requests, explains state, and keeps the operator oriented.",
        "wayne",
    ),
    "batman": (
        "Batman",
        "Architect",
        "Plans and coordinates multi-repo work with operator approval.",
        "wayne",
    ),
    "lucius": (
        "Lucius",
        "Senior Developer",
        "Ships scoped implementation issues as pull requests.",
        "orbit",
    ),
    "drake": (
        "Drake",
        "Spec Planner",
        "Turns specs and vague requests into implementation-ready issues.",
        "atelier",
    ),
    "damian": (
        "Damian",
        "Bundle Planner",
        "Finds spec-level multi-repo bundles and files coordinated work.",
        "wayne",
    ),
    "bane": (
        "Bane",
        "Test Engineer",
        "Adds targeted regression coverage for recently changed code.",
        "mythic",
    ),
    "rasalghul": (
        "Ra's al Ghul",
        "Principal Reviewer",
        "Reviews pull requests for correctness, safety, and architecture.",
        "wayne",
    ),
    "nightwing": (
        "Nightwing",
        "CI Engineer",
        "Fixes failing checks and unresolved review feedback.",
        "wayne",
    ),
    "robin": (
        "Robin",
        "Bug Triage",
        "Reproduces, labels, and scopes bugs before implementation.",
        "wayne",
    ),
    "huntress": (
        "Huntress",
        "QA Smoke Runner",
        "Runs scheduled smoke checks and reports deploy regressions.",
        "wayne",
    ),
    "gordon": (
        "Gordon",
        "Ops Briefing",
        "Summarizes deployment and fleet health signals.",
        "wayne",
    ),
    "automerge": (
        "Automerge",
        "Release Steward",
        "Merges blessed agent-authored pull requests after gates clear.",
        "orbit",
    ),
    "agent-cleanup": (
        "Agent Cleanup",
        "Workspace Janitor",
        "Prunes stale claims, branches, and worktrees.",
        "atelier",
    ),
    "cleanup": (
        "Cleanup",
        "Workspace Janitor",
        "Prunes stale claims, branches, and worktrees.",
        "atelier",
    ),
    "code-map-refresh": (
        "Code Map",
        "Context Indexer",
        "Refreshes per-repo code maps for planning and implementation.",
        "orbit",
    ),
    "fleet-doctor": (
        "Fleet Doctor",
        "Health Auditor",
        "Checks local fleet health and configuration drift.",
        "mythic",
    ),
    "memory-harvest": (
        "Memory Harvest",
        "Lessons Curator",
        "Queues reviewable memory candidates from repeated patterns.",
        "mythic",
    ),
    "morning-brief": (
        "Morning Brief",
        "Daily Briefing",
        "Prepares the operator's daily status summary.",
        "atelier",
    ),
}


def _env_key(codename: str, suffix: str) -> str:
    return "ALFRED_" + codename.upper().replace("-", "_") + "_" + suffix


def _title_from_codename(codename: str) -> str:
    return " ".join(part.capitalize() for part in codename.replace("_", "-").split("-"))


def agent_theme(theme_id: str | None) -> AgentTheme:
    key = (theme_id or "").strip().lower()
    return THEMES.get(key) or THEMES["wayne"]


def agent_profile(codename: str) -> AgentProfile:
    """Return a shared display profile for ``codename``.

    Defaults are public-safe and generic. Operators can override display
    fields through environment variables rendered by their local setup:
    ``ALFRED_<CODENAME>_DISPLAY_NAME``, ``ROLE_TITLE``, ``PURPOSE``,
    and ``THEME``.
    """
    normalized = (codename or "").strip()
    is_known_profile = normalized in DEFAULT_PROFILES
    defaults = DEFAULT_PROFILES.get(
        normalized,
        (_title_from_codename(normalized), agent_role(normalized), "", "wayne"),
    )
    display_name, role_title, purpose, theme_id = defaults
    display_name = os.environ.get(
        _env_key(normalized, "DISPLAY_NAME"),
        display_name,
    ).strip()
    explicit_role_title = os.environ.get(_env_key(normalized, "ROLE_TITLE"))
    role_title = (explicit_role_title or role_title).strip()
    legacy_role = agent_role(normalized)
    if legacy_role and not explicit_role_title and not is_known_profile:
        role_title = legacy_role
    purpose = os.environ.get(_env_key(normalized, "PURPOSE"), purpose).strip()
    theme_id = os.environ.get(_env_key(normalized, "THEME"), theme_id).strip()
    return AgentProfile(
        codename=normalized,
        display_name=display_name or _title_from_codename(normalized),
        role_title=role_title,
        purpose=purpose,
        theme=agent_theme(theme_id),
    )


def agent_label(codename: str) -> str:
    """Return the user-facing ``Display · Role`` label for an agent."""
    return agent_profile(codename).label


def agent_role(codename: str) -> str:
    """Return the one-line operational role descriptor for an agent.

    Read from ``ALFRED_<CODENAME>_ROLE`` (rendered into each launchd
    plist by ``launchd/render.sh`` from agents.conf column 7). Returns
    the empty string when no role is set; never raises. Hyphens in
    compound codenames are translated to underscores to match what
    ``render.sh`` emits.
    """
    if not codename:
        return ""
    env_key = "ALFRED_" + codename.upper().replace("-", "_") + "_ROLE"
    return (os.environ.get(env_key) or "").strip()


def codename_with_role(codename: str) -> str:
    """Format the user-facing agent label.

    Kept under the historical function name so existing Slack and CLI callers
    gain clearer role labels without changing imports.
    """
    return agent_label(codename)


def commit_trailer(agent: str, firing_id: str, *, extra: dict[str, str] | None = None) -> str:
    """Build a multi-line commit-trailer block.

    Caller appends this to their commit message. Format follows the
    ``Trailer: Value`` convention git itself uses (so ``git
    interpret-trailers`` parses it correctly):

        Agent-Codename: lucius
        Agent-Firing-Id: 2026-04-29-1647-bf3a

    ``extra`` keys are PascalCased before they're written, so
    ``{"issue_number": "275"}`` becomes ``Issue-Number: 275``.
    """
    lines = [
        f"Agent-Codename: {agent}",
        f"Agent-Firing-Id: {firing_id}",
    ]
    if extra:
        for k, v in extra.items():
            key = "-".join(part.capitalize() for part in k.replace("_", "-").split("-"))
            lines.append(f"{key}: {v}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Hand-off table
# --------------------------------------------------------------------------


@dataclass
class HandoffTable:
    """Static codename routing map.

    Intentionally minimal: just a triple-keyed dict and validation
    helpers. The actual routing happens via GitHub labels and state,
    not in-process calls; this struct is documentation plus a shape
    ``doctor.sh`` can inspect.
    """

    edges: dict[tuple[str, str], str] = field(default_factory=dict)

    def add(self, from_agent: str, outcome: str, to_agent: str) -> None:
        """Register one ``(from, outcome) -> to`` edge."""
        self.edges[(from_agent, outcome)] = to_agent

    def consumers(self, codename: str) -> list[str]:
        """Return outcomes ``codename`` emits that route to another codename."""
        return [outcome for (src, outcome), _ in self.edges.items() if src == codename]

    def producers(self, codename: str) -> list[tuple[str, str]]:
        """Return ``(from-agent, outcome)`` pairs that route to ``codename``."""
        return [(src, outcome) for (src, outcome), dst in self.edges.items() if dst == codename]

    def validate(self, known_codenames: set[str]) -> list[str]:
        """Return list of issues: orphan emitters / consumers / unknown agents."""
        misses: list[str] = []
        for (src, outcome), dst in self.edges.items():
            if src not in known_codenames:
                misses.append(f"hand-off from unknown agent '{src}' (outcome={outcome})")
            if dst not in known_codenames:
                misses.append(f"hand-off to unknown agent '{dst}' (outcome={outcome})")
        return misses


HANDOFFS = HandoffTable()
"""Module-level singleton; consumers extend at import time."""


# --------------------------------------------------------------------------
# Prompt loader
# --------------------------------------------------------------------------


def load_prompt(path: Path | str, *, extra_vars: dict[str, str] | None = None) -> str:
    """Read a prompt file and substitute ``${VAR}`` placeholders from env.

    Unset variables are left as literal ``${VAR}``. That's deliberate:
    a missing variable shouldn't crash the agent or silently substitute
    an empty string into a shell command. Use ``preflight()`` with the
    env var in ``env_vars`` to fail loud on missing config.

    ``extra_vars`` overrides ``os.environ`` for specific keys; useful for
    per-firing context like ``${ISSUE_NUMBER}`` or ``${REPO_SLUG}``.
    """
    p = Path(path)
    text = p.read_text()
    mapping = dict(os.environ)
    mapping.setdefault("ALFRED_HOME", str(ALFRED_HOME))
    mapping.setdefault("WORKSPACE_ROOT", str(WORKSPACE_ROOT))
    if extra_vars:
        mapping.update(extra_vars)
    return string.Template(text).safe_substitute(mapping)
