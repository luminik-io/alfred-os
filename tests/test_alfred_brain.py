"""CLI tests for ``bin/alfred-brain.py``.

Exercises the subcommands end-to-end against a temporary SQLite
brain. We import the script via ``importlib.util`` because the file
name has a hyphen.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
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


@pytest.fixture(scope="module")
def github_poll_mod() -> ModuleType:
    repo = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo / "lib"))
    return _load("fleet_github_poll", repo / "bin" / "fleet-github-poll.py")


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
    assert "candidates  0 (0 open)" in out
    assert "failures    0" in out
    assert "github      0" in out
    assert "bundles     0" in out
    assert "workers     0 (0 running)" in out
    assert str(brain_db) in out


def test_cli_auto_promote_loads_persisted_env_before_opening_brain(
    cli_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    custom_db = runtime / "fleet-brain.db"
    home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text(
        f"ALFRED_FLEET_BRAIN_DB={custom_db}\nALFRED_AUTO_PROMOTE=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_FLEET_BRAIN_DB", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    built_db_paths: list[str | None] = []
    received_envs: list[dict[str, str] | None] = []

    class FakeBrain:
        def __init__(self, db_path: str | None = None) -> None:
            built_db_paths.append(db_path)

        @classmethod
        def from_env(cls, env: dict[str, str]):
            received_envs.append(env)
            return cls(db_path=env.get("ALFRED_FLEET_BRAIN_DB"))

        def auto_promote_candidates(
            self,
            *,
            threshold: float | None = None,
            max_per_run: int | None = None,
            env: dict[str, str] | None = None,
        ) -> dict[str, object]:
            received_envs.append(env)
            return {"enabled": False, "promoted": [], "considered": 0}

    monkeypatch.setattr(cli_mod, "FleetBrain", FakeBrain)

    rc = cli_mod.main(["auto-promote", "--json"])

    assert rc == 0
    assert built_db_paths == [str(custom_db)]
    assert received_envs[0] is not None
    assert received_envs[-1]["ALFRED_AUTO_PROMOTE"] == "0"
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_cli_auto_promote_uses_persisted_alfred_home_for_default_brain(
    cli_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text("ALFRED_AUTO_PROMOTE=0\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_FLEET_BRAIN_DB", raising=False)
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    built_db_paths: list[str | None] = []
    received_envs: list[dict[str, str] | None] = []

    class FakeBrain:
        def __init__(self, db_path: str | None = None) -> None:
            built_db_paths.append(db_path)

        @classmethod
        def from_env(cls, env: dict[str, str]):
            received_envs.append(env)
            return cls(db_path=str(Path(env["ALFRED_HOME"]) / "fleet-brain.db"))

        def auto_promote_candidates(
            self,
            *,
            threshold: float | None = None,
            max_per_run: int | None = None,
            env: dict[str, str] | None = None,
        ) -> dict[str, object]:
            received_envs.append(env)
            return {"enabled": False, "promoted": [], "considered": 0}

    monkeypatch.setattr(cli_mod, "FleetBrain", FakeBrain)

    rc = cli_mod.main(["auto-promote", "--json"])

    assert rc == 0
    assert built_db_paths == [str(runtime / "fleet-brain.db")]
    assert received_envs[-1] is not None
    assert received_envs[-1]["ALFRED_HOME"] == str(runtime)
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_cli_auto_promote_preserves_process_db_over_runtime_env(
    cli_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    stale_db = tmp_path / "stale.db"
    custom_db = runtime / "fleet-brain.db"
    home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text(
        f"ALFRED_FLEET_BRAIN_DB={custom_db}\nALFRED_AUTO_PROMOTE=0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.setenv("ALFRED_FLEET_BRAIN_DB", str(stale_db))
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    built_db_paths: list[str | None] = []

    class FakeBrain:
        def __init__(self, db_path: str | None = None) -> None:
            built_db_paths.append(db_path)

        @classmethod
        def from_env(cls, env: dict[str, str]):
            return cls(db_path=env.get("ALFRED_FLEET_BRAIN_DB"))

        def auto_promote_candidates(
            self,
            *,
            threshold: float | None = None,
            max_per_run: int | None = None,
            env: dict[str, str] | None = None,
        ) -> dict[str, object]:
            return {"enabled": False, "promoted": [], "considered": 0}

    monkeypatch.setattr(cli_mod, "FleetBrain", FakeBrain)

    rc = cli_mod.main(["auto-promote", "--json"])

    assert rc == 0
    assert built_db_paths == [str(stale_db)]
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_cli_auto_promote_applies_persisted_env_to_process_dependencies(
    cli_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    home.mkdir()
    runtime.mkdir()
    (runtime / ".env").write_text(
        "ALFRED_AUTO_PROMOTE=0\n"
        "ALFRED_REDIS_MEMORY_URL=http://memory.custom\n"
        "ALFRED_AMS_TOKEN=custom-token\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALFRED_HOME", str(runtime))
    monkeypatch.delenv("ALFRED_AUTO_PROMOTE", raising=False)
    monkeypatch.delenv("ALFRED_REDIS_MEMORY_URL", raising=False)
    monkeypatch.delenv("ALFRED_AMS_TOKEN", raising=False)

    class FakeBrain:
        @classmethod
        def from_env(cls, env: dict[str, str]):
            assert env["ALFRED_REDIS_MEMORY_URL"] == "http://memory.custom"
            return cls()

        def auto_promote_candidates(
            self,
            *,
            threshold: float | None = None,
            max_per_run: int | None = None,
            env: dict[str, str] | None = None,
        ) -> dict[str, object]:
            assert env is not None
            assert os.environ["ALFRED_REDIS_MEMORY_URL"] == "http://memory.custom"
            assert os.environ["ALFRED_AMS_TOKEN"] == "custom-token"
            return {"enabled": False, "promoted": [], "considered": 0}

    monkeypatch.setattr(cli_mod, "FleetBrain", FakeBrain)

    rc = cli_mod.main(["auto-promote", "--json"])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


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


def test_cli_candidate_promote_and_reject(
    cli_mod: ModuleType,
    brain_db: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Promotion writes the lesson to AMS first (no local fallback), so stub the
    # AMS transport: a CLI test must not require a live Redis AMS on :8088. The
    # provider builds the Lesson from its inputs, so any non-error response is a
    # successful write.
    monkeypatch.setattr(
        "memory.redis_agent_memory._default_transport",
        lambda method, url, payload, headers, timeout: {},
    )
    rc = cli_mod.main(
        [
            "propose",
            "lucius",
            "org/api",
            "Use the API fixture factory.",
            "--tag",
            "tests",
            "--confidence",
            "0.75",
            "--json",
        ]
    )
    assert rc == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["status"] == "candidate"
    assert candidate["confidence"] == 0.75

    rc = cli_mod.main(["promote", candidate["id"], "--reviewer", "alice", "--json"])
    assert rc == 0
    promoted = json.loads(capsys.readouterr().out)
    assert promoted["lesson_id"]

    cli_mod.main(["candidates", "--status", "validated", "--json"])
    candidates = json.loads(capsys.readouterr().out)
    assert candidates[0]["promoted_lesson_id"] == promoted["lesson_id"]

    cli_mod.main(["propose", "lucius", "org/api", "too vague", "--json"])
    rejected_candidate = json.loads(capsys.readouterr().out)
    rc = cli_mod.main(["reject", rejected_candidate["id"], "--note", "too vague", "--json"])
    assert rc == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert rejected["review_note"] == "too vague"


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


def test_cli_redis_status_json(
    cli_mod: ModuleType,
    brain_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRedis:
        def health(self) -> dict[str, object]:
            return {
                "ok": True,
                "base_url": "http://memory.local",
                "namespace": "alfred",
                "response": {"status": "healthy"},
            }

    monkeypatch.setattr(cli_mod, "_build_redis_provider", lambda: FakeRedis())

    rc = cli_mod.main(["redis-status", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["response"]["status"] == "healthy"


def test_cli_ams_status_json(
    cli_mod: ModuleType,
    brain_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRedis:
        def health(self) -> dict[str, object]:
            return {
                "ok": True,
                "base_url": "http://127.0.0.1:8088",
                "namespace": "alfred",
                "response": {"status": "healthy"},
            }

    monkeypatch.setattr(cli_mod, "_build_redis_provider", lambda: FakeRedis())
    monkeypatch.delenv("ALFRED_AMS_PORT", raising=False)

    rc = cli_mod.main(["ams-status", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_url"] == "http://127.0.0.1:8088"
    assert payload["embedding_model"] == "ollama/mxbai-embed-large"
    assert payload["health"]["ok"] is True


def test_cli_redis_sync_pushes_reviewed_lessons(
    cli_mod: ModuleType,
    brain_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_mod.main(["reflect", "lucius", "org/api", "sync me"])
    capsys.readouterr()
    synced: list[str] = []

    class FakeRedis:
        def sync_lesson(self, lesson) -> bool:  # type: ignore[no-untyped-def]
            synced.append(lesson.body)
            return True

    monkeypatch.setattr(cli_mod, "_build_redis_provider", lambda: FakeRedis())

    rc = cli_mod.main(["redis-sync", "--codename", "lucius", "--repo", "org/api", "--json"])

    assert rc == 0
    assert synced == ["sync me"]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"dry_run": False, "matched": 1, "synced": 1, "failed": []}


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


def test_cli_failures_and_doctor(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    brain.record_failure(
        codename="huntress",
        repo="org/web",
        firing_id="fid",
        subtype="error_timeout",
        summary="browser install missing",
        engine="claude",
    )
    capsys.readouterr()
    rc = cli_mod.main(["failures", "--json"])
    assert rc == 0
    failures = json.loads(capsys.readouterr().out)
    assert failures[0]["subtype"] == "error_timeout"

    rc = cli_mod.main(["doctor", "--json"])
    assert rc in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] in {"ok", "warn", "fail"}
    assert report["db"] == str(brain_db)


def test_cli_failure_patterns_and_governor(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    for idx in range(2):
        brain.record_failure(
            codename="huntress",
            repo="org/web",
            firing_id=f"fid-{idx}",
            subtype="error_timeout",
            summary="browserType.launch: Executable doesn't exist at chromium_headless_shell",
            engine="claude",
        )
    capsys.readouterr()

    rc = cli_mod.main(["failure-patterns", "--json"])
    assert rc == 0
    patterns = json.loads(capsys.readouterr().out)
    assert patterns[0]["classification"] == "local_setup"
    assert patterns[0]["suggested_action"] == "file_setup_issue"

    rc = cli_mod.main(["governor", "--json"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["actions"][0]["kind"] == "failure_pattern"


def test_cli_harvest_previews_and_applies_failure_memories(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    for idx in range(2):
        brain.record_failure(
            codename="huntress",
            repo="org/web",
            firing_id=f"fid-harvest-{idx}",
            subtype="error_timeout",
            summary="browserType.launch: Executable doesn't exist at chromium_headless_shell",
            engine="claude",
        )
    capsys.readouterr()

    rc = cli_mod.main(["harvest", "--json"])
    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["applied"] is False
    assert preview["proposals"][0]["status"] == "preview"
    assert "local setup" in preview["proposals"][0]["body"]
    assert brain.list_memory_candidates() == []

    rc = cli_mod.main(["harvest", "--apply", "--json"])
    assert rc == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["queued"] == 1
    assert applied["proposals"][0]["status"] == "queued"
    candidate = brain.list_memory_candidates()[0]
    assert candidate.source == "memory-harvest"
    assert "failure-pattern" in candidate.tags

    brain.record_failure(
        codename="huntress",
        repo="org/web",
        firing_id="fid-harvest-later",
        subtype="error_timeout",
        summary="browserType.launch: Executable doesn't exist at chromium_headless_shell",
        engine="claude",
    )

    rc = cli_mod.main(["harvest", "--apply", "--json"])
    assert rc == 0
    duplicate = json.loads(capsys.readouterr().out)
    assert duplicate["duplicates"] == 1
    assert duplicate["proposals"][0]["status"] == "duplicate"


def test_harvest_duplicate_detection_only_handles_sqlite_unique_errors(
    cli_mod: ModuleType,
) -> None:
    assert cli_mod._looks_like_duplicate_candidate(
        sqlite3.IntegrityError("UNIQUE constraint failed: memory_candidates.id")
    )
    assert not cli_mod._looks_like_duplicate_candidate(
        sqlite3.IntegrityError("NOT NULL constraint failed: memory_candidates.repo")
    )
    assert not cli_mod._looks_like_duplicate_candidate(RuntimeError("duplicate network response"))


def test_cli_workers_github_bundles_and_promotions(
    cli_mod: ModuleType, brain_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    candidate = brain.propose_memory(
        codename="lucius",
        repo="org/api",
        body="Prefer request fixtures.",
        tags=["tests"],
        evidence="Observed in recent PRs.",
        confidence=0.85,
    )
    brain.upsert_github_item(
        repo="org/api",
        number=42,
        kind="pr",
        state="open",
        title="feat: endpoint",
        labels=["agent:bundle:billing"],
    )
    brain.upsert_worker_heartbeat(codename="lucius", firing_id="fid-1", repo="org/api")
    capsys.readouterr()

    assert cli_mod.main(["promotions", "--json"]) == 0
    promotions = json.loads(capsys.readouterr().out)
    assert promotions[0]["candidate_id"] == candidate.id

    assert cli_mod.main(["github", "--json"]) == 0
    github_items = json.loads(capsys.readouterr().out)
    assert github_items[0]["bundle_slug"] == "billing"

    assert cli_mod.main(["bundles", "billing", "--json"]) == 0
    bundles = json.loads(capsys.readouterr().out)
    assert bundles[0]["repo"] == "org/api"

    assert cli_mod.main(["workers", "--json"]) == 0
    workers = json.loads(capsys.readouterr().out)
    assert workers[0]["firing_id"] == "fid-1"


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
                json.dumps(
                    {
                        "event": "memory_candidate",
                        "codename": "lucius",
                        "repo": "org/api",
                        "body": "candidate-from-outbox",
                        "tags": ["candidate"],
                    }
                ),
                json.dumps(
                    {
                        "event": "failure_event",
                        "codename": "lucius",
                        "repo": "org/api",
                        "firing_id": "fid-1",
                        "subtype": "error_timeout",
                        "summary": "timeout",
                        "engine": "claude",
                    }
                ),
                json.dumps(
                    {
                        "event": "github_item",
                        "repo": "org/api",
                        "number": 7,
                        "kind": "issue",
                        "state": "open",
                        "title": "bundle issue",
                        "labels": ["agent:bundle:billing"],
                    }
                ),
                json.dumps(
                    {
                        "event": "worker_heartbeat",
                        "codename": "lucius",
                        "firing_id": "fid-1",
                        "status": "running",
                        "repo": "org/api",
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
    assert brain.list_memory_candidates()[0].body == "candidate-from-outbox"
    assert brain.list_failures()[0].subtype == "error_timeout"
    assert brain.list_github_items()[0].number == 7
    assert brain.list_bundle_items(bundle_slug="billing")[0].number == 7
    assert brain.list_worker_heartbeats()[0].firing_id == "fid-1"

    # Re-running consumes nothing new (cursor advanced).
    rc = ingest_mod.main([])
    assert rc == 0
    out_after = brain.recall(codename="lucius", repo="org/api")
    assert len(out_after) == len(out)


def test_ingest_handles_missing_outbox(
    ingest_mod: ModuleType, brain_db: Path, tmp_path: Path
) -> None:
    # No outbox dir created - should exit 0 cleanly.
    rc = ingest_mod.main([])
    assert rc == 0


def test_github_poll_preserves_omitted_line_counts(github_poll_mod: ModuleType) -> None:
    assert github_poll_mod._optional_non_negative_int({}, "additions") is None
    assert github_poll_mod._optional_non_negative_int({"additions": None}, "additions") is None
    assert github_poll_mod._optional_non_negative_int({"additions": 0}, "additions") == 0
    assert github_poll_mod._optional_non_negative_int({"additions": "12"}, "additions") == 12
    assert github_poll_mod._optional_non_negative_int({"additions": -4}, "additions") == 0


def test_github_poll_records_issues_prs_and_bundles(
    github_poll_mod: ModuleType, brain_db: Path
) -> None:
    from subprocess import CompletedProcess

    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> CompletedProcess[str]:
        calls.append(cmd)
        if cmd[1] == "issue":
            payload = [
                {
                    "number": 10,
                    "title": "implement bundle",
                    "state": "OPEN",
                    "labels": [{"name": "agent:bundle:billing"}],
                    "updatedAt": "2026-05-26T12:00:00Z",
                    "closedAt": None,
                    "url": "https://github.com/org/api/issues/10",
                }
            ]
        else:
            payload = [
                {
                    "number": 11,
                    "title": "feat: bundle",
                    "state": "OPEN",
                    "labels": [{"name": "agent:bundle:billing"}],
                    "updatedAt": "2026-05-26T12:05:00Z",
                    "closedAt": None,
                    "mergedAt": None,
                    "url": "https://github.com/org/api/pull/11",
                    "headRefName": "lucius/11",
                    "baseRefName": "main",
                }
            ]
        return CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    from fleet_brain import FleetBrain

    brain = FleetBrain(db_path=brain_db)
    counts = github_poll_mod.poll_repos(["org/api"], brain=brain, runner=fake_runner)
    assert counts == {"repos": 1, "issues": 1, "prs": 1, "errors": 0}
    assert len(calls) == 2
    assert {item.kind for item in brain.list_github_items(bundle_slug="billing")} == {
        "issue",
        "pr",
    }
    assert len(brain.list_bundle_items(bundle_slug="billing")) == 2
