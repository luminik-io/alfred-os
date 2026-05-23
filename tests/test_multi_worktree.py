"""Tests for ``lib/multi_worktree.py``.

The git-shellout is injected via the ``GitRunner`` Protocol, so these
tests run with a fake runner that records calls instead of touching real
git. No subprocesses spawned, no real worktrees created.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

from multi_worktree import (  # noqa: E402
    GitResult,
    MultiWorktree,
    WorktreeRequest,
    iter_worktree_paths,
    verify_worktree_clean,
)


@dataclass
class FakeGit:
    """Records every git command and returns scripted results."""

    script: dict[str, GitResult] = field(default_factory=dict)
    """Map of ``" ".join(args)`` -> result. Missing keys -> success."""
    calls: list[tuple[tuple[str, ...], Path]] = field(default_factory=list)

    def __call__(self, args: Sequence[str], *, cwd: Path, timeout: int = 60) -> GitResult:
        key = " ".join(args)
        self.calls.append((tuple(args), cwd))
        return self.script.get(key, GitResult(returncode=0))


@pytest.fixture()
def repos(tmp_path: Path) -> list[Path]:
    """Two fake local checkouts, each existing on disk."""
    paths: list[Path] = []
    for name in ("backend", "frontend"):
        p = tmp_path / "code" / name
        p.mkdir(parents=True)
        paths.append(p)
    return paths


def test_context_manager_creates_and_cleans_up(repos: list[Path], tmp_path: Path) -> None:
    fake = FakeGit()
    requests = [
        WorktreeRequest(repo_name="backend", checkout_path=repos[0], gh_repo="your-org/backend"),
        WorktreeRequest(repo_name="frontend", checkout_path=repos[1], gh_repo="your-org/frontend"),
    ]
    wt_root = tmp_path / "wt"

    with MultiWorktree(
        requests,
        agent="batman",
        feature_id="42",
        worktree_root=wt_root,
        git=fake,
        now=1_700_000_000,
    ) as mw:
        assert len(mw.specs) == 2
        for spec in mw.specs:
            assert spec.branch_name.startswith("batman/42-")
            assert spec.worktree_path.parent == wt_root
            # Worktree path was passed to `worktree add`
            assert any(
                "worktree" in c[0] and "add" in c[0] and str(spec.worktree_path) in c[0]
                for c in fake.calls
            )

    # After exit, two `worktree remove --force` calls should have happened
    remove_calls = [c for c in fake.calls if c[0][:3] == ("worktree", "remove", "--force")]
    assert len(remove_calls) == 2


def test_setup_failure_rolls_back_already_created(repos: list[Path], tmp_path: Path) -> None:
    """If repo 2's `worktree add` fails, repo 1's worktree must be cleaned up."""
    fake = FakeGit()
    # Make the SECOND `worktree add` fail (target = frontend repo).
    fake.script[
        "worktree add -b batman/x-frontend-100 "
        + str(tmp_path / "wt" / "batman-frontend-x-100")
        + " origin/main"
    ] = GitResult(returncode=1, stderr="boom")

    requests = [
        WorktreeRequest("backend", repos[0]),
        WorktreeRequest("frontend", repos[1]),
    ]
    wt_root = tmp_path / "wt"
    mw = MultiWorktree(
        requests, agent="batman", feature_id="x", worktree_root=wt_root, git=fake, now=100
    )
    with pytest.raises(RuntimeError, match="worktree add failed"):
        mw.__enter__()

    # Rollback ran: a remove call for the backend worktree was issued.
    remove_calls = [c for c in fake.calls if c[0][:2] == ("worktree", "remove")]
    assert remove_calls, "expected at least one remove during rollback"


def test_missing_local_checkout_raises(tmp_path: Path) -> None:
    requests = [
        WorktreeRequest("ghost", tmp_path / "does-not-exist"),
    ]
    mw = MultiWorktree(
        requests, agent="batman", feature_id="g", worktree_root=tmp_path / "wt", git=FakeGit()
    )
    with pytest.raises(RuntimeError, match="local checkout not found"):
        mw.__enter__()


def test_fetch_failure_is_surfaced(repos: list[Path], tmp_path: Path) -> None:
    fake = FakeGit(script={"fetch origin main": GitResult(returncode=1, stderr="net error")})
    requests = [WorktreeRequest("backend", repos[0])]
    mw = MultiWorktree(
        requests, agent="batman", feature_id="f", worktree_root=tmp_path / "wt", git=fake
    )
    with pytest.raises(RuntimeError, match="git fetch failed"):
        mw.__enter__()


def test_cleanup_errors_are_logged_not_raised(repos: list[Path], tmp_path: Path) -> None:
    fake = FakeGit()
    # Make the cleanup remove call fail.
    requests = [WorktreeRequest("backend", repos[0])]
    wt_root = tmp_path / "wt"
    with MultiWorktree(
        requests, agent="batman", feature_id="c", worktree_root=wt_root, git=fake, now=200
    ) as mw:
        # Schedule failure on the upcoming remove call by mutating the script.
        wt_path = mw.specs[0].worktree_path
        fake.script[f"worktree remove --force {wt_path}"] = GitResult(
            returncode=1, stderr="cannot remove"
        )
    # No exception bubbled out of the context manager.


def test_verify_worktree_clean_true_on_empty_status(tmp_path: Path) -> None:
    fake = FakeGit(script={"status --short": GitResult(returncode=0, stdout="")})
    assert verify_worktree_clean(tmp_path, git=fake) is True


def test_verify_worktree_clean_false_when_dirty(tmp_path: Path) -> None:
    fake = FakeGit(script={"status --short": GitResult(returncode=0, stdout=" M lib/labels.py\n")})
    assert verify_worktree_clean(tmp_path, git=fake) is False


def test_iter_worktree_paths_yields_each(repos: list[Path], tmp_path: Path) -> None:
    fake = FakeGit()
    requests = [WorktreeRequest("backend", repos[0])]
    with MultiWorktree(
        requests, agent="batman", feature_id="i", worktree_root=tmp_path / "wt", git=fake, now=300
    ) as mw:
        paths = list(iter_worktree_paths(mw.specs))
    assert paths == [Path(tmp_path / "wt" / "batman-backend-i-300")]
