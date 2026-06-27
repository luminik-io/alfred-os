"""Tests for ``alfred labels`` operator commands."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin" / "alfred"
LIB = REPO_ROOT / "lib"
sys.path.insert(0, str(LIB))


@pytest.fixture()
def cli_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / ".alfred"))
    monkeypatch.setenv("GH_ORG", "acme")
    loader = SourceFileLoader("alfred_cli_labels", str(BIN))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_cli_labels"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_label_catalogue_includes_lifecycle_batman_and_operator_labels(cli_module) -> None:
    names = {name for name, _, _ in cli_module._label_bootstrap_catalog()}
    assert "agent:implement" in names
    assert "agent:large-feature" in names
    assert "agent:authored" in names
    assert "agent:plan-pending-approval" in names
    assert "do-not-merge" in names


def test_labels_check_reports_missing_without_creating(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "label", "list"]:
            existing = [{"name": "agent:implement", "color": "0e8a16", "description": ""}]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(existing), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    rc = cli_module.main(["labels", "bootstrap", "your-backend", "--check"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "labels check on acme/your-backend" in out
    assert "agent:in-flight (MISSING)" in out
    assert all(cmd[:3] == ["gh", "label", "list"] for cmd in calls)


def test_labels_bootstrap_creates_missing_labels(
    cli_module, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "label", "create"]:
            created.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)
    rc = cli_module.main(["labels", "bootstrap", "your-backend"])

    assert rc == 0
    assert "agent:implement" in created
    assert "agent:large-feature" in created
    assert "do-not-merge" in created
    assert "labels bootstrap on acme/your-backend" in capsys.readouterr().out


def test_labels_all_reads_fleet_repo_env(cli_module, monkeypatch: pytest.MonkeyPatch) -> None:
    repos: list[str] = []
    monkeypatch.setenv("ALFRED_LUCIUS_REPOS", "api,web")
    monkeypatch.setenv("ALFRED_RASALGHUL_REPOS", "web,mobile")
    monkeypatch.setattr(
        cli_module,
        "_labels_bootstrap_one",
        lambda repo, *, check, force: repos.append(repo) or 0,
    )

    assert cli_module.main(["labels", "check", "--all"]) == 0
    assert repos == ["api", "web", "mobile"]


def test_clear_lock_clears_dead_lock(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    posts: list[str] = []
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])
    monkeypatch.setattr(
        cli_module, "_clear_lock_scheduler_health", lambda agent: "scheduler: loaded"
    )
    monkeypatch.setattr(
        cli_module.agent_runner, "slack_post", lambda text: posts.append(text) or True
    )

    assert cli_module.main(["clear-lock", "lucius"]) == 0
    assert not lock_dir.exists()
    out = capsys.readouterr().out
    assert "cleared" in out
    assert "scheduler: loaded" in out
    assert posts == [f"alfred clear-lock: cleared lucius lock at {lock_dir}; scheduler: loaded"]


def test_clear_lock_quiet_skips_slack_post(
    cli_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])
    monkeypatch.setattr(
        cli_module, "_clear_lock_scheduler_health", lambda agent: "scheduler: loaded"
    )
    monkeypatch.setattr(
        cli_module.agent_runner,
        "slack_post",
        lambda text: (_ for _ in ()).throw(AssertionError("unexpected Slack post")),
    )

    assert cli_module.main(["clear-lock", "lucius", "--quiet"]) == 0
    assert not lock_dir.exists()


def test_clear_lock_refuses_live_matching_holder(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, True, True))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    assert "refusing to clear" in capsys.readouterr().out


def test_clear_lock_refuses_unknown_holder(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (None, False, None))
    monkeypatch.setattr(cli_module, "_matching_worktree_risks", lambda agent, **kw: [])

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    out = capsys.readouterr().out
    assert "pid is unknown" in out
    assert "refusing to clear" in out


def test_clear_lock_refuses_matching_unpushed_worktree(
    cli_module, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_dir = tmp_path / "agent-lock-lucius"
    lock_dir.mkdir()
    monkeypatch.setattr(cli_module, "_lock_dir_for_agent", lambda agent: lock_dir)
    monkeypatch.setattr(cli_module, "_describe_lock", lambda lock, agent: (12345, False, None))
    monkeypatch.setattr(
        cli_module,
        "_matching_worktree_risks",
        lambda agent, **kw: ["/tmp/wt-lucius (lucius/42, ahead of remote)"],
    )

    assert cli_module.main(["clear-lock", "lucius"]) == 1
    assert lock_dir.exists()
    out = capsys.readouterr().out
    assert "worktree risk" in out
    assert "refusing to clear" in out


def test_brain_command_forwards_to_brain_cli(cli_module, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["brain", "lessons", "lucius", "org/api"]) == 0
    assert calls == [
        [
            sys.executable,
            str(REPO_ROOT / "bin" / "alfred-brain.py"),
            "lessons",
            "lucius",
            "org/api",
        ]
    ]


def test_code_memory_command_forwards_to_launcher(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["code-memory", "doctor"]) == 0
    assert calls == [[str(REPO_ROOT / "bin" / "code-memory-mcp"), "doctor"]]


def test_code_memory_command_defaults_to_doctor(
    cli_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    assert cli_module.main(["code-memory"]) == 0
    assert calls == [[str(REPO_ROOT / "bin" / "code-memory-mcp"), "doctor"]]
