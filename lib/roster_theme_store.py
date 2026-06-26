"""Server-side persistence for the active roster theme and custom names.

The roster theme is the named cast applied to the agent roster: the shipped
Batman roster by default, plus presets (Transformers, Justice League) and an
operator-authored ``custom`` theme. The desktop client picked themes in #303 but
only persisted the choice to ``localStorage``, so the choice never reached other
surfaces. This module gives the choice one inspectable home under the runtime
state dir so every surface (the desktop AND the Slack message path) can honor the
same theme and the same operator-authored names.

What is stored, and only this:

* ``theme``        the active preset id (``batman`` by default).
* ``custom_names`` an optional map of fleet codename -> operator-chosen display
                   name, used only when ``theme`` is ``custom``.
* ``custom_roles`` an optional map of fleet codename -> operator-chosen role
                   label, used only when ``theme`` is ``custom``.

No message text, no Slack ids, nothing else. The file is written atomically under
``$ALFRED_HOME/state/roster-theme/roster-theme.json`` so a running Slack listener
can pick up a theme change without a restart. Presets stay in the client; the
server only needs to know which preset is active and the operator's custom names,
so the server never has to be redeployed to add a preset.
"""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The preset ids the client ships. ``custom`` is the operator-authored theme
# whose names/roles live in this store. Kept in lockstep with the desktop
# ``agentThemes.ts`` RosterThemeId union; a value outside this set is rejected so
# a typo can never silently persist an unknown theme.
PRESET_THEME_IDS: tuple[str, ...] = ("batman", "transformers", "justice-league")
CUSTOM_THEME_ID = "custom"
VALID_THEME_IDS: tuple[str, ...] = (*PRESET_THEME_IDS, CUSTOM_THEME_ID)
DEFAULT_THEME_ID = "batman"

# A fleet codename is a short slug (``batman``, ``fleet-doctor``). We never store
# anything that does not look like one, so the map can never be abused to carry
# free text. Length is bounded to keep a single entry small.
_CODENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Operator-chosen display names and role labels are short, human, single-line.
# We strip control characters and bound the length so a name can never carry a
# newline (which would break a Slack header line) or an unbounded blob.
_MAX_LABEL_LEN = 64
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# A custom theme can only name the fleet it actually has; cap the number of
# entries so a malformed payload cannot grow the file without bound.
_MAX_CUSTOM_ENTRIES = 128

# The shipped Batman display name per known fleet codename. The desktop builds a
# ``custom`` theme on top of this base (agentThemes.ts ``BATMAN_THEME``), so an
# agent the operator has NOT renamed still shows its Batman-base name there. This
# table mirrors that base so the Slack path resolves the same name for an unnamed
# agent under a custom theme, instead of falling back to the bare codename and
# diverging from the desktop. Kept in lockstep with ``agentThemes.ts``.
BATMAN_BASE_NAMES: dict[str, str] = {
    "robin": "Robin",
    "drake": "Drake",
    "damian": "Damian",
    "batman": "Batman",
    "lucius": "Lucius",
    "bane": "Bane",
    "nightwing": "Nightwing",
    "rasalghul": "Ra's al Ghul",
    "huntress": "Huntress",
    "automerge": "Auto-merge",
    "gordon": "Gordon",
    "fleet-doctor": "Fleet doctor",
    "cleanup": "Cleanup",
    "agent-cleanup": "Cleanup",
    "memory-harvest": "Memory harvest",
    "code-map-refresh": "Code map",
    "proof-telemetry": "Telemetry",
}

