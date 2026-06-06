from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import workflow_validation as workflow_validation_module  # noqa: E402
from workflow_validation import changed_workflow_files, validate_changed_workflows  # noqa: E402


def test_changed_workflow_files_detects_unstaged_yaml(tmp_path):
    worktree = tmp_path / "repo"
    workflows = worktree / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "ci.yml"
    workflow.write_text("name: CI\n", encoding="utf-8")

    def fake_run(cmd, **_kwargs):
        assert cmd[0:3] == ["git", "diff", "--name-only"]
        return subprocess.CompletedProcess(cmd, 0, stdout=".github/workflows/ci.yml\nREADME.md\n")

    assert changed_workflow_files(worktree, run_cmd=fake_run) == (".github/workflows/ci.yml",)


def test_changed_workflow_files_returns_empty_for_missing_worktree(tmp_path):
    assert changed_workflow_files(tmp_path / "missing") == ()


def test_validate_changed_workflows_passes_when_no_workflows(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()

    def fake_run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    result = validate_changed_workflows(worktree, run_cmd=fake_run)

    assert result.files == ()
    assert result.ok is True
    assert result.reason == ""


def test_validate_changed_workflows_fails_closed_when_actionlint_missing(tmp_path, monkeypatch):
    worktree = tmp_path / "repo"
    (worktree / ".github" / "workflows").mkdir(parents=True)

    def fake_run(cmd, **_kwargs):
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=".github/workflows/ci.yml\n")
        raise FileNotFoundError("actionlint")

    monkeypatch.setattr(workflow_validation_module.shutil, "which", lambda _binary: None)

    result = validate_changed_workflows(worktree, run_cmd=fake_run)

    assert result.ok is False
    assert result.files == (".github/workflows/ci.yml",)
    assert result.reason == "actionlint missing"


def test_validate_changed_workflows_passes_with_actionlint(tmp_path):
    worktree = tmp_path / "repo"
    (worktree / ".github" / "workflows").mkdir(parents=True)
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=".github/workflows/ci.yml\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = validate_changed_workflows(
        worktree,
        actionlint_bin="actionlint",
        run_cmd=fake_run,
    )

    assert result.ok is True
    assert result.reason == ""
    assert calls[-1][0] == "actionlint"


def test_validate_changed_workflows_reports_actionlint_failure(tmp_path):
    worktree = tmp_path / "repo"
    (worktree / ".github" / "workflows").mkdir(parents=True)

    def fake_run(cmd, **_kwargs):
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=".github/workflows/ci.yml\n")
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="bad workflow")

    result = validate_changed_workflows(
        worktree,
        actionlint_bin="actionlint",
        run_cmd=fake_run,
    )

    assert result.ok is False
    assert result.reason == "actionlint failed"
    assert result.stderr == "bad workflow"


def test_validate_changed_workflows_reports_actionlint_timeout(tmp_path):
    worktree = tmp_path / "repo"
    (worktree / ".github" / "workflows").mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=".github/workflows/ci.yml\n")
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    result = validate_changed_workflows(
        worktree,
        actionlint_bin="actionlint",
        run_cmd=fake_run,
    )

    assert result.ok is False
    assert result.reason == "actionlint failed"
    assert "timed out" in result.stderr
