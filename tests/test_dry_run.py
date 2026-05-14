"""Tests for alfred-os ``--dry-run`` / ``ALFRED_DRY_RUN`` mode.

Dry-run runs the whole firing lifecycle but stubs every side-effecting
boundary: no real LLM call, no spend mutation, no gh / Slack / git side
effects. These tests assert each seam is stubbed and that the example
runners complete a full lifecycle exit-0 with zero host config.

Run via ``pytest tests/``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    """Point ALFRED_HOME at a clean tmp dir and import agent_runner fresh.

    Mirrors the fixture in test_agent_runner.py: every state file lives
    under ALFRED_HOME, so this is what keeps tests off the operator's
    real ~/.alfred/.
    """
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.delenv("ALFRED_DRY_RUN", raising=False)
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    yield


# ---------- is_dry_run / set_dry_run ----------


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
    ],
)
def test_is_dry_run_reads_env(monkeypatch, val, expected):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", val)
    assert ar.is_dry_run() is expected


def test_set_dry_run_round_trips(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("ALFRED_DRY_RUN", raising=False)
    assert ar.is_dry_run() is False
    ar.set_dry_run(True)
    assert ar.is_dry_run() is True
    ar.set_dry_run(False)
    assert ar.is_dry_run() is False


# ---------- LLM seam: no real claude / codex subprocess ----------


def test_claude_invoke_dry_run_makes_no_subprocess(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    monkeypatch.setattr(
        ar, "run", lambda *a, **kw: pytest.fail("claude_invoke shelled out under dry-run")
    )

    result = ar.claude_invoke("a prompt", workdir=Path("/tmp"), allowed_tools="", model="opus")

    assert result.success is True
    assert result.subtype == "success"
    assert result.cost_usd == 0.0
    assert result.raw.get("dry_run") is True
    assert "[dry-run]" in result.result_text


def test_codex_invoke_dry_run_makes_no_subprocess(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")

    def boom(*a, **kw):
        pytest.fail("codex_invoke shelled out under dry-run")

    monkeypatch.setattr(ar.subprocess, "run", boom)

    result = ar.codex_invoke("review this", workdir=Path("/tmp"), agent="reviewer")

    assert result.success is True
    assert result.cost_usd == 0.0
    assert result.raw.get("engine") == "codex"
    assert "[dry-run]" in result.result_text


def test_invoke_agent_engine_dry_run_does_not_call_real_engines(monkeypatch):
    """The default claude_fn / codex_fn route through the stubbed wrappers."""
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    monkeypatch.setattr(
        ar, "run", lambda *a, **kw: pytest.fail("real claude subprocess under dry-run")
    )
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **kw: pytest.fail("real codex subprocess under dry-run")
    )

    result, engine_used = ar.invoke_agent_engine(
        "implement this",
        engine="hybrid",
        agent="lucius",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read,Edit",
        timeout=60,
    )
    assert result.success is True
    assert engine_used == "claude"  # synthetic claude success => no fallback


# ---------- Spend seam: no real ledger mutation ----------


def test_spend_state_dry_run_writes_separate_ledger(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    spend = ar.SpendState("lucius")
    real_path = spend._path
    spend.increment(firings_today=1, turns_today=10, cost_usd_today=1.5)
    spend.set(consecutive_failures=0)

    # The real per-day ledger is never written under dry-run.
    assert not real_path.exists()
    # A clearly-separate dry-run ledger is used instead.
    dry_path = real_path.with_name(f"spend-dryrun-{ar.today_str()}.json")
    assert dry_path.exists()
    data = json.loads(dry_path.read_text())
    assert data["firings_today"] == 1
    assert data["turns_today"] == 10


def test_spend_state_real_ledger_untouched_after_dry_run(monkeypatch):
    """A dry-run firing must not inflate the agent's real counters."""
    import agent_runner as ar

    # First: a real firing writes the real ledger.
    monkeypatch.delenv("ALFRED_DRY_RUN", raising=False)
    real = ar.SpendState("bane")
    real.increment(firings_today=1)
    real_path = real._path
    assert json.loads(real_path.read_text())["firings_today"] == 1

    # Then: a dry-run firing must leave that real ledger untouched.
    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    dry = ar.SpendState("bane")
    dry.increment(firings_today=5, turns_today=99)
    assert json.loads(real_path.read_text())["firings_today"] == 1