# The shipped Batman role label per known fleet codename. The desktop custom
# theme falls back to this Batman-base role label (agentThemes.ts
# ``ROLE_LABELS_DEFAULT`` keyed via ``CODENAME_ROLE_HINTS``) when the operator
# names an agent but sets no per-agent role label. The Slack path must resolve
# the SAME label there, instead of falling back to the ``ALFRED_<CODENAME>_ROLE``
# env label, so a saved ``batman -> Sherlock`` without a custom role renders
# identically on both surfaces. Kept in lockstep with ``agentThemes.ts``.
BATMAN_BASE_ROLES: dict[str, str] = {
    "robin": "Triage lead",
    "drake": "Triage lead",
    "damian": "Triage lead",
    "batman": "Architect",
    "lucius": "Senior developer",
    "bane": "Senior developer",
    "nightwing": "Senior developer",
    "rasalghul": "Reviewer",
    "automerge": "Release",
    "gordon": "Ops & health",
    "fleet-doctor": "Ops & health",
    "huntress": "Ops & health",
    "cleanup": "Ops & health",
    "agent-cleanup": "Ops & health",
    "memory-harvest": "Ops & health",
    "code-map-refresh": "Ops & health",
    "proof-telemetry": "Ops & health",
}


# The preset rosters re-skin the SAME cast as the Batman base, so each preset
# names every codename ``BATMAN_BASE_NAMES`` does. The presets share the Batman
# role labels (agentThemes.ts gives every preset ``ROLE_LABELS_DEFAULT`` with no
# per-codename override), so a preset's role label is ``BATMAN_BASE_ROLES`` for
# the codename; only the display name changes. Kept in lockstep with the
# desktop ``agentThemes.ts`` preset ``nameByCodename`` maps; a parity test holds
# the codename set identical to ``BATMAN_BASE_NAMES`` so a new agent cannot be
# named under Batman without also being named under every preset.
PRESET_DISPLAY_NAMES: dict[str, dict[str, str]] = {
    "transformers": {
        "robin": "Bumblebee",
        "drake": "Hot Rod",
        "damian": "Blurr",
        "batman": "Optimus Prime",
        "lucius": "Ironhide",
        "bane": "Grimlock",
        "nightwing": "Sideswipe",
        "rasalghul": "Ratchet",
        "huntress": "Arcee",
        "automerge": "Jazz",
        "gordon": "Wheeljack",
        "fleet-doctor": "Perceptor",
        "cleanup": "Cosmos",
        "agent-cleanup": "Cosmos",
        "memory-harvest": "Brainstorm",
        "code-map-refresh": "Beachcomber",
        "proof-telemetry": "Blaster",
    },
    "justice-league": {
        "robin": "The Flash",
        "drake": "Green Arrow",
        "damian": "Hawkgirl",
        "batman": "Batman",
        "lucius": "Superman",
        "bane": "Shazam",
        "nightwing": "Aquaman",
        "rasalghul": "Wonder Woman",
        "huntress": "Martian Manhunter",
        "automerge": "Green Lantern",
        "gordon": "Cyborg",
        "fleet-doctor": "Doctor Fate",
        "cleanup": "Atom",
        "agent-cleanup": "Atom",
        "memory-harvest": "Zatanna",
        "code-map-refresh": "Vixen",
        "proof-telemetry": "Firestorm",
    },
}


class RosterThemeError(ValueError):
    """Raised when an inbound theme payload fails validation."""


