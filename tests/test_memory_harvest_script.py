from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from importlib import util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from fleet_brain import FleetBrain  # noqa: E402


def _load_script_module():
    spec = util.spec_from_file_location(
        "memory_harvest_script", REPO_ROOT / "bin" / "memory-harvest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_harvest(tmp_path: Path, db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "ALFRED_FLEET_BRAIN_DB": str(db),
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "memory-harvest.py"), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )


@pytest.mark.parametrize(
    ("script", "sentinel"),
    [
        ("memory-harvest.py", "[MEMORY-HARVEST-DOCTOR-OK]"),
        ("memory-auto-promote.py", "[MEMORY-AUTO-PROMOTE-DOCTOR-OK]"),
    ],
)
def test_memory_wrappers_doctor_mode_short_circuits(
    tmp_path: Path, script: str, sentinel: str
) -> None:
    db = tmp_path / "brain.db"
    env = {
        **os.environ,
        "ALFRED_DOCTOR": "1",
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "ALFRED_FLEET_BRAIN_DB": str(db),
    }
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / script), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert sentinel in result.stdout
    assert not db.exists()


def test_memory_harvest_queues_reviewable_candidates(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    brain = FleetBrain(db_path=db)
    now = datetime.now(UTC)
    for idx in range(2):
        brain.record_failure(
            codename="huntress",
            repo="org/web",
            firing_id=f"fid-{idx}",
            subtype="error_timeout",
            summary="browserType.launch: Executable does not exist",
            engine="claude",
            created_at=now - timedelta(minutes=idx),
        )

    result = _run_harvest(tmp_path, db, "--json", "--no-slack")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["queued"] == 1
    candidates = brain.list_memory_candidates(status="candidate")
    assert len(candidates) == 1
    assert candidates[0].source == "memory-harvest"
    assert "Seen at least 2 times as of harvest time." in candidates[0].body


def test_memory_harvest_preview_does_not_queue(tmp_path: Path) -> None:
    db = tmp_path / "brain.db"
    brain = FleetBrain(db_path=db)
    for idx in range(2):
        brain.record_failure(
            codename="lucius",
            repo="org/api",
            firing_id=f"fid-{idx}",
            subtype="error_timeout",
            summary="timed out waiting for engine",
            engine="claude",
        )

    result = _run_harvest(tmp_path, db, "--preview", "--json", "--no-slack")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["queued"] == 0
    assert len(payload["proposals"]) == 1
    assert brain.list_memory_candidates(status="candidate") == []


def test_memory_harvest_timeout_kills_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _load_script_module()
    pid_file = tmp_path / "slow-brain.pid"
    slow_brain = tmp_path / "slow-brain.py"
    slow_brain.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                "from pathlib import Path",
                "Path(os.environ['MEMORY_HARVEST_PID_FILE']).write_text(str(os.getpid()))",
                "time.sleep(30)",
            ]
        )
    )
    monkeypatch.setenv("MEMORY_HARVEST_PID_FILE", str(pid_file))
    monkeypatch.setattr(script, "_brain_script", lambda: slow_brain)

    args = script.build_parser().parse_args(["--timeout", "1", "--no-slack"])
    with pytest.raises(RuntimeError, match="timed out"):
        script._run_harvest(args)

    pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("timed-out memory harvest child was not reaped")


def test_slack_trigger_uses_rendered_queued_candidates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script_module()
    payload = {
        "applied": True,
        "queued": 3,
        "duplicates": 0,
        "proposals": [{"status": "duplicate", "candidate_id": "mem_existing"}],
    }
    posts: list[str] = []

    monkeypatch.setattr(script, "_run_harvest", lambda _args: payload)
    monkeypatch.setattr(
        script,
        "_post_slack",
        lambda message, *, severity="info": posts.append(message) or True,
    )

    assert script.main([]) == 0
    assert posts == []
    assert "queued=0" in capsys.readouterr().out
