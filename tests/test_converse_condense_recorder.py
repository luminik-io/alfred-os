"""Server wiring for persisting condensation records.

The converse endpoints hand ``run_turn`` an ``on_condense`` callback built by
``views._converse_condense_recorder``. This proves that callback writes an
auditable record under ``<state>/condensations`` keyed by the draft slug, and
that a disk failure is swallowed so a persistence hiccup never fails the user's
converse turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
for sub in ("lib", "lib/server"):
    candidate = REPO_ROOT / sub
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import conversation_condenser as condenser  # noqa: E402
from server import views  # noqa: E402


def _request(state_root: Path) -> SimpleNamespace:
    """A minimal stand-in for the Starlette request the recorder reads.

    ``_state_root`` only touches ``request.app.state.reader.state_root``, so a
    namespace tree with that single attribute is enough to exercise the wiring
    without spinning a full app.
    """
    reader = SimpleNamespace(state_root=state_root)
    app = SimpleNamespace(state=SimpleNamespace(reader=reader))
    return SimpleNamespace(app=app)


def _record() -> condenser.CondensationRecord:
    return condenser.CondensationRecord(
        summary="condensed summary of older turns",
        summarized_indices=(1, 2, 3),
        kept_first=1,
        kept_last=2,
        original_turn_count=12,
        condensed_turn_count=4,
        reason="proactive",
    )


def test_recorder_persists_record_under_state_condensations(tmp_path: Path) -> None:
    request = _request(tmp_path)
    on_condense = views._converse_condense_recorder(request, draft_id="dark-mode")

    on_condense(_record())

    records_dir = tmp_path / "condensations"
    written = list(records_dir.glob("*.json"))
    assert len(written) == 1
    name = written[0].name
    # The slug is folded into the filename so an operator can find a draft's
    # condensations by name.
    assert "dark-mode" in name

    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["reason"] == "proactive"
    assert data["summarized_indices"] == [1, 2, 3]
    assert data["original_turn_count"] == 12


def test_recorder_without_draft_id_still_persists(tmp_path: Path) -> None:
    request = _request(tmp_path)
    on_condense = views._converse_condense_recorder(request, draft_id=None)

    on_condense(_record())

    written = list((tmp_path / "condensations").glob("*.json"))
    assert len(written) == 1


def test_recorder_swallows_persistence_failure(tmp_path: Path, monkeypatch) -> None:
    # A disk failure inside persistence must never propagate out of the
    # callback, or it would fail the user's converse turn.
    request = _request(tmp_path)
    on_condense = views._converse_condense_recorder(request, draft_id="x")

    def _boom(*_args: object, **_kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(condenser, "persist_record", _boom)

    # Must not raise.
    on_condense(_record())