@dataclass(frozen=True)
class RosterThemeState:
    """The persisted roster-theme choice plus operator-authored names."""

    theme: str
    custom_names: Mapping[str, str]
    custom_roles: Mapping[str, str]
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "custom_names": dict(self.custom_names),
            "custom_roles": dict(self.custom_roles),
            "updated_at": self.updated_at,
        }

    def display_name_for(self, codename: str) -> str | None:
        """Operator-chosen display name for a codename, or ``None``.

        Only the ``custom`` theme carries names here; presets are resolved
        client-side / by the Slack path's own preset table, so this returns
        ``None`` for every non-custom theme.
        """
        if self.theme != CUSTOM_THEME_ID:
            return None
        return self.custom_names.get(_normalize_codename(codename) or "")

    def role_label_for(self, codename: str) -> str | None:
        """Operator-chosen role label for a codename, or ``None``."""
        if self.theme != CUSTOM_THEME_ID:
            return None
        return self.custom_roles.get(_normalize_codename(codename) or "")

    def custom_display_name_for(self, codename: str) -> str | None:
        """Resolve the desktop-equivalent display name under the custom theme.

        Under the ``custom`` theme the desktop builds names on the Batman base,
        so an agent the operator has NOT renamed still shows its Batman-base
        name (``Lucius``), never the bare codename. This mirrors that: the
        operator's custom name when set, else the Batman base name for a known
        codename. Returns ``None`` for a non-custom theme or an unknown codename
        so the caller keeps the shipped behavior for those.
        """
        if self.theme != CUSTOM_THEME_ID:
            return None
        short = _normalize_codename(codename) or ""
        return self.custom_names.get(short) or BATMAN_BASE_NAMES.get(short)

    def custom_role_label_for(self, codename: str) -> str | None:
        """Resolve the desktop-equivalent role label under the custom theme.

        Under the ``custom`` theme the desktop overlays the operator's per-agent
        role label on the Batman-base role label (agentThemes.ts
        ``roleLabelByCodename`` over ``ROLE_LABELS_DEFAULT``), NOT on the
        ``ALFRED_<CODENAME>_ROLE`` env label. This mirrors that: the operator's
        custom role when set, else the Batman base role for a known codename. So
        a ``batman -> Sherlock`` with no custom role renders ``Sherlock
        (Architect)`` on both the desktop and Slack. Returns ``None`` for a
        non-custom theme or an unknown codename so the caller keeps the shipped
        env-role behavior for those.
        """
        if self.theme != CUSTOM_THEME_ID:
            return None
        short = _normalize_codename(codename) or ""
        return self.custom_roles.get(short) or BATMAN_BASE_ROLES.get(short)

    def themed_display_name_for(self, codename: str) -> str | None:
        """Display name for a codename under the ACTIVE theme, or ``None``.

        This is the theme-aware resolver the Slack path uses so every saved
        theme renders on Slack the way it does on the desktop:

        * ``custom``  -> the operator's name, else the Batman base name.
        * a preset    -> the preset's themed name (``Optimus Prime``).
        * ``batman``  -> ``None``, so the caller keeps the shipped
          ``codename_with_role`` rendering unchanged.

        Returns ``None`` for an unknown codename so the caller falls back to the
        shipped behavior rather than inventing a name.
        """
        if self.theme == CUSTOM_THEME_ID:
            return self.custom_display_name_for(codename)
        preset = PRESET_DISPLAY_NAMES.get(self.theme)
        if preset is None:
            return None
        return preset.get(_normalize_codename(codename) or "")

    def themed_role_label_for(self, codename: str) -> str | None:
        """Role label for a codename under the ACTIVE theme, or ``None``.

        The presets share the Batman role labels (agentThemes.ts gives each
        preset ``ROLE_LABELS_DEFAULT`` with no per-codename override), so a
        preset resolves to the Batman base role for the codename. ``custom``
        keeps its own per-agent overlay; ``batman`` returns ``None`` so the
        caller keeps the shipped env-role behavior.
        """
        if self.theme == CUSTOM_THEME_ID:
            return self.custom_role_label_for(codename)
        if self.theme in PRESET_DISPLAY_NAMES:
            return BATMAN_BASE_ROLES.get(_normalize_codename(codename) or "")
        return None


def default_theme_state() -> RosterThemeState:
    """The unchanged default: the Batman roster, no custom names."""
    return RosterThemeState(theme=DEFAULT_THEME_ID, custom_names={}, custom_roles={})


