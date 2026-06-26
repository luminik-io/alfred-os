"""Central source of truth for Alfred's GitHub-label state machine.

Every label string the fleet relies on lives here, plus the transition
table that documents which moves are legal. Other modules import names
from this file rather than duplicating string literals; if a label needs
to change, change it here and the rest of the fleet follows.

The lifecycle is described in detail in ``docs/STATE_MACHINE.md`` and
``site/src/content/docs/concepts/state-machine.md``. This module is the
machine-readable form of that doc.

Design notes:

- No I/O. This module is pure data and pure functions so it can be
  imported from any context (CLI, hook, library, test) without dragging
  in subprocess, gh CLI, filesystem, or network.
- ``agent_runner.py`` predates this module; the existing constants on
  that module remain valid and continue to be the canonical names used
  by claim_issue / release_issue. ``labels.is_lifecycle_label`` etc. use
  the strings defined here; tests assert the two stay in sync.
- Bundle labels are dynamic (``agent:bundle:<slug>``) so we expose a
  predicate and a slug helper rather than a static set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

# --------------------------------------------------------------------------
# Lifecycle labels (mutually exclusive - at most one set on an issue).
# --------------------------------------------------------------------------

IMPLEMENT: Final[str] = "agent:implement"
"""Eligible for autonomous pickup by a planner agent."""

IN_FLIGHT: Final[str] = "agent:in-flight"
"""An agent is actively working this issue."""

PR_OPEN: Final[str] = "agent:pr-open"
"""A PR exists for this issue. Set on successful release."""

DONE: Final[str] = "agent:done"
"""Issue shipped. Set externally on PR merge."""

LIFECYCLE_LABEL_SET: Final[frozenset[str]] = frozenset({IMPLEMENT, IN_FLIGHT, PR_OPEN, DONE})


# --------------------------------------------------------------------------
# Sticky modifiers (orthogonal - may coexist with any lifecycle label).
# --------------------------------------------------------------------------

DO_NOT_PICKUP: Final[str] = "do-not-pickup"
"""Operator override: agents must not claim this issue."""

NEEDS_HUMAN_SCOPE: Final[str] = "needs:human-scope"
"""Issue is too vague for autonomous work; not eligible for pickup."""

STICKY_LABEL_SET: Final[frozenset[str]] = frozenset({DO_NOT_PICKUP, NEEDS_HUMAN_SCOPE})


# --------------------------------------------------------------------------
# Bundle / large-feature labels.
# --------------------------------------------------------------------------

LARGE_FEATURE: Final[str] = "agent:large-feature"
"""Multi-repo feature; picked up as a bundle by Batman."""

BUNDLE_LABEL_PREFIX: Final[str] = "agent:bundle:"
"""Prefix for per-bundle labels (``agent:bundle:<slug>``)."""

PLAN_PENDING_APPROVAL: Final[str] = "agent:plan-pending-approval"
"""Set on a bundle parent while Batman waits on operator approval before execute."""

NEEDS_HUMAN_REVIEW: Final[str] = "agent:needs-human-review"
"""Human review is required before any autonomous pickup."""

NEEDS_INFO: Final[str] = "needs:info"
"""Reporter needs to provide missing detail before autonomous work."""

LEGACY_PR_OPEN: Final[str] = "lucius-pr-open"
"""Legacy PR-open marker kept for dashboards and old runners."""

AGENT_PR_OPEN_SUFFIX: Final[str] = "-pr-open"
"""Suffix for agent-specific PR-open markers, e.g. ``custom-lucius-pr-open``."""

DONE_ALREADY: Final[str] = "done-already"
"""Legacy terminal marker for already-complete issues."""

FEATURE: Final[str] = "feature"
"""Human/product feature request label, not an autonomous implementation gate."""

ENHANCEMENT: Final[str] = "enhancement"
"""Human/product enhancement label, not an autonomous implementation gate."""

# Legacy long-form alias kept for backward compatibility with downstream code
# that imports ``LABEL_AGENT_PLAN_PENDING_APPROVAL`` directly (lib/slack_approval.py,
# lib/batman.py, and any operator extension code that mirrored the original
# slack_approval module's naming).
LABEL_AGENT_PLAN_PENDING_APPROVAL: Final[str] = PLAN_PENDING_APPROVAL


def bundle_label(slug: str) -> str:
    """Return the bundle label for a given slug.

    Args:
        slug: bundle slug, e.g. ``"oauth-rollout"``.

    Returns:
        The full label string, e.g. ``"agent:bundle:oauth-rollout"``.

    Raises:
        ValueError: if the slug is empty or contains whitespace.
    """
    if not slug or any(c.isspace() for c in slug):
        raise ValueError(f"invalid bundle slug: {slug!r}")
    return f"{BUNDLE_LABEL_PREFIX}{slug}"


def is_bundle_label(label: str) -> bool:
    """True if ``label`` is an ``agent:bundle:<slug>`` label."""
    return label.startswith(BUNDLE_LABEL_PREFIX) and len(label) > len(BUNDLE_LABEL_PREFIX)


def bundle_slug(label: str) -> str | None:
    """Extract the slug from a bundle label; ``None`` if not a bundle label."""
    if not is_bundle_label(label):
        return None
    return label[len(BUNDLE_LABEL_PREFIX) :]


def is_agent_pr_open_label(label: str) -> bool:
    """True if ``label`` is an agent-specific PR-open marker."""
    return label.endswith(AGENT_PR_OPEN_SUFFIX) and label != PR_OPEN


def agent_pr_open_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return every agent-specific PR-open marker in the set, sorted."""
    return sorted(label for label in labels if is_agent_pr_open_label(label))


