"""CLI tests for ``bin/alfred-brain.py``.

Exercises the subcommands end-to-end against a temporary SQLite
brain. We import the script via ``importlib.util`` because the file
name has a hyphen.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC
from pathlib import Path
from types import ModuleType

import pytest


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cli_mod() -> ModuleType:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    return _load("alfred_brain", repo / "bin" / "alfred-brain.py")


@pytest.fixture(scope="module")
def ingest_mod() -> ModuleType:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    return _load("fleet_ingest", repo / "bin" / "fleet-ingest.py")


@pytest.fixture()
def brain_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "brain.db"
    monkeypatch.setenv("ALFRED_FLEET_BRAIN_DB", str(db))
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred-home"))
    return db


# ---------------------------------------------------------------------------
# alfred-brain.py
# ---------------------------------------------------------------------------


def test_cli_status_empty(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_mod.main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lessons     0" in out
    assert "file_touches 0" in out
    assert str(brain_db) in out


def test_cli_reflect_then_lessons(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_mod.main(
        [
            "reflect",
            "lucius",
            "your-org/api",
            "GraphQL schema lives under src/schema.graphql",
            "--tag",
            "graphql",
            "--tag",
            "layout",
            "--severity",
            "warning",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "reflected lesson" in out

    rc = cli_mod.main(["lessons", "lucius", "your-org/api"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "GraphQL schema lives under src/schema.graphql" in out
    assert "(warning)" in out
    assert "graphql" in out


def test_cli_lessons_json(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_mod.main(["reflect", "lucius", "org/api", "first lesson"])
    capsys.readouterr()  # drain
    cli_mod.main(["lessons", "lucius", "org/api", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["body"] == "first lesson"
    assert payload[0]["codename"] == "lucius"


def test_cli_lessons_wildcard_codename(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_mod.main(["reflect", "lucius", "org/api", "alpha"])
    cli_mod.main(["reflect", "drake", "org/api", "beta"])
    capsys.readouterr()
    cli_mod.main(["lessons", "-", "org/api"])
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out


def test_cli_forget(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli_mod.main(["reflect", "lucius", "org/api", "to be forgotten"])
    capsys.readouterr()
    cli_mod.main(["lessons", "lucius", "org/api", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload
    lesson_id = payload[0]["id"]
    rc = cli_mod.main(["forget", lesson_id])
    assert rc == 0
    capsys.readouterr()
    cli_mod.main(["lessons", "lucius", "org/api", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload == []


def test_cli_forget_before(
    cli_mod: ModuleType, brain_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timedelta

    # Backdate one lesson via the public API.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    brain.reflect(
        codename="lucius",
        repo="org/api",
        body="ancient",
        created_at=datetime.now(UTC) - timedelta(days=60),
    )
    brain.reflect(codename="lucius", repo="org/api", body="recent")
    rc = cli_mod.main(["forget", "--before", "30d"])
    assert rc == 0
    remaining = brain.recall(codename="lucius", repo="org/api")
    assert [L.body for L in remaining] == ["recent"]


def test_cli_export_writes_file(cli_mod: ModuleType, brain_db: Path, tmp_path: Path) -> None:
    cli_mod.main(["reflect", "lucius", "org/api", "alpha"])
    target = tmp_path / "snapshot.json"
    rc = cli_mod.main(["export", "--out", str(target)])
    assert rc == 0
    payload = json.loads(target.read_text())
    assert payload["lessons"][0]["body"] == "alpha"


def test_cli_firings(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    brain.firing_log(
        firing_id="01HZA",
        codename="lucius",
        status="ok",
        summary="opened PR #42",
        repo="org/api",
    )
    capsys.readouterr()
    rc = cli_mod.main(["firings"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "01HZA" in out
    assert "status=ok" in out


def test_cli_files(cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    brain.record_file_touch(
        repo="org/api",
        path="src/api.py",
        codename="lucius",
        firing_id="fid",
        pr_url="https://github.com/org/api/pull/42",
    )
    capsys.readouterr()
    rc = cli_mod.main(["files", "org/api", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["path"] == "src/api.py"
    assert payload[0]["codename"] == "lucius"


# ---------------------------------------------------------------------------
# fleet-ingest.py
# ---------------------------------------------------------------------------


def test_ingest_drains_outbox(
    ingest_mod: ModuleType,
    cli_mod: ModuleType,
    brain_db: Path,
    tmp_path: Path,
) -> None:
    outbox = tmp_path / "alfred-home" / "state" / "memory-outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    target = outbox / "lucius.jsonl"
    target.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "reflect",
                        "codename": "lucius",
                        "repo": "org/api",
                        "body": "from-outbox",
                        "tags": ["ingest"],
                    }
                ),
                json.dumps(
                    {
                        "event": "firing_log",
                        "firing_id": "fid-1",
                        "codename": "lucius",
                        "status": "ok",
                        "summary": "done",
                        "repo": "org/api",
                        "files_touched": [{"path": "src/embedded.py", "change_type": "modified"}],
                    }
                ),
                json.dumps(
                    {
                        "event": "note_repo",
                        "repo": "org/api",
                        "body": "running summary",
                    }
                ),
                json.dumps(
                    {
                        "event": "file_touch",
                        "repo": "org/api",
                        "path": "src/api.py",
                        "codename": "lucius",
                        "firing_id": "fid-1",
                        "change_type": "added",
                    }
                ),
                "",  # blank line
                "{not-json",
                json.dumps({"event": "unknown_kind", "x": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = ingest_mod.main([])
    assert rc == 0

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    out = brain.recall(codename="lucius", repo="org/api")
    assert any(L.body == "from-outbox" for L in out)
    assert brain.list_firings(codename="lucius")[0].firing_id == "fid-1"
    files = brain.list_file_touches(repo="org/api", codename="lucius")
    assert {T.path for T in files} == {"src/api.py", "src/embedded.py"}
    assert brain.get_repo_note("org/api") is not None

    # Re-running consumes nothing new (cursor advanced).
    rc = ingest_mod.main([])
    assert rc == 0
    out_after = brain.recall(codename="lucius", repo="org/api")
    assert len(out_after) == len(out)


def test_ingest_handles_missing_outbox(
    ingest_mod: ModuleType, brain_db: Path, tmp_path: Path
) -> None:
    # No outbox dir created — should exit 0 cleanly.
    rc = ingest_mod.main([])
    assert rc == 0
