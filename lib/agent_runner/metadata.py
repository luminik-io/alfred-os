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
    """Format ``"<codename> (<role>)"`` when a role is set, else the bare codename.

    Slack post prefixes and CLI status output use this so a reader who
    hasn't memorised the agent cast still gets operational context next
    to every codename.
    """
    role = agent_role(codename)
    return f"{codename} ({role})" if role else codename


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
    ``alfred doctor`` can inspect.
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