PICKUP_BLOCKING_LABEL_SET: Final[frozenset[str]] = frozenset(
    {
        IN_FLIGHT,
        PR_OPEN,
        LEGACY_PR_OPEN,
        DO_NOT_PICKUP,
        NEEDS_HUMAN_SCOPE,
        NEEDS_HUMAN_REVIEW,
        NEEDS_INFO,
        DONE,
        DONE_ALREADY,
        LARGE_FEATURE,
        PLAN_PENDING_APPROVAL,
    }
)
"""Static labels that make an issue ineligible for autonomous pickup."""

CLAIM_BLOCKING_LABEL_SET: Final[frozenset[str]] = frozenset(
    label
    for label in PICKUP_BLOCKING_LABEL_SET
    if label not in {LARGE_FEATURE, PLAN_PENDING_APPROVAL}
)
"""Static labels that make an atomic issue claim unsafe.

Unlike pickup scanning, claim-time blocking deliberately ignores
``agent:large-feature``, ``agent:plan-pending-approval``, and dynamic
``agent:bundle:*`` labels. Batman owns those labels and must still be able to
claim bundle members and approval parents atomically after it has selected
eligible work.
"""

ROBIN_TRIAGE_BLOCKING_LABEL_SET: Final[frozenset[str]] = PICKUP_BLOCKING_LABEL_SET
"""Labels that make an issue ineligible for Robin to turn into implementation work."""

FEATURE_DEV_PRODUCT_CLAIM_BLOCKING_LABEL_SET: Final[frozenset[str]] = frozenset(
    {FEATURE, ENHANCEMENT}
)
"""Product labels that make a feature-dev claim unsafe before Robin promotion."""

FEATURE_DEV_ALWAYS_CLAIM_BLOCKING_LABEL_SET: Final[frozenset[str]] = frozenset(
    {LARGE_FEATURE, PLAN_PENDING_APPROVAL}
)
"""Batman-owned labels that always make a feature-dev claim unsafe."""