class RosterThemeStore:
    """Atomically stores the active roster theme and custom name/role maps."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "roster-theme.json"
        self.lock_path = root / "roster-theme.lock"

    @classmethod
    def from_state_root(cls, state_root: Path) -> RosterThemeStore:
        return cls(Path(state_root) / "roster-theme")

    def load(self) -> RosterThemeState:
        """Return the persisted state, falling back to the default.

        Never raises on a missing or malformed file: an unreadable store
        degrades to the shipped Batman default rather than breaking the surface
        that reads it (the Slack path must keep posting even if the file is
        corrupt). Unknown themes and malformed entries are dropped.
        """
        payload = self._read_payload()
        theme = _coerce_theme(payload.get("theme"))
        custom_names = _coerce_label_map(payload.get("custom_names"))
        custom_roles = _coerce_label_map(payload.get("custom_roles"))
        return RosterThemeState(
            theme=theme,
            custom_names=custom_names,
            custom_roles=custom_roles,
            updated_at=_coerce_str(payload.get("updated_at")),
        )

    def save(
        self,
        *,
        theme: str,
        custom_names: Mapping[str, Any] | None = None,
        custom_roles: Mapping[str, Any] | None = None,
    ) -> RosterThemeState:
        """Validate and persist a theme choice plus optional custom maps.

        Raises :class:`RosterThemeError` when ``theme`` is not a known id or a
        custom map is malformed (bad codename, empty/over-long label, too many
        entries). The write is atomic under a file lock so a concurrent Slack
        read never sees a half-written file.
        """
        normalized_theme = _coerce_theme(theme, strict=True)
        names = _validate_label_map(custom_names, field="custom_names")
        roles = _validate_label_map(custom_roles, field="custom_roles")
        # A theme switch that carries no custom payload (the desktop sends only a
        # ``theme`` when it flips presets) must NOT delete the authored custom
        # cast: retain whatever the operator last saved. Under a preset the names
        # are kept on disk but never exposed (display_name_for/role_label_for
        # return None for any non-custom theme); switching back to ``custom`` (or
        # a restart) then restores them. Only an explicit custom payload on this
        # write replaces the retained cast, so the operator can still clear it.
        retain_existing = custom_names is None and custom_roles is None
        with self._locked():
            if retain_existing:
                existing = self.load()
                names = dict(existing.custom_names)
                roles = dict(existing.custom_roles)
            self._write(theme=normalized_theme, custom_names=names, custom_roles=roles)
        return RosterThemeState(
            theme=normalized_theme,
            custom_names=names,
            custom_roles=roles,
            updated_at=_utc_now(),
        )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_payload(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(
        self,
        *,
        theme: str,
        custom_names: Mapping[str, str],
        custom_roles: Mapping[str, str],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _utc_now(),
            "theme": theme,
            "custom_names": dict(sorted(custom_names.items())),
            "custom_roles": dict(sorted(custom_roles.items())),
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def _normalize_codename(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    # Slack/runtime codenames sometimes arrive dotted (``alfred.batman``); keep
    # the last segment so the map keys on the bare codename the presets use.
    text = (text.split(".")[-1] or "").strip()
    return text if _CODENAME_RE.match(text) else None


def _clean_label(value: Any) -> str | None:
    text = _CONTROL_CHARS_RE.sub(" ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    return text[:_MAX_LABEL_LEN]


def _coerce_theme(value: Any, *, strict: bool = False) -> str:
    text = str(value or "").strip().lower()
    if text in VALID_THEME_IDS:
        return text
    if strict:
        raise RosterThemeError(f"unknown roster theme: {value!r}")
    return DEFAULT_THEME_ID


def _coerce_label_map(value: Any) -> dict[str, str]:
    """Best-effort read: drop malformed entries instead of raising."""
    out: dict[str, str] = {}
    if not isinstance(value, Mapping):
        return out
    for key, raw in value.items():
        codename = _normalize_codename(key)
        label = _clean_label(raw)
        if codename and label:
            out[codename] = label
        if len(out) >= _MAX_CUSTOM_ENTRIES:
            break
    return out


def _validate_label_map(value: Mapping[str, Any] | None, *, field: str) -> dict[str, str]:
    """Strict read for inbound writes: reject anything malformed."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RosterThemeError(f"{field} must be an object")
    if len(value) > _MAX_CUSTOM_ENTRIES:
        raise RosterThemeError(f"{field} has too many entries (max {_MAX_CUSTOM_ENTRIES})")
    out: dict[str, str] = {}
    for key, raw in value.items():
        codename = _normalize_codename(key)
        if codename is None:
            raise RosterThemeError(f"{field}: not a fleet codename: {key!r}")
        label = _clean_label(raw)
        if label is None:
            raise RosterThemeError(f"{field}: empty name for {key!r}")
        out[codename] = label
    return out


def _coerce_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BATMAN_BASE_NAMES",
    "CUSTOM_THEME_ID",
    "DEFAULT_THEME_ID",
    "PRESET_THEME_IDS",
    "VALID_THEME_IDS",
    "RosterThemeError",
    "RosterThemeState",
    "RosterThemeStore",
    "default_theme_state",
]
