"""Tests for ``bin/scrub-check.sh`` — specifically the extra-patterns loader.

The scrub script loads operator-specific exact patterns from a gitignored
file (``bin/.scrub-extra-patterns``, overridable via
``ALFRED_SCRUB_EXTRA_PATTERNS``). The loader skips comment lines, but a
naive ``#*`` glob also drops legitimate patterns that *begin* with ``#``,
such as a private Slack channel name (``#my-channel``). These tests pin
the contract: a line is a comment iff it is exactly ``#`` or starts with
``# `` (hash + space); ``#my-channel`` (hash + non-space) is a real
pattern and must be loaded.

We drive the script over a throwaway git repo so its working-tree scan
sees only files we plant, and we point ``ALFRED_SCRUB_EXTRA_PATTERNS`` at
a controlled file. A pattern that is *loaded* trips the scan (exit 1)
when a planted file contains the matching text; a pattern that is
*skipped* leaves the scan clean (exit 0).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRUB = REPO / "bin" / "scrub-check.sh"


def _isolated_git_env() -> dict[str, str]:
    """A git env that ignores the operator's global config and hooks.

    The host may install a global ``core.hooksPath`` pre-commit hook (e.g.
    enforcing a specific author email). The throwaway repos here must not
    inherit it, so we point global/system config at /dev/null and disable
    hooks via env. ``GIT_TERMINAL_PROMPT=0`` keeps git non-interactive.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_isolated_git_env(),
    )


@pytest.fixture
def scrub_repo(tmp_path):
    """A throwaway git repo containing a copy of scrub-check.sh.

    ``scrub-check.sh`` resolves its ROOT_DIR as the parent of ``bin/`` and
    scans that repo's tracked + untracked files. We copy the script into a
    fresh repo so the scan only ever sees files we plant here, never the
    real alfred-os tree.
    """
    if not shutil.which("git"):
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    shutil.copy2(SCRUB, repo / "bin" / "scrub-check.sh")

    _git(["init", "-q"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "test"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "init"], repo)
    return repo


def _run_scrub(repo: Path, extra_patterns_file: Path) -> subprocess.CompletedProcess:
    env = _isolated_git_env()
    env["ALFRED_SCRUB_EXTRA_PATTERNS"] = str(extra_patterns_file)
    return subprocess.run(
        ["bash", str(repo / "bin" / "scrub-check.sh")],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def test_hash_prefixed_pattern_is_loaded_not_treated_as_comment(scrub_repo, tmp_path):
    """``#my-channel`` (hash + non-space) is a real pattern, not a comment.

    With it loaded as a pattern, a planted file containing ``#my-channel``
    must trip the scan (exit 1) rather than being silently ignored.
    """
    extra = tmp_path / "extra-patterns"
    extra.write_text("#my-channel\n")

    # Plant a file that contains the channel name the pattern matches.
    (scrub_repo / "leaky.txt").write_text("posted to #my-channel last night\n")

    res = _run_scrub(scrub_repo, extra)

    assert res.returncode == 1, (
        "loaded '#my-channel' pattern should have tripped the scan on a file "
        f"containing it; stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "#my-channel" in res.stdout or "#my-channel" in res.stderr


def test_true_comment_lines_are_skipped(scrub_repo, tmp_path):
    """Lines that are exactly ``#`` or start with ``# `` are comments.

    They must NOT become scan patterns. A file containing the literal
    comment text must therefore leave the scan clean (exit 0).
    """
    extra = tmp_path / "extra-patterns"
    # A bare hash, a hash+space comment, and a blank line — all skipped.
    extra.write_text("#\n# a comment\n\n")

    # Plant a file that contains the comment text. If '# a comment' were
    # wrongly loaded as a pattern, this file would trip the scan.
    (scrub_repo / "notes.txt").write_text("this line has # a comment in it\n")

    res = _run_scrub(scrub_repo, extra)

    assert res.returncode == 0, (
        "true comment lines must be skipped, not loaded as patterns; "
        f"stdout={res.stdout!r} stderr={res.stderr!r}"
    )
    assert "clean" in res.stdout


def test_comment_and_pattern_coexist_in_one_file(scrub_repo, tmp_path):
    """A realistic extra-patterns file mixes comments and a ``#`` pattern.

    Only the true comment is skipped; the ``#my-channel`` pattern still
    loads and trips on a matching file.
    """
    extra = tmp_path / "extra-patterns"
    extra.write_text("# private slack channels:\n#my-channel\n")

    (scrub_repo / "leak.md").write_text("see #my-channel for details\n")

    res = _run_scrub(scrub_repo, extra)

    assert res.returncode == 1, (
        "the '#my-channel' pattern below a comment line must still load; "
        f"stdout={res.stdout!r} stderr={res.stderr!r}"
    )