def pickup_blocking_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return sorted labels that block autonomous pickup.

    Dynamic ``agent:bundle:<slug>`` labels also block pickup because Batman
    owns bundle claims as an atomic unit.
    """
    s = set(labels)
    blockers = set(s & PICKUP_BLOCKING_LABEL_SET)
    blockers.update(bundle_labels(s))
    blockers.update(agent_pr_open_labels(s))
    return sorted(blockers)


def has_pickup_blocker(labels: set[str] | frozenset[str] | list[str]) -> bool:
    """True when any label blocks autonomous pickup."""
    return bool(pickup_blocking_labels(labels))


def _is_robin_promoted(labels: set[str]) -> bool:
    return IMPLEMENT in labels and any(label.startswith("severity:") for label in labels)


def feature_dev_pickup_blocking_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return labels that block the feature-dev agent from picking up work.

    Batman-created child issues keep their ``agent:bundle:<slug>`` provenance
    label while also carrying ``agent:implement``. Lucius must still pick those
    up. Static large-feature / approval labels remain blockers. Human/product
    feature labels stay blocked until Robin adds severity and ``agent:implement``.
    """
    s = set(labels)
    blockers = set(s & PICKUP_BLOCKING_LABEL_SET)
    blockers.update(agent_pr_open_labels(s))
    if not _is_robin_promoted(s):
        blockers.update(s & FEATURE_DEV_PRODUCT_CLAIM_BLOCKING_LABEL_SET)
    return sorted(blockers)


def has_feature_dev_pickup_blocker(labels: set[str] | frozenset[str] | list[str]) -> bool:
    """True when labels block the feature-dev pickup path."""
    return bool(feature_dev_pickup_blocking_labels(labels))


def feature_dev_claim_blocking_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return labels that block a feature-dev claim after pickup."""
    s = set(labels)
    blockers = set(s & FEATURE_DEV_ALWAYS_CLAIM_BLOCKING_LABEL_SET)
    blockers.update(agent_pr_open_labels(s))
    if not _is_robin_promoted(s):
        blockers.update(s & FEATURE_DEV_PRODUCT_CLAIM_BLOCKING_LABEL_SET)
    return sorted(blockers)


def claim_blocking_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return sorted labels that block the shared claim primitive."""
    s = set(labels)
    blockers = set(s & CLAIM_BLOCKING_LABEL_SET)
    blockers.update(agent_pr_open_labels(s))
    return sorted(blockers)


