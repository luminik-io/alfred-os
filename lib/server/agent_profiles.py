"""Public agent display profiles for Alfred surfaces."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AgentProfile:
    codename: str
    display_name: str
    role_title: str
    purpose: str
    theme: str
    theme_label: str
    theme_accent: str
    order: int


AGENT_PROFILES: tuple[AgentProfile, ...] = (
    AgentProfile(
        codename="batman",
        display_name="Batman",
        role_title="Architect",
        purpose="Plans and coordinates multi-repo work with approval.",
        theme="architect",
        theme_label="Architecture",
        theme_accent="#3B82F6",
        order=10,
    ),
    AgentProfile(
        codename="lucius",
        display_name="Lucius",
        role_title="Senior Developer",
        purpose="Ships scoped implementation issues as pull requests.",
        theme="builder",
        theme_label="Implementation",
        theme_accent="#7CE2B0",
        order=20,
    ),
    AgentProfile(
        codename="drake",
        display_name="Drake",
        role_title="Planner",
        purpose="Turns specs and loose requests into implementation-ready issues.",
        theme="planner",
        theme_label="Planning",
        theme_accent="#00E5C7",
        order=30,
    ),
    AgentProfile(
        codename="rasalghul",
        display_name="Ras al Ghul",
        role_title="Reviewer",
        purpose="Reviews PR diffs, tests, and posts P0/P1 findings.",
        theme="reviewer",
        theme_label="Review",
        theme_accent="#A78BFA",
        order=40,
    ),
    AgentProfile(
        codename="bane",
        display_name="Bane",
        role_title="Test Engineer",
        purpose="Adds or strengthens tests around shipped code paths.",
        theme="quality",
        theme_label="Tests",
        theme_accent="#F59E0B",
        order=50,
    ),
    AgentProfile(
        codename="nightwing",
        display_name="Nightwing",
        role_title="Fixer",
        purpose="Applies high-priority review and CI feedback.",
        theme="fixer",
        theme_label="Review fixes",
        theme_accent="#8FA6C9",
        order=60,
    ),
    AgentProfile(
        codename="robin",
        display_name="Robin",
        role_title="Bug Triage",
        purpose="Labels and scopes bug reports for the fleet.",
        theme="triage",
        theme_label="Triage",
        theme_accent="#F87171",
        order=70,
    ),
    AgentProfile(
        codename="damian",
        display_name="Damian",
        role_title="Spec Planner",
        purpose="Plans spec-level bundles before implementation starts.",
        theme="planner",
        theme_label="Spec planning",
        theme_accent="#14B8A6",
        order=80,
    ),
    AgentProfile(
        codename="huntress",
        display_name="Huntress",
        role_title="QA Runner",
        purpose="Runs end-to-end smoke checks and reports failures.",
        theme="qa",
        theme_label="QA",
        theme_accent="#EC4899",
        order=90,
    ),
    AgentProfile(
        codename="gordon",
        display_name="Gordon",
        role_title="Ops Watch",
        purpose="Checks uptime, incidents, and operational health.",
        theme="ops",
        theme_label="Operations",
        theme_accent="#38BDF8",
        order=100,
    ),
    AgentProfile(
        codename="automerge",
        display_name="Automerge",
        role_title="Merge Sweeper",
        purpose="Merges approved low-risk PRs when policy allows.",
        theme="release",
        theme_label="Release",
        theme_accent="#22C55E",
        order=110,
    ),
    AgentProfile(
        codename="agent-cleanup",
        display_name="Cleanup",
        role_title="Workspace Janitor",
        purpose="Sweeps stale worktrees and local branch leftovers.",
        theme="ops",
        theme_label="Cleanup",
        theme_accent="#94A3B8",
        order=120,
    ),
    AgentProfile(
        codename="cleanup",
        display_name="Cleanup",
        role_title="Workspace Janitor",
        purpose="Sweeps stale worktrees and local branch leftovers.",
        theme="ops",
        theme_label="Cleanup",
        theme_accent="#94A3B8",
        order=120,
    ),
    AgentProfile(
        codename="memory-harvest",
        display_name="Memory Harvest",
        role_title="Memory Curator",
        purpose="Queues repeated lessons for review before recall.",
        theme="memory",
        theme_label="Memory",
        theme_accent="#C084FC",
        order=130,
    ),
    AgentProfile(
        codename="fleet-doctor",
        display_name="Fleet Doctor",
        role_title="Health Check",
        purpose="Reports fleet health, pauses, locks, and runner gates.",
        theme="ops",
        theme_label="Health",
        theme_accent="#60A5FA",
        order=140,
    ),
    AgentProfile(
        codename="code-map-refresh",
        display_name="Code Map",
        role_title="Repo Indexer",
        purpose="Refreshes repo maps for planners and reviewers.",
        theme="indexing",
        theme_label="Indexing",
        theme_accent="#FBBF24",
        order=150,
    ),
    AgentProfile(
        codename="proof-telemetry",
        display_name="Proof Telemetry",
        role_title="Impact Reporter",
        purpose="Sends anonymous aggregate usage totals when configured.",
        theme="impact",
        theme_label="Impact",
        theme_accent="#00E5C7",
        order=160,
    ),
)

_PROFILE_BY_CODENAME = {profile.codename: profile for profile in AGENT_PROFILES}
_UNKNOWN_ORDER = 10_000


def agent_profile(codename: str) -> AgentProfile | None:
    """Return the public display profile for a codename, if Alfred knows it."""
    return _PROFILE_BY_CODENAME.get(_normalize_codename(codename))


def profile_payload(codename: str) -> dict[str, Any]:
    """Return serializable display metadata for a codename."""
    profile = agent_profile(codename)
    if profile is None:
        return {}
    payload = asdict(profile)
    payload.pop("codename", None)
    payload.pop("order", None)
    return payload


def profile_order(codename: str) -> int:
    """Stable fleet display order with Batman, Lucius, and Drake first."""
    profile = agent_profile(codename)
    return profile.order if profile is not None else _UNKNOWN_ORDER


def sort_codenames(codenames: list[str]) -> list[str]:
    """Sort codenames by public roster order, then alphabetically."""
    return sorted(codenames, key=lambda codename: (profile_order(codename), codename))


def _normalize_codename(codename: str) -> str:
    return codename.rsplit(".", 1)[-1].strip().lower()
