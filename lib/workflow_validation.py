"""Validate changed GitHub Actions workflows before agent push."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

RunCmd = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class WorkflowValidationResult:
    """Result of pre-push workflow validation."""

    ok: bool
    files: tuple[str, ...] = ()
    reason: str = ""
    stdout: str = ""
    stderr: str = ""


def _is_workflow_path(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.rsplit(".", 1)[-1].lower() in {
        "yml",
        "yaml",
    }


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    run_cmd: RunCmd,
) -> subprocess.CompletedProcess:
    cmd = list(args)
    try:
        return run_cmd(
            cmd,
            cwd=str(cwd),
            timeout=timeout,
            text=True,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = f"command timed out after {exc.timeout}s: {' '.join(cmd)}"
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout=exc.stdout or "",
            stderr=stderr,
        )
    except OSError as exc:
        stderr = f"{exc.__class__.__name__}: {exc}"
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=127,
            stdout="",
            stderr=stderr,
        )


def changed_workflow_files(
    worktree: Path,
    *,
    base: str = "origin/main",
    run_cmd: RunCmd = subprocess.run,
) -> tuple[str, ...]:
    """Return changed workflow YAML files in ``worktree``."""
    if not worktree.exists():
        return ()
    commands = (
        ("git", "diff", "--name-only", f"{base}..HEAD"),
        ("git", "diff", "--name-only", "--cached"),
        ("git", "diff", "--name-only"),
    )
    changed: set[str] = set()
    for cmd in commands:
        res = _run(cmd, cwd=worktree, timeout=10, run_cmd=run_cmd)
        if res.returncode != 0:
            continue
        for line in (res.stdout or "").splitlines():
            path = line.strip()
            if _is_workflow_path(path):
                changed.add(path)
    return tuple(sorted(changed))


def validate_changed_workflows(
    worktree: Path,
    *,
    base: str = "origin/main",
    actionlint_bin: str | None = None,
    run_cmd: RunCmd = subprocess.run,
) -> WorkflowValidationResult:
    """Run actionlint when workflow YAML changed."""
    files = changed_workflow_files(worktree, base=base, run_cmd=run_cmd)
    if not files:
        return WorkflowValidationResult(ok=True)

    actionlint = actionlint_bin or shutil.which("actionlint")
    if not actionlint:
        return WorkflowValidationResult(
            ok=False,
            files=files,
            reason="actionlint missing",
        )

    res = _run([actionlint, *files], cwd=worktree, timeout=30, run_cmd=run_cmd)
    if res.returncode != 0:
        return WorkflowValidationResult(
            ok=False,
            files=files,
            reason="actionlint failed",
            stdout=res.stdout or "",
            stderr=res.stderr or "",
        )
    return WorkflowValidationResult(
        ok=True,
        files=files,
        stdout=res.stdout or "",
        stderr=res.stderr or "",
    )
