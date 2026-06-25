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

from roster_theme_store import (  # noqa: E402
    DEFAULT_THEME_ID,
    RosterThemeError,
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
    # Saving a preset wipes any name map, so display_name_for is always None.
    store.save(theme="custom", custom_names={"batman": "Sherlock"})
    store.save(theme="batman")
    loaded = _store(tmp_path).load()
    assert loaded.theme == "batman"
    assert loaded.display_name_for("batman") is None
    assert dict(loaded.custom_names) == {}


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
    (root / "roster-theme.json").write_text(
        json.dumps({"theme": "garbage"}), encoding="utf-8"
    )
    assert RosterThemeStore.from_state_root(tmp_path / "state").load().theme == "batman"