def test_set_global_block_dry_run_writes_no_poison_pill(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    until = ar.set_global_block(hours=1, reason="lucius-error_rate_limit")
    assert until  # caller still gets the until-string for its messaging
    assert not ar.GLOBAL_BLOCKED_FILE.exists()
    # And the fleet is therefore not actually blocked.
    assert ar.is_globally_blocked() is None


# ---------- Slack seam: no real webhook POST ----------


def test_slack_post_dry_run_does_not_hit_webhook(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **kw: pytest.fail("slack_post hit the webhook under dry-run"),
    )

    assert ar.slack_post("the build shipped", severity="warn") is True


def test_slack_post_dry_run_logs_the_line(monkeypatch, capsys):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    ar.slack_post("staging is down", severity="alert")
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "would post to Slack" in out
    assert "severity=alert" in out
    assert "staging is down" in out


# ---------- GitHub seam: no gh mutation ----------


def test_gh_mutators_dry_run_make_no_subprocess(monkeypatch):
    """gh helpers stub out cleanly even with NOTHING configured, no GH_ORG,
    no gh auth. ``_full_repo`` falls back to a clearly-fake org placeholder."""
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.setattr(
        ar, "run", lambda *a, **kw: pytest.fail("gh helper shelled out under dry-run")
    )

    assert ar.gh_issue_edit("backend", 7, add_labels=["agent:in-flight"]) is True
    assert ar.gh_issue_comment("backend", 7, "a comment") is True
    assert ar.gh_pr_comment("backend", 7, "a comment") is True
    ar.ensure_labels("backend")  # must not raise / shell out
    url = ar.gh_pr_create(
        "backend", title="feat: x", body_file=Path("/tmp/none"), head="b", labels=["agent:authored"]
    )
    # No GH_ORG configured -> the clearly-fake dry-run placeholder org.
    assert url and url.startswith("https://github.com/dry-run-org/backend/pull/")


def test_claim_and_release_issue_dry_run_make_no_subprocess(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.setattr(
        ar, "run", lambda *a, **kw: pytest.fail("claim/release shelled out under dry-run")
    )
    monkeypatch.setattr(
        ar, "_issue_state", lambda *a, **kw: pytest.fail("claim/release read gh under dry-run")
    )

    assert ar.claim_issue("backend", 7, codename="lucius", firing_id="f1") is True
    assert (
        ar.release_issue(
            "backend",
            7,
            codename="lucius",
            firing_id="f1",
            outcome="success",
            transition_to="agent:pr-open",
            pr_url="https://example.com/pr/1",
        )
        is True
    )
    assert (
        ar.force_release_stale_claim(
            "backend",
            7,
            sweep_id="sweep-1",
            released_codename="lucius",
            released_firing_id="f1",
        )
        is True
    )


# ---------- git seam: no real worktree mutation ----------


def test_make_worktree_dry_run_uses_throwaway_repo(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DRY_RUN", "1")
    wt, branch = ar.make_worktree("backend", "lucius", "275")

    # A real, self-contained git repo in a temp dir, never the operator's
    # configured WORKSPACE checkout, never WORKTREE_ROOT.
    assert wt.exists()
    assert str(ar.WORKSPACE) not in str(wt)
    assert str(ar.WORKTREE_ROOT) not in str(wt)
    assert branch.startswith("lucius/275-")

    # The throwaway repo is coherent: one commit ahead of origin/main, so a
    # runner inspecting it sees the "engine committed" state.
    revs = ar.run(["git", "rev-list", "origin/main..HEAD"], cwd=str(wt), timeout=10).stdout.strip()
    assert len([line for line in revs.splitlines() if line.strip()]) == 1

    ar.remove_worktree("backend", wt)
    assert not wt.exists()


# ---------- end-to-end: example runners complete exit-0 with zero config ----------


def _run_example(script: str, env_extra: dict, tmp_path) -> subprocess.CompletedProcess:
    env = {
        "ALFRED_HOME": str(tmp_path / "alfred"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "PYTHONPATH": str(REPO_ROOT / "lib"),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def test_hello_example_dry_run_completes_exit_0(tmp_path):
    proc = _run_example("examples/bin/hello.py", {"ALFRED_DRY_RUN": "1"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "[dry-run]" in proc.stdout
    assert "would post to Slack" in proc.stdout


def test_echo_example_dry_run_completes_full_lifecycle_exit_0(tmp_path):
    """Echo runs pick -> claim -> invoke -> comment -> release with zero
    host config (no gh auth, no Claude, no Slack) and exits 0."""
    proc = _run_example("examples/bin/echo_summarise.py", {"ALFRED_DRY_RUN": "1"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # The whole lifecycle is narrated.
    assert "(pick)" in out
    assert "would claim" in out
    assert "would invoke claude" in out
    assert "would `gh issue comment" in out
    assert "would release" in out
    assert "would post to Slack" in out


def test_lucius_runner_dry_run_completes_full_lifecycle_exit_0(tmp_path):
    """Lucius runs pick -> claim -> worktree -> invoke -> push/PR -> release
    with zero host config and exits 0 on the happy path."""
    proc = _run_example("bin/lucius.py", {"ALFRED_DRY_RUN": "1"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "would `git worktree add" in out
    assert "would invoke claude" in out
    assert "would `git push" in out
    assert "would `gh pr create" in out
    assert "Lucius shipped" in out


def test_dry_run_writes_no_real_spend_ledger_for_runners(tmp_path):
    """After a dry-run firing only the dry-run ledger exists, never the real one."""
    proc = _run_example("examples/bin/echo_summarise.py", {"ALFRED_DRY_RUN": "1"}, tmp_path)
    assert proc.returncode == 0, proc.stderr
    echo_state = tmp_path / "alfred" / "state" / "echo"
    ledgers = sorted(p.name for p in echo_state.glob("spend-*.json")) if echo_state.exists() else []
    # Exactly the dry-run ledger, no real spend-<date>.json.
    assert ledgers, "expected a dry-run ledger to be written"
    assert all(name.startswith("spend-dryrun-") for name in ledgers), ledgers
