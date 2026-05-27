"""Tests for ``alfred serve`` v1.

Covers:

* Empty state: missing state root renders a clean empty fleet view.
* Populated state via ``tmp_path``: events JSONL is parsed and surfaced
  on /, /firings, and /firings/<id>.
* 404 for unknown firing id.
* Filter by codename on /firings.
* HTMX partial swap on /.
* Path-traversal rejection on the firing id.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Skip the entire module when the `serve` extra is not installed. CI runs
# default `pip install -e .` which does not pull fastapi/uvicorn/jinja2;
# those tests only make sense when the operator has chosen to install
# the serve dashboard. Pytest's importorskip skips with a clear marker
# rather than letting a collection-time ImportError crash the suite.
pytest.importorskip("fastapi")

# lib/ is not a package on install yet, add it to sys.path explicitly.
REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from fastapi.testclient import TestClient  # noqa: E402
from server import FilesystemReader, create_app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def empty_state(tmp_path: Path) -> Path:
    """An ``$ALFRED_HOME/state`` directory that exists but is empty."""
    state = tmp_path / "state"
    state.mkdir()
    return state


@pytest.fixture()
def populated_state(tmp_path: Path) -> Path:
    """A state tree with two codenames, three firings, mixed statuses."""
    state = tmp_path / "state"
    (state / "lucius" / "events").mkdir(parents=True)
    (state / "drake" / "events").mkdir(parents=True)
    # Also seed a reserved subdir so we can confirm it is not enumerated
    # as a codename.
    (state / "transcripts").mkdir()

    _write_jsonl(
        state / "lucius" / "events" / "2026-05-23-1200-aa.jsonl",
        [
            {"ts": "2026-05-23T12:00:00Z", "event": "firing_started", "agent": "lucius"},
            {
                "ts": "2026-05-23T12:01:30Z",
                "event": "issue_picked",
                "repo": "your-org/api",
                "number": 42,
            },
            {
                "ts": "2026-05-23T12:05:00Z",
                "event": "firing_ended",
                "agent": "lucius",
                "result": "pr_opened",
            },
        ],
    )
    _write_jsonl(
        state / "lucius" / "events" / "2026-05-22-0900-bb.jsonl",
        [
            {"ts": "2026-05-22T09:00:00Z", "event": "firing_started", "agent": "lucius"},
            {"ts": "2026-05-22T09:00:45Z", "event": "firing_failed", "reason": "rate_limit"},
        ],
    )
    _write_jsonl(
        state / "drake" / "events" / "2026-05-23-1100-cc.jsonl",
        [
            {"ts": "2026-05-23T11:00:00Z", "event": "firing_started", "agent": "drake"},
            {"ts": "2026-05-23T11:02:00Z", "event": "firing_ended", "agent": "drake"},
        ],
    )
    return state


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _client(state_root: Path) -> TestClient:
    return TestClient(create_app(FilesystemReader(state_root=state_root)))


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


def test_empty_state_root_does_not_exist(tmp_path: Path) -> None:
    """Reader must tolerate a totally missing state root."""
    client = _client(tmp_path / "does-not-exist")
    response = client.get("/")
    assert response.status_code == 200
    assert "No codenames found" in response.text


def test_empty_state_root_empty_dir(empty_state: Path) -> None:
    client = _client(empty_state)
    response = client.get("/")
    assert response.status_code == 200
    assert "No codenames found" in response.text

    response = client.get("/firings")
    assert response.status_code == 200
    assert "No firings to show" in response.text


# ---------------------------------------------------------------------------
# Populated state
# ---------------------------------------------------------------------------


def test_fleet_view_lists_codenames(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/")
    assert response.status_code == 200
    assert "lucius" in response.text
    assert "drake" in response.text
    # Reserved subdir must not be enumerated as a codename.
    assert "transcripts</strong>" not in response.text


def test_fleet_view_htmx_partial(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "fleet-table" in response.text
    assert "<header" not in response.text  # partial, not full shell


def test_fleet_view_htmx_partial_skips_reliability_report(populated_state: Path) -> None:
    class CountingReader(FilesystemReader):
        reliability_calls = 0

        def reliability_report(self) -> dict[str, object]:
            self.reliability_calls += 1
            return {
                "status": "ok",
                "actions": [],
                "failure_patterns": [],
                "stale_workers": [],
                "promotion_suggestions": [],
            }

    reader = CountingReader(state_root=populated_state)
    client = TestClient(create_app(reader))

    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "fleet-table" in response.text
    assert reader.reliability_calls == 0

    response = client.get("/")
    assert response.status_code == 200
    assert reader.reliability_calls == 1


def test_firings_view_lists_recent(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings")
    assert response.status_code == 200
    assert "2026-05-23-1200-aa" in response.text
    assert "2026-05-22-0900-bb" in response.text
    assert "2026-05-23-1100-cc" in response.text


def test_firings_view_filter_by_codename(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings", params={"codename": "drake"})
    assert response.status_code == 200
    assert "2026-05-23-1100-cc" in response.text
    assert "2026-05-23-1200-aa" not in response.text


def test_firing_detail_renders_events(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/2026-05-23-1200-aa")
    assert response.status_code == 200
    assert "issue_picked" in response.text
    assert "your-org/api" in response.text
    assert "firing_ended" in response.text


def test_firing_detail_failed_marks_error(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/2026-05-22-0900-bb")
    assert response.status_code == 200
    assert "dot-error" in response.text
    assert "rate_limit" in response.text


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_firing_returns_404(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/firings/does-not-exist")
    assert response.status_code == 404
    assert "not located" in response.text


def test_firing_id_rejects_path_traversal(populated_state: Path) -> None:
    """Operator-supplied firing id must not be able to read arbitrary files."""
    _client(populated_state)
    # Hitting "%2E%2E%2F" decoded equals "../"  FastAPI normalizes path
    # params so the safest assertion is that any explicit traversal char
    # is rejected by the reader.
    reader = FilesystemReader(state_root=populated_state)
    assert reader.get_firing("../etc/passwd") is None
    assert reader.get_firing(".hidden") is None
    assert reader.get_firing("a\\b") is None


def test_healthz(populated_state: Path) -> None:
    client = _client(populated_state)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def test_reliability_report_missing_brain_db_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "missing-fleet-brain.db"
    monkeypatch.setenv("ALFRED_FLEET_BRAIN_DB", str(db_path))

    report = FilesystemReader(state_root=tmp_path / "state").reliability_report()

    assert report["status"] == "unknown"
    assert "not initialized" in report["error"]
    assert not db_path.exists()


def test_malformed_jsonl_does_not_crash(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "lucius" / "events").mkdir(parents=True)
    path = state / "lucius" / "events" / "2026-05-23-1300-zz.jsonl"
    path.write_text(
        '{"ts":"2026-05-23T13:00:00Z","event":"firing_started"}\n'
        "this is not json\n"
        '{"ts":"2026-05-23T13:01:00Z","event":"firing_ended"}\n',
        encoding="utf-8",
    )
    client = _client(state)
    response = client.get("/firings/2026-05-23-1300-zz")
    assert response.status_code == 200
    # The good lines survive; the bad one is dropped silently.
    assert "firing_started" in response.text
    assert "firing_ended" in response.text
