"""Server-side persistence for the active roster theme and custom names.

The roster theme is the named display layer applied to the agent roster: the
shipped Batman roster by default, plus presets (Transformers, Justice League) and an
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

_BATMAN_BASE_THEME_ID = "batman"

# A fleet codename is a short slug (``batman``, ``fleet-doctor``). We never store
# anything that does not look like one, so the map can never be abused to carry
# free text. Length is bounded to keep a single entry small.
_CODENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _load_roster_manifest() -> dict[str, Any]:
    path = Path(__file__).with_name("roster_manifest.json")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return _validate_roster_manifest(payload, path)


def _manifest_error(path: Path, message: str) -> RuntimeError:
    return RuntimeError(f"invalid roster manifest {path}: {message}")


def _validate_roster_manifest(payload: Any, path: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _manifest_error(path, "top-level payload must be an object")

    preset_ids_raw = payload.get("preset_theme_ids")
    if (
        not isinstance(preset_ids_raw, list)
        or not preset_ids_raw
        or not all(isinstance(theme_id, str) and theme_id.strip() for theme_id in preset_ids_raw)
    ):
        raise _manifest_error(path, "preset_theme_ids must be a non-empty string array")
    preset_ids = tuple(theme_id.strip() for theme_id in preset_ids_raw)
    if _BATMAN_BASE_THEME_ID not in preset_ids:
        raise _manifest_error(path, f"preset_theme_ids must include {_BATMAN_BASE_THEME_ID!r}")

    default_theme = payload.get("default_theme")
    if not isinstance(default_theme, str) or default_theme not in preset_ids:
        raise _manifest_error(path, "default_theme must be one of preset_theme_ids")

    role_labels = payload.get("role_labels")
    if not isinstance(role_labels, Mapping) or not role_labels:
        raise _manifest_error(path, "role_labels must be a non-empty object")
    for role, label in role_labels.items():
        if not isinstance(role, str) or not role.strip():
            raise _manifest_error(path, "role_labels keys must be non-empty strings")
        if not isinstance(label, str) or not label.strip():
            raise _manifest_error(path, f"role_labels[{role!r}] must be a non-empty string")

    themes = payload.get("themes")
    if not isinstance(themes, Mapping):
        raise _manifest_error(path, "themes must be an object keyed by preset theme id")
    for theme_id in preset_ids:
        meta = themes.get(theme_id)
        if not isinstance(meta, Mapping):
            raise _manifest_error(path, f"themes[{theme_id!r}] must be an object")
        for field in ("label", "blurb"):
            value = meta.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _manifest_error(path, f"themes[{theme_id!r}].{field} must be non-empty")

    agents = payload.get("agents")
    if not isinstance(agents, list) or not agents:
        raise _manifest_error(path, "agents must be a non-empty array")
    seen: set[str] = set()
    for index, agent in enumerate(agents):
        if not isinstance(agent, Mapping):
            raise _manifest_error(path, f"agents[{index}] must be an object")
        codename = agent.get("codename")
        if not isinstance(codename, str) or not _CODENAME_RE.fullmatch(codename):
            raise _manifest_error(path, f"agents[{index}].codename is not a valid codename")
        if codename in seen:
            raise _manifest_error(path, f"duplicate agent codename {codename!r}")
        seen.add(codename)
        role = agent.get("role")
        if not isinstance(role, str) or role not in role_labels:
            raise _manifest_error(path, f"agents[{index}].role must reference role_labels")
        names = agent.get("names")
        if not isinstance(names, Mapping):
            raise _manifest_error(path, f"agents[{index}].names must be an object")
        for theme_id in preset_ids:
            name = names.get(theme_id)
            if not isinstance(name, str) or not name.strip():
                raise _manifest_error(
                    path, f"agents[{index}].names[{theme_id!r}] must be non-empty"
                )

    return payload


_ROSTER_MANIFEST = _load_roster_manifest()
_MANIFEST_AGENTS: tuple[dict[str, Any], ...] = tuple(_ROSTER_MANIFEST.get("agents") or ())
_ROLE_LABELS_DEFAULT: dict[str, str] = {
    str(role): str(label) for role, label in dict(_ROSTER_MANIFEST.get("role_labels") or {}).items()
}

# The preset ids the client ships. ``custom`` is the operator-authored theme
# whose names/roles live in this store. Kept in lockstep with the desktop
# ``agentThemes.ts`` RosterThemeId union; a value outside this set is rejected so
# a typo can never silently persist an unknown theme.
PRESET_THEME_IDS: tuple[str, ...] = tuple(
    str(theme_id) for theme_id in (_ROSTER_MANIFEST.get("preset_theme_ids") or ())
)
CUSTOM_THEME_ID = "custom"
VALID_THEME_IDS: tuple[str, ...] = (*PRESET_THEME_IDS, CUSTOM_THEME_ID)
DEFAULT_THEME_ID = str(_ROSTER_MANIFEST.get("default_theme") or "batman")

# Operator-chosen display names and role labels are short, human, single-line.
# We strip control characters and bound the length so a name can never carry a
# newline (which would break a Slack header line) or an unbounded blob.
_MAX_LABEL_LEN = 64
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# A custom theme can only name the fleet it actually has; cap the number of
# entries so a malformed payload cannot grow the file without bound.
_MAX_CUSTOM_ENTRIES = 128

# The shipped Batman display name per known fleet codename. The desktop builds a
# ``custom`` theme on top of this base, so an agent the operator has NOT renamed
# still shows its Batman-base name there. Derived from ``roster_manifest.json``
# so Python and the desktop share one roster contract.
BATMAN_BASE_NAMES: dict[str, str] = {
    str(agent["codename"]): str(agent["names"][_BATMAN_BASE_THEME_ID]) for agent in _MANIFEST_AGENTS
}

# The shipped Batman role label per known fleet codename. Derived from the
# manifest's canonical role for the codename plus its role label table.
BATMAN_BASE_ROLES: dict[str, str] = {
    str(agent["codename"]): _ROLE_LABELS_DEFAULT[str(agent["role"])] for agent in _MANIFEST_AGENTS
}

# The preset rosters re-skin the SAME fleet as the Batman base; only the display
# name changes. Derived from the manifest so new codenames/themes cannot drift
# between Python Slack rendering and the desktop.
PRESET_DISPLAY_NAMES: dict[str, dict[str, str]] = {
    theme_id: {str(agent["codename"]): str(agent["names"][theme_id]) for agent in _MANIFEST_AGENTS}
    for theme_id in PRESET_THEME_IDS
    if theme_id != _BATMAN_BASE_THEME_ID
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
        # roster: retain whatever the operator last saved. Under a preset the names
        # are kept on disk but never exposed (display_name_for/role_label_for
        # return None for any non-custom theme); switching back to ``custom`` (or
        # a restart) then restores them. Only an explicit custom payload on this
        # write replaces the retained roster, so the operator can still clear it.
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