def robin_triage_blocking_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return sorted labels that block Robin from adding ``agent:implement``."""
    s = set(labels)
    blockers = set(s & ROBIN_TRIAGE_BLOCKING_LABEL_SET)
    blockers.update(bundle_labels(s))
    blockers.update(agent_pr_open_labels(s))
    return sorted(blockers)


# --------------------------------------------------------------------------
# Author / provenance labels.
# --------------------------------------------------------------------------

AUTHORED: Final[str] = "agent:authored"
"""PR was authored by an agent (vs. an operator). Set on PR open."""


# --------------------------------------------------------------------------
# Claim-comment prefixes (HTML-comment audit trail).
# --------------------------------------------------------------------------

CLAIM_COMMENT_PREFIX: Final[str] = "<!-- agent-claim:"
RELEASE_COMMENT_PREFIX: Final[str] = "<!-- agent-release:"


# --------------------------------------------------------------------------
# Framework-provided label definitions (name, color, description).
# ensure_labels() reads this list to create missing labels on a repo on
# first contact. Color values are hex without the leading '#'.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelDef:
    """A GitHub label definition.

    Frozen so the module-level list of definitions is safe to share.
    """

    name: str
    color: str  # six-char hex, no leading '#'
    description: str


LIFECYCLE_LABEL_DEFS: Final[tuple[LabelDef, ...]] = (
    LabelDef(IN_FLIGHT, "e11d21", "An agent is actively working this issue."),
    LabelDef(PR_OPEN, "fbca04", "A PR exists for this issue. Set by release_issue on success."),
    LabelDef(DONE, "0e8a16", "Issue shipped. Set externally on PR merge."),
    LabelDef(DO_NOT_PICKUP, "5319e7", "Operator override: agents must not claim this issue."),
    LabelDef(
        NEEDS_HUMAN_SCOPE,
        "e99695",
        "Issue requires manual scoping; not eligible for autonomous pickup.",
    ),
    LabelDef(
        LARGE_FEATURE,
        "ff6b00",
        "Multi-repo feature; picked up as a bundle by Batman.",
    ),
    LabelDef(
        AUTHORED,
        "c2e0c6",
        "PR was authored by an agent (vs. an operator). Set on PR open.",
    ),
)
"""Tuple of every label the framework guarantees on a repo it touches."""

LIFECYCLE_LABELS_TUPLES: Final[tuple[tuple[str, str, str], ...]] = tuple(
    (d.name, d.color, d.description) for d in LIFECYCLE_LABEL_DEFS
)
"""Back-compat shape matching agent_runner.LIFECYCLE_LABELS."""


# --------------------------------------------------------------------------
# State machine: legal transitions.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """A legal lifecycle move.

    ``trigger`` names the event that drove the move (in code paths or in
    operator commands). Not enforced; the doc value of the table is what
    matters.
    """

    src: str
    dst: str
    trigger: str


_TRANSITIONS: Final[tuple[Transition, ...]] = (
    Transition("(none)", IMPLEMENT, "drake / human files issue"),
    Transition(IMPLEMENT, IN_FLIGHT, "claim_issue()"),
    Transition(IMPLEMENT, NEEDS_HUMAN_SCOPE, "3+ failed attempts"),
    Transition(IN_FLIGHT, IMPLEMENT, "release_issue(transition_to=None)"),
    Transition(IN_FLIGHT, PR_OPEN, "release_issue(transition_to=agent:pr-open)"),
    Transition(IN_FLIGHT, IMPLEMENT, "stale-claim sweep (>max_age_hours)"),
    Transition(IN_FLIGHT, IMPLEMENT, "race-yield to earlier claim"),
    Transition(PR_OPEN, DONE, "automerge or human merge"),
    Transition(PR_OPEN, IMPLEMENT, "PR closed without merge"),
)


def legal_transitions(src: str) -> tuple[Transition, ...]:
    """Return every legal transition from ``src``."""
    return tuple(t for t in _TRANSITIONS if t.src == src)


def is_legal_transition(src: str, dst: str) -> bool:
    """True if ``src -> dst`` is a documented lifecycle move."""
    return any(t.src == src and t.dst == dst for t in _TRANSITIONS)


def all_transitions() -> tuple[Transition, ...]:
    """Return the full transition table (for docs / introspection)."""
    return _TRANSITIONS


# --------------------------------------------------------------------------
# Inspection helpers - pure predicates on label sets.
# --------------------------------------------------------------------------


def lifecycle_state(labels: set[str] | frozenset[str] | list[str]) -> str | None:
    """Return the active lifecycle label on an issue, or ``None`` if none.

    A well-formed issue carries at most one lifecycle label. If multiple
    are present (the state machine got into a bad state) the most
    advanced one wins: ``done`` > ``pr_open`` > ``in_flight`` > ``implement``.
    """
    s = set(labels)
    for label in (DONE, PR_OPEN, IN_FLIGHT, IMPLEMENT):
        if label in s:
            return label
    return None


def has_blocker(labels: set[str] | frozenset[str] | list[str]) -> bool:
    """Legacy alias for pickup-blocking labels."""
    return has_pickup_blocker(labels)


def bundle_labels(labels: set[str] | frozenset[str] | list[str]) -> list[str]:
    """Return every ``agent:bundle:<slug>`` label in the set, sorted."""
    return sorted(label for label in labels if is_bundle_label(label))


# --------------------------------------------------------------------------
# Operator-config plumbing (env-driven; no hardcoded repo names).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelStateConfig:
    """Configuration for the operator CLI and sweep helpers.

    Sourced from env vars (12-factor). All fields are read-only; build
    via :func:`LabelStateConfig.from_env` to pick up overrides.
    """

    gh_org: str = ""
    sweep_repos: tuple[str, ...] = field(default_factory=tuple)
    alfred_home: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LabelStateConfig:
        """Build a config from an env mapping (defaults to ``os.environ``).

        ``LABEL_STATE_SWEEP_REPOS`` is comma-separated; whitespace is
        stripped. Missing or empty yields an empty tuple - the caller
        decides whether that's an error (typically ``sweep --repo`` is
        then required).
        """
        if env is None:
            import os as _os

            env = dict(_os.environ)
        raw = env.get("LABEL_STATE_SWEEP_REPOS", "").strip()
        sweep = tuple(r.strip() for r in raw.split(",") if r.strip()) if raw else ()
        return cls(
            gh_org=env.get("GH_ORG", "").strip(),
            sweep_repos=sweep,
            alfred_home=env.get("ALFRED_HOME", "").strip(),
        )
