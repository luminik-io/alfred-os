from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (ROOT / "bin" / name).read_text()


def test_codex_agents_with_gh_or_worktree_access_bypass_sandbox() -> None:
    """Codex sandboxing cannot read the macOS keychain-backed gh auth reliably."""
    for name in ("bane.py", "drake.py", "lucius.py", "nightwing.py", "rasalghul.py", "robin.py"):
        assert "codex_bypass_approvals_and_sandbox=True" in _source(name)


def test_lucius_grants_codex_source_gitdir_for_worktree_commits() -> None:
    source = _source("lucius.py")
    assert 'codex_add_dirs=[(WORKSPACE / repo / ".git").resolve()]' in source
