"""Validate changed GitHub Actions workflows before agent push."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

RunCmd = Callable[..., subprocess.CompletedProcess]
DEFAULT_WORKFLOW_BASE = "origin/main"


@dataclass(frozen=True)
class WorkflowValidationResult:
    """Result of pre-push workflow validation."""

    ok: bool
    files: tuple[str, ...] = ()
    reason: str = ""
    stdout: str = ""
    stderr: str = ""


class WorkflowDiffFailed(RuntimeError):
    """Raised when the committed workflow diff cannot be computed."""

    def __init__(self, command: Sequence[str], stdout: str, stderr: str) -> None:
        super().__init__("workflow diff failed")
        self.command = tuple(command)
        self.stdout = stdout
        self.stderr = stderr


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


def _remote_default_ref(worktree: Path, *, run_cmd: RunCmd) -> str:
    res = _run(
        ("git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"),
        cwd=worktree,
        timeout=10,
        run_cmd=run_cmd,
    )
    ref = (res.stdout or "").strip()
    if res.returncode == 0 and ref.startswith("origin/"):
        return ref
    return "origin/main"


def changed_workflow_files(
    worktree: Path,
    *,
    base: str | None = None,
    fail_on_base_error: bool = False,
    run_cmd: RunCmd = subprocess.run,
) -> tuple[str, ...]:
    """Return changed workflow YAML files in ``worktree``."""
    if not worktree.exists():
        return ()
    comparison_base = base or _remote_default_ref(worktree, run_cmd=run_cmd)
    commands = (
        ("git", "diff", "--name-only", "--diff-filter=ACMRTUXB", f"{comparison_base}...HEAD"),
        ("git", "diff", "--name-only", "--diff-filter=ACMRTUXB", "--cached"),
        ("git", "diff", "--name-only", "--diff-filter=ACMRTUXB"),
    )
    changed: set[str] = set()
    for index, cmd in enumerate(commands):
        res = _run(cmd, cwd=worktree, timeout=10, run_cmd=run_cmd)
        if res.returncode != 0:
            if index == 0 and fail_on_base_error:
                raise WorkflowDiffFailed(cmd, res.stdout or "", res.stderr or "")
            continue
        for line in (res.stdout or "").splitlines():
            path = line.strip()
            if _is_workflow_path(path) and (worktree / path).is_file():
                changed.add(path)
    return tuple(sorted(changed))


def validate_changed_workflows(
    worktree: Path,
    *,
    base: str | None = None,
    actionlint_bin: str | None = None,
    run_cmd: RunCmd = subprocess.run,
) -> WorkflowValidationResult:
    """Run actionlint when workflow YAML changed."""
    try:
        files = changed_workflow_files(
            worktree,
            base=base,
            fail_on_base_error=True,
            run_cmd=run_cmd,
        )
    except WorkflowDiffFailed as exc:
        return WorkflowValidationResult(
            ok=False,
            reason="workflow diff failed",
            stdout=exc.stdout,
            stderr=exc.stderr or f"git diff failed: {' '.join(exc.command)}",
        )
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
