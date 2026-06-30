"""Tests for ``lib/roster_theme_store.py``.

The store is the single inspectable home for the active roster theme plus
operator-authored custom names, shared across the desktop and the Slack
message path. These tests cover the three things that matter: a clean
default, atomic round-trip persistence, and strict input validation on
writes paired with lenient coercion on reads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from roster_theme_store import (
    BATMAN_BASE_NAMES,
    BATMAN_BASE_ROLES,
    DEFAULT_THEME_ID,
    PRESET_DISPLAY_NAMES,
    RosterThemeError,
    RosterThemeState,
    RosterThemeStore,
    default_theme_state,
)


def _store(tmp_path: Path) -> RosterThemeStore:
    return RosterThemeStore.from_state_root(tmp_path / "state")


def test_load_missing_file_returns_batman_default(tmp_path: Path) -> None:
    state = _store(tmp_path).load()
    assert state.theme == DEFAULT_THEME_ID == "batman"
    assert dict(state.custom_names) == {}
    assert dict(state.custom_roles) == {}


def test_default_theme_state_is_batman() -> None:
    assert default_theme_state().theme == "batman"


def test_save_preset_round_trips(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.save(theme="transformers")
    assert saved.theme == "transformers"
    assert saved.updated_at is not None
    # A fresh load (new process simulation) reads the same value off disk.
    assert _store(tmp_path).load().theme == "transformers"


def test_save_custom_names_and_roles_persist_and_resolve(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(
        theme="custom",
        custom_names={"batman": "Sherlock", "fleet-doctor": "Watson"},
        custom_roles={"batman": "Lead detective"},
    )
    loaded = _store(tmp_path).load()
    assert loaded.theme == "custom"
    assert loaded.display_name_for("batman") == "Sherlock"
    assert loaded.display_name_for("fleet-doctor") == "Watson"
    assert loaded.role_label_for("batman") == "Lead detective"
    # A codename without a custom role resolves to None.
    assert loaded.role_label_for("fleet-doctor") is None


def test_dotted_codename_normalizes_to_bare_slug(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="custom", custom_names={"alfred.batman": "Sherlock"})
    loaded = _store(tmp_path).load()
    assert loaded.display_name_for("batman") == "Sherlock"


def test_preset_theme_does_not_expose_custom_names(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Under a preset, the authored roster is never exposed (display_name_for is
    # None), even though the names are retained on disk for a later switch back.
    store.save(theme="custom", custom_names={"batman": "Sherlock"})
    store.save(theme="batman")
    loaded = _store(tmp_path).load()
    assert loaded.theme == "batman"
    assert loaded.display_name_for("batman") is None


def test_preset_switch_retains_custom_roster_for_later_restore(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Author a custom roster, then temporarily switch to a preset with no payload.
    store.save(
        theme="custom",
        custom_names={"batman": "Sherlock"},
        custom_roles={"batman": "Lead detective"},
    )
    store.save(theme="justice-league")

    # The preset is active and exposes nothing, but the authored roster survives on
    # disk so a restart (fresh load) does not lose it.
    reloaded = _store(tmp_path).load()
    assert reloaded.theme == "justice-league"
    assert reloaded.display_name_for("batman") is None
    assert dict(reloaded.custom_names) == {"batman": "Sherlock"}
    assert dict(reloaded.custom_roles) == {"batman": "Lead detective"}

    # Switching back to custom (no payload) restores the authored roster intact.
    store.save(theme="custom")
    restored = _store(tmp_path).load()
    assert restored.theme == "custom"
    assert restored.display_name_for("batman") == "Sherlock"
    assert restored.role_label_for("batman") == "Lead detective"


def test_explicit_custom_payload_replaces_retained_roster(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="custom", custom_names={"batman": "Sherlock"})
    # An explicit (even empty) custom payload on a preset write clears the roster,
    # so the operator can deliberately discard it rather than have it linger.
    store.save(theme="batman", custom_names={}, custom_roles={})
    loaded = _store(tmp_path).load()
    assert loaded.theme == "batman"
    assert dict(loaded.custom_names) == {}


def test_custom_display_name_falls_back_to_batman_base(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="custom", custom_names={"batman": "Sherlock"})
    loaded = _store(tmp_path).load()
    # The renamed agent uses the operator name.
    assert loaded.custom_display_name_for("batman") == "Sherlock"
    # An un-renamed known agent uses the Batman-base name, not the bare codename,
    # so the Slack path matches the desktop (which overlays on the same base).
    assert loaded.custom_display_name_for("lucius") == "Lucius"
    # An unknown codename has no base persona, so it returns None.
    assert loaded.custom_display_name_for("mystery-bot") is None


def test_custom_display_name_is_none_for_preset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="justice-league")
    loaded = _store(tmp_path).load()
    assert loaded.custom_display_name_for("lucius") is None


def test_custom_role_label_falls_back_to_batman_base(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(
        theme="custom",
        custom_names={"batman": "Sherlock"},
        custom_roles={"batman": "Lead detective"},
    )
    loaded = _store(tmp_path).load()
    # The operator role wins when set.
    assert loaded.custom_role_label_for("batman") == "Lead detective"
    # A known agent with no custom role uses the Batman-base role label, matching
    # the desktop, not the env role or None.
    assert loaded.custom_role_label_for("lucius") == "Senior developer"
    # An unknown codename has no base role, so it returns None and the caller
    # keeps the shipped env-role behavior.
    assert loaded.custom_role_label_for("mystery-bot") is None


def test_custom_role_label_is_none_for_preset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="transformers")
    loaded = _store(tmp_path).load()
    assert loaded.custom_role_label_for("batman") is None


def test_save_rejects_unknown_theme(tmp_path: Path) -> None:
    with pytest.raises(RosterThemeError):
        _store(tmp_path).save(theme="nope")


def test_save_rejects_non_codename_key(tmp_path: Path) -> None:
    with pytest.raises(RosterThemeError):
        _store(tmp_path).save(theme="custom", custom_names={"Bad Key!": "X"})


def test_save_rejects_empty_label(tmp_path: Path) -> None:
    with pytest.raises(RosterThemeError):
        _store(tmp_path).save(theme="custom", custom_names={"batman": "   "})


def test_save_rejects_non_mapping_custom_names(tmp_path: Path) -> None:
    with pytest.raises(RosterThemeError):
        _store(tmp_path).save(theme="custom", custom_names=["not", "a", "map"])  # type: ignore[arg-type]


def test_save_rejects_too_many_entries(tmp_path: Path) -> None:
    too_many = {f"agent-{i}": f"name{i}" for i in range(200)}
    with pytest.raises(RosterThemeError):
        _store(tmp_path).save(theme="custom", custom_names=too_many)


def test_label_strips_control_chars_and_bounds_length(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.save(theme="custom", custom_names={"batman": "Sher\nlock" + "x" * 200})
    loaded = _store(tmp_path).load()
    name = loaded.display_name_for("batman")
    assert name is not None
    assert "\n" not in name
    assert len(name) <= 64


def test_load_drops_malformed_entries_without_raising(tmp_path: Path) -> None:
    # Hand-write a payload with junk an attacker or a stale writer might leave.
    root = tmp_path / "state" / "roster-theme"
    root.mkdir(parents=True)
    (root / "roster-theme.json").write_text(
        json.dumps(
            {
                "theme": "custom",
                "custom_names": {"batman": "Sherlock", "Not A Codename!": "x", "ok": ""},
            }
        ),
        encoding="utf-8",
    )
    loaded = RosterThemeStore.from_state_root(tmp_path / "state").load()
    assert loaded.display_name_for("batman") == "Sherlock"
    # The malformed key and the empty label are silently dropped.
    assert dict(loaded.custom_names) == {"batman": "Sherlock"}


def test_load_unknown_theme_falls_back_to_default(tmp_path: Path) -> None:
    root = tmp_path / "state" / "roster-theme"
    root.mkdir(parents=True)
    (root / "roster-theme.json").write_text(json.dumps({"theme": "garbage"}), encoding="utf-8")
    assert RosterThemeStore.from_state_root(tmp_path / "state").load().theme == "batman"


def test_preset_maps_cover_the_same_roster_as_batman_base() -> None:
    # Every preset re-skins the SAME fleet as the Batman base. If a new agent is
    # added to BATMAN_BASE_NAMES without a matching entry in each preset, Slack
    # would render that agent's bare codename under a preset while the desktop
    # shows a themed name. Hold the codename sets identical so that cannot ship.
    base = set(BATMAN_BASE_NAMES)
    for theme, names in PRESET_DISPLAY_NAMES.items():
        assert set(names) == base, f"{theme} preset roster drifted from the Batman base"


def test_batman_base_uses_canonical_scheduled_codenames() -> None:
    assert "cleanup" not in BATMAN_BASE_NAMES
    assert "cleanup" not in BATMAN_BASE_ROLES
    assert "agent-cleanup" in BATMAN_BASE_NAMES
    assert "memory-auto-promote" in BATMAN_BASE_NAMES
    assert "agent-morning-brief" in BATMAN_BASE_NAMES
    assert "shipped-summary-weekly" in BATMAN_BASE_NAMES


def test_themed_display_name_resolves_preset_identity() -> None:
    state = RosterThemeState(theme="transformers", custom_names={}, custom_roles={})
    assert state.themed_display_name_for("lucius") == "Ironhide"
    assert state.themed_display_name_for("batman") == "Optimus Prime"
    # Role label comes from the Batman base the presets share.
    assert state.themed_role_label_for("lucius") == BATMAN_BASE_ROLES["lucius"]


def test_themed_display_name_batman_theme_keeps_shipped_behavior() -> None:
    state = RosterThemeState(theme="batman", custom_names={}, custom_roles={})
    # Batman theme returns None so the caller keeps codename_with_role.
    assert state.themed_display_name_for("lucius") is None
    assert state.themed_role_label_for("lucius") is None


def test_themed_display_name_custom_theme_uses_custom_overlay() -> None:
    state = RosterThemeState(
        theme="custom",
        custom_names={"batman": "Sherlock"},
        custom_roles={"batman": "Lead detective"},
    )
    assert state.themed_display_name_for("batman") == "Sherlock"
    assert state.themed_role_label_for("batman") == "Lead detective"
    # An unnamed agent still resolves to its Batman-base name under custom.
    assert state.themed_display_name_for("lucius") == BATMAN_BASE_NAMES["lucius"]
