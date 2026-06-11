from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALFRED = ROOT / "bin" / "alfred"
SCHEDULE = ROOT / "bin" / "alfred-schedule.py"


def _load_schedule_module():
    spec = importlib.util.spec_from_file_location("alfred_schedule", SCHEDULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["alfred_schedule"] = module
    spec.loader.exec_module(module)
    return module


def _write_conf(repo: Path) -> Path:
    conf = repo / "launchd" / "agents.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "\n".join(
            [
                "# test agents.conf",
                "alfred.lucius\tlucius.py\tinterval:600\tyes\t\topus\tEngineer",
                "alfred.batman\tbatman.py\tinterval:5400\tyes\t\topus\tCoordinator",
                "alfred.gordon\tgordon.py\tcron:8:00\tno\t\t\tWatch",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return conf


def test_canonical_schedule_accepts_human_shortcuts() -> None:
    sched = _load_schedule_module()

    assert sched.canonical_schedule("10m") == "interval:600"
    assert sched.canonical_schedule("every 2h") == "interval:7200"
    assert sched.canonical_schedule("daily@09:05") == "cron:9:05"
    assert sched.canonical_schedule("weekly@mon:09:05") == "cron:1:9:05"
    assert sched.canonical_schedule("cron:0:22:00") == "cron:0:22:00"


@pytest.mark.parametrize(
    "value",
    ["", "interval:0", "cron:24:00", "weekly@nope:09:00", "tomorrow"],
)
def test_canonical_schedule_rejects_invalid_values(value: str) -> None:
    sched = _load_schedule_module()

    with pytest.raises(sched.ScheduleError):
        sched.canonical_schedule(value)


def test_update_schedule_dry_run_does_not_write(tmp_path: Path) -> None:
    sched = _load_schedule_module()
    conf = _write_conf(tmp_path)
    before = conf.read_text(encoding="utf-8")

    result = sched.update_schedule(conf, "lucius", "20m", dry_run=True)

    assert result["oldSchedule"] == "interval:600"
    assert result["newSchedule"] == "interval:1200"
    assert result["dryRun"] is True
    assert conf.read_text(encoding="utf-8") == before


def test_update_schedule_writes_only_target_row(tmp_path: Path) -> None:
    sched = _load_schedule_module()
    conf = _write_conf(tmp_path)

    result = sched.update_schedule(conf, "lucius", "20m")

    assert result["changed"] is True
    text = conf.read_text(encoding="utf-8")
    assert "alfred.lucius\tlucius.py\tinterval:1200" in text
    assert "alfred.batman\tbatman.py\tinterval:5400" in text
    assert "alfred.gordon\tgordon.py\tcron:8:00" in text


def test_agents_conf_path_prefers_renamed_alfred_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sched = _load_schedule_module()
    workspace = tmp_path / "workspace"
    public_repo = workspace / "alfred-os"
    legacy_repo = workspace / "product" / "alfred"
    expected = _write_conf(public_repo)
    _write_conf(legacy_repo)

    monkeypatch.delenv("ALFRED_REPO", raising=False)
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.chdir(tmp_path)

    assert sched.agents_conf_path() == expected


def test_unified_alfred_schedule_dispatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    conf = _write_conf(repo)
    hermes = tmp_path / "hermes"
    bin_dir = hermes / "bin"
    bin_dir.mkdir(parents=True)
    target = bin_dir / "alfred-schedule.py"
    target.write_text(SCHEDULE.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)

    env = os.environ.copy()
    env["ALFRED_HOME"] = str(hermes)
    env["ALFRED_REPO"] = str(repo)

    res = subprocess.run(
        [sys.executable, str(ALFRED), "schedule", "set", "lucius", "30m", "--dry-run"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert res.returncode == 0, res.stderr
    assert "would update lucius interval:600 -> interval:1800" in res.stdout
    assert "interval:600" in conf.read_text(encoding="utf-8")


def test_unified_alfred_assign_dispatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_conf(repo)
    hermes = tmp_path / "hermes"

    env = os.environ.copy()
    env["ALFRED_HOME"] = str(hermes)
    env["ALFRED_REPO"] = str(repo)

    res = subprocess.run(
        [sys.executable, str(ALFRED), "assign", "not-an-issue"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert res.returncode == 2
    assert "expected a GitHub issue URL or owner/repo#123" in res.stderr
