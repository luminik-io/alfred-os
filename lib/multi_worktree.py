"""Multi-repo worktree manager for cross-repo agents.

Coordinates creation and cleanup of per-repo git worktrees with
synchronised branch names and a shared feature ID. Used by bundle /
large-feature coordinators (e.g. Batman) that need to make changes
across two or more repos in lockstep.

Design:

- :class:`MultiWorktree` is a context manager. Cleanup runs on exit
  whether the block completed cleanly or raised; partial failures during
  setup roll back every worktree created so far before re-raising.
- The git interaction is injected via the :class:`GitRunner` Protocol so
  tests can drive the manager against a fake subprocess (no real git
  calls, no real worktrees). The default implementation shells out to
  ``git worktree`` via :mod:`subprocess`.
- No hardcoded repo names. The caller provides ``WorktreeRequest``
  records that fully describe each repo (local checkout path, branch
  name, target worktree directory). Resolution from logical repo names
  to filesystem paths is the caller's responsibility — typically a
  lookup against an env-configured map.
- 12-factor: configuration via env vars (``ALFRED_HOME``,
  ``WORKSPACE_ROOT``); the module reads defaults but every value is
  override-able through the constructor.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Protocol

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Public dataclasses.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeRequest:
    """A request to make one worktree.

    Args:
        repo_name: logical name (e.g. ``"backend"``). Used for branch /
            worktree-directory naming and as the key in the resulting
            specs map; not used to resolve filesystem paths.
        checkout_path: existing local git checkout to branch from. Must
            be a valid repo (i.e. contain ``.git``).
        gh_repo: optional GitHub repo slug for downstream PR helpers.
            Not used by this module directly.
        base: base ref to branch from (default ``"origin/main"``).
    """

    repo_name: str
    checkout_path: Path
    gh_repo: str = ""
    base: str = "origin/main"


@dataclass(frozen=True)
class WorktreeSpec:
    """A successfully-created worktree."""

    repo_name: str
    worktree_path: Path
    branch_name: str
    gh_repo: str
    checkout_path: Path


# --------------------------------------------------------------------------
# Injection seam: git is reached via a Protocol so tests can substitute.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GitResult:
    """Minimal shape returned by a :class:`GitRunner` call."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class GitRunner(Protocol):
    """Protocol for running a git command in a working directory."""

    def __call__(
        self, args: Sequence[str], *, cwd: Path, timeout: int = 60
    ) -> GitResult:  # pragma: no cover — Protocol body
        ...


class SubprocessGitRunner:
    """Default :class:`GitRunner` implementation; shells out to ``git``."""

    def __call__(
        self, args: Sequence[str], *, cwd: Path, timeout: int = 60
    ) -> GitResult:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
        return GitResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )


# --------------------------------------------------------------------------
# Context-manager API.
# --------------------------------------------------------------------------


def _default_worktree_root() -> Path:
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base) / "worktrees"


@dataclass
class MultiWorktree:
    """Context manager that creates per-repo worktrees and tears them down.

    Usage::

        requests = [
            WorktreeRequest("backend", Path("/home/me/code/backend")),
            WorktreeRequest("frontend", Path("/home/me/code/frontend")),
        ]
        with MultiWorktree(requests, agent="batman", feature_id="42") as wts:
            for spec in wts.specs:
                ...  # make commits inside spec.worktree_path

    On block exit (success or exception) every worktree is removed via
    ``git worktree remove --force``. Cleanup failures are logged but do
    not mask the original exception.
    """

    requests: Sequence[WorktreeRequest]
    agent: str
    feature_id: str
    worktree_root: Path = field(default_factory=_default_worktree_root)
    git: GitRunner = field(default_factory=SubprocessGitRunner)
    now: float | None = None
    _specs: list[WorktreeSpec] = field(default_factory=list, init=False, repr=False)

    @property
    def specs(self) -> list[WorktreeSpec]:
        """Specs created so far. Mutated by ``__enter__``."""
        return list(self._specs)

    # ----- context-manager protocol ----------------------------------------

    def __enter__(self) -> MultiWorktree:
        ts = int(self.now if self.now is not None else time.time())
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        try:
            for req in self.requests:
                spec = self._create_one(req, ts=ts)
                self._specs.append(spec)
        except Exception:
            self._cleanup_quiet()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._cleanup_quiet()

    # ----- single-worktree helpers ----------------------------------------

    def _create_one(self, req: WorktreeRequest, *, ts: int) -> WorktreeSpec:
        if not req.checkout_path.exists():
            raise RuntimeError(f"local checkout not found: {req.checkout_path}")

        branch = f"{self.agent}/{self.feature_id}-{req.repo_name}-{ts}"
        wt_path = (
            self.worktree_root
            / f"{self.agent}-{req.repo_name}-{self.feature_id}-{ts}"
        )

        # Fetch the request's configured base ref, not a hardcoded "main".
        # Repos using "master" or a release-train base would otherwise fail
        # at the fetch step before `worktree add` could use the requested
        # base. `req.base` may be a bare ref ("main", "master") or qualified
        # ("origin/release/24w11"); strip a leading "origin/" so the
        # `git fetch origin <ref>` form is well-defined.
        fetch_ref = req.base.removeprefix("origin/")
        fetch = self.git(
            ["fetch", "origin", fetch_ref], cwd=req.checkout_path, timeout=60
        )
        if fetch.returncode != 0:
            raise RuntimeError(
                f"git fetch failed for {req.repo_name} base={req.base!r}: "
                f"{fetch.stderr.strip()}"
            )

        add = self.git(
            ["worktree", "add", "-b", branch, str(wt_path), req.base],
            cwd=req.checkout_path,
            timeout=60,
        )
        if add.returncode != 0:
            raise RuntimeError(
                f"worktree add failed for {req.repo_name}: {add.stderr.strip()}"
            )

        return WorktreeSpec(
            repo_name=req.repo_name,
            worktree_path=wt_path,
            branch_name=branch,
            gh_repo=req.gh_repo,
            checkout_path=req.checkout_path,
        )

    def _cleanup_quiet(self) -> None:
        for spec in list(reversed(self._specs)):
            try:
                self._remove_one(spec)
            except Exception as e:
                logger.warning(
                    "worktree cleanup failed for %s: %s", spec.repo_name, e
                )
        self._specs.clear()

    def _remove_one(self, spec: WorktreeSpec) -> None:
        res = self.git(
            ["worktree", "remove", "--force", str(spec.worktree_path)],
            cwd=spec.checkout_path,
            timeout=30,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"worktree remove failed for {spec.repo_name}: {res.stderr.strip()}"
            )


# --------------------------------------------------------------------------
# Module-level helpers (no class needed).
# --------------------------------------------------------------------------


def verify_worktree_clean(
    wt_path: Path, *, git: GitRunner | None = None
) -> bool:
    """True if a worktree has no uncommitted changes.

    Args:
        wt_path: path to the worktree (``cwd`` for the ``git status`` call).
        git: optional injected runner for tests; defaults to subprocess.

    Returns:
        True if clean, False if dirty or status query failed.
    """
    runner = git or SubprocessGitRunner()
    res = runner(["status", "--short"], cwd=wt_path, timeout=10)
    if res.returncode != 0:
        return False
    return not (res.stdout or "").strip()


def iter_worktree_paths(specs: Iterable[WorktreeSpec]) -> Iterator[Path]:
    """Yield the worktree paths of an iterable of specs."""
    for spec in specs:
        yield spec.worktree_path
