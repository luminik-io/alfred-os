"""Tests for ``bin/nightwing.py`` no-commit diagnostics + escalation (issue #109).

The full firing loop touches gh + git + Slack and is not unit-testable
without heavy mocking. These tests target the three new helpers
(``diagnose_no_commit``, ``escalate_no_commit``, ``load/save_no_commit_streaks``)
and the streak-key shape, which is what the operator-facing behaviour
hinges on. The escalation Slack + label calls go through the existing
``slack_post`` / ``run`` injection points so we monkeypatch those.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "bin"
LIB_DIR = ROOT / "lib"


@pytest.fixture
def nightwing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "acme")
    sys.path.insert(0, str(LIB_DIR))
    module_name = "nightwing_under_test_no_commit"
    spec = importlib.util.spec_from_file_location(module_name, BIN_DIR / "nightwing.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(LIB_DIR))


# ---------- diagnose_no_commit ----------------------------------------------


def test_diagnose_no_commit_clean_worktree_blames_prose_only_engine(
    nightwing, monkeypatch, tmp_path
):
    """When the worktree is clean (no porcelain output), the diagnostic
    blames the engine for describing the fix without invoking a write
    tool. This is the most common failure mode and the operator
    response is 'tighten the prompt', not 'check hooks'."""

    class FakeProc:
        stdout = ""
        returncode = 0
        stderr = ""

    monkeypatch.setattr(nightwing, "run", lambda *a, **kw: FakeProc())
    out = nightwing.diagnose_no_commit(str(tmp_path), debug_dir=None)
    assert "clean" in out.lower()
    assert "described the fix in prose" in out
    assert "Transcript:" not in out  # debug_dir is None


def test_diagnose_no_commit_dirty_worktree_blames_missing_commit(nightwing, monkeypatch, tmp_path):
    """When git status --porcelain has output, the diagnostic surfaces
    those lines and points the operator at pre-commit hooks / missing
    `git commit` invocations, NOT at prompt tuning."""
    porcelain = " M src/foo.py\n?? .tmp-notes\nA  src/bar.py\n"

    class FakeProc:
        stdout = porcelain
        returncode = 0
        stderr = ""

    monkeypatch.setattr(nightwing, "run", lambda *a, **kw: FakeProc())
    out = nightwing.diagnose_no_commit(str(tmp_path), debug_dir="/state/x.jsonl")
    assert "git status --porcelain" in out
    assert " M src/foo.py" in out
    assert "?? .tmp-notes" in out
    assert "wrote files but did not commit" in out
    assert "Transcript: /state/x.jsonl" in out


def test_diagnose_no_commit_truncates_runaway_porcelain(nightwing, monkeypatch, tmp_path):
    """A massive worktree should not blow up the log line. The
    diagnostic caps the porcelain dump at a sensible number of rows."""
    porcelain = "\n".join(f" M file{i}.txt" for i in range(50))

    class FakeProc:
        stdout = porcelain
        returncode = 0
        stderr = ""

    monkeypatch.setattr(nightwing, "run", lambda *a, **kw: FakeProc())
    out = nightwing.diagnose_no_commit(str(tmp_path), debug_dir=None)
    # We cap at 20 + 1 header line. Counting porcelain rows in the
    # truncated output, each ` M fileN.txt` shows up at most once.
    porcelain_rows = [line for line in out.splitlines() if line.strip().startswith("M ")]
    assert len(porcelain_rows) <= 20


def test_diagnose_no_commit_tolerates_git_failure(nightwing, monkeypatch, tmp_path):
    """If git status itself blows up (corrupt worktree, missing git in
    PATH), the diagnostic must still return SOMETHING so the NO-COMMIT
    log line isn't empty. Defaults to the clean-tree branch."""

    def boom(*a, **kw):
        raise RuntimeError("git missing")

    monkeypatch.setattr(nightwing, "run", boom)
    out = nightwing.diagnose_no_commit(str(tmp_path), debug_dir=None)
    assert out  # non-empty
    assert "clean" in out.lower()


# ---------- streak load/save ------------------------------------------------


def test_streak_key_format_pins_pr_url_and_comment(nightwing):
    """The on-disk key shape is the (pr_url, comment_id) contract from
    the issue. Tests pin it because operators may grep / edit the file
    directly to clear stuck streaks."""
    key = nightwing._streak_key("backend", 123, 4567890)
    # agent_runner.GH_ORG is captured at module-load time; use whichever
    # value the module exposes so the test is robust to the test-host
    # GH_ORG default rather than hardcoding one.
    assert key == f"{nightwing.GH_ORG}/backend#123:4567890"


def test_no_commit_streaks_roundtrip_persists_to_alfred_state(nightwing):
    """load -> save -> load round-trips the streak counts."""
    nightwing.save_no_commit_streaks(
        {f"{nightwing.GH_ORG}/backend#1:42": 2, f"{nightwing.GH_ORG}/backend#1:43": 3}
    )
    out = nightwing.load_no_commit_streaks()
    assert out == {
        f"{nightwing.GH_ORG}/backend#1:42": 2,
        f"{nightwing.GH_ORG}/backend#1:43": 3,
    }


def test_no_commit_streaks_load_returns_empty_on_garbage(nightwing, tmp_path):
    """A corrupted streaks file must not crash the firing - the streak
    counter loses one cycle of memory, the firing continues."""
    nightwing.NO_COMMIT_STREAKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    nightwing.NO_COMMIT_STREAKS_FILE.write_text("not-json{")
    assert nightwing.load_no_commit_streaks() == {}


# ---------- escalate_no_commit ----------------------------------------------


def test_escalate_no_commit_posts_slack_and_adds_label(nightwing, monkeypatch):
    """At the threshold, escalation posts to Slack AND adds the
    nightwing:human-needed label. Both calls happen even if one fails."""
    slack_calls: list[tuple[tuple, dict]] = []
    run_calls: list[list[str]] = []

    monkeypatch.setattr(nightwing, "slack_post", lambda *a, **kw: slack_calls.append((a, kw)))

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **_kw):
        run_calls.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(nightwing, "run", fake_run)
    nightwing.escalate_no_commit("backend", 123, 4567890, streak=3)

    assert slack_calls, "expected a Slack escalation post"
    msg = slack_calls[0][0][0]
    assert "3 consecutive no-commits" in msg
    assert "nightwing:human-needed" in msg
    label_cmd = next(
        cmd for cmd in run_calls if "--add-label" in cmd and "nightwing:human-needed" in cmd
    )
    assert "pr" in label_cmd and "edit" in label_cmd
    assert f"{nightwing.GH_ORG}/backend" in label_cmd


def test_escalate_no_commit_survives_label_add_failure(nightwing, monkeypatch):
    """A label-permission misconfig must not kill the firing; the
    Slack post is still the operator-facing signal."""
    slack_calls: list[tuple[tuple, dict]] = []

    monkeypatch.setattr(nightwing, "slack_post", lambda *a, **kw: slack_calls.append((a, kw)))

    def boom(*a, **kw):
        raise RuntimeError("gh label permission denied")

    monkeypatch.setattr(nightwing, "run", boom)
    # Should NOT raise.
    nightwing.escalate_no_commit("backend", 123, 4567890, streak=3)
    assert slack_calls


# ---------- pick_target gates on nightwing:human-needed (PR #111 follow-up) -


def _pick_target_pr_row(num: int, *, labels: list[str]):
    """Build a minimal PR row in the shape `gh pr list --json` returns."""
    return {
        "number": num,
        "headRefName": f"branch-{num}",
        "reviewDecision": "",
        "createdAt": "2026-05-25T10:00:00Z",
        "labels": [{"name": name} for name in labels],
    }


def test_fixed_comment_ids_from_pr_comments_parses_nightwing_replies(nightwing):
    comments = [
        {"body": "Nightwing: fixed in abc1234 (re: comment 4567890 from coderabbitai[bot])"},
        {"body": "not a fixed reply"},
    ]

    assert nightwing.fixed_comment_ids_from_pr_comments(comments) == {4567890}


def test_pick_target_skips_comment_fixed_by_prior_pr_reply(nightwing, monkeypatch):
    monkeypatch.setenv("ALFRED_NIGHTWING_REPOS", "backend")
    fake_comment = {
        "id": 4567890,
        "user": {"login": "coderabbitai[bot]"},
        "body": "P1: still visible in the review thread",
        "path": "src/x.py",
        "line": 1,
    }
    fixed_reply = {
        "body": "Nightwing: fixed in abc1234 (re: comment 4567890 from coderabbitai[bot])"
    }

    def fake_gh_json(cmd, *, default):
        joined = " ".join(str(part) for part in cmd)
        if "pr list" in joined:
            return [_pick_target_pr_row(99, labels=["agent:authored"])]
        if "/pulls/" in joined:
            return [fake_comment]
        if "/issues/" in joined:
            return [fixed_reply]
        return default

    monkeypatch.setattr(nightwing, "gh_json", fake_gh_json)
    monkeypatch.setattr(nightwing, "is_repo_paused", lambda _repo: False)
    monkeypatch.setattr(nightwing, "WATCH_REPOS", ["backend"])

    repo, pr, comments = nightwing.pick_target(fixed_ids=set())

    assert (repo, pr, comments) == (None, None, None)


def test_pick_target_skips_pr_carrying_human_needed_label(nightwing, monkeypatch):
    """Once Nightwing escalates a PR, the operator owns it; subsequent
    firings must not re-pick its comments and burn turns."""
    monkeypatch.setenv("ALFRED_NIGHTWING_REPOS", "backend")

    def fake_gh_json(cmd, *, default):
        if "pr" in cmd and "list" in cmd:
            return [_pick_target_pr_row(99, labels=["agent:authored", "nightwing:human-needed"])]
        return default

    monkeypatch.setattr(nightwing, "gh_json", fake_gh_json)
    monkeypatch.setattr(nightwing, "is_repo_paused", lambda _repo: False)
    monkeypatch.setattr(nightwing, "WATCH_REPOS", ["backend"])

    repo, pr, comments = nightwing.pick_target(fixed_ids=set())
    assert (repo, pr, comments) == (None, None, None)


def test_pick_target_re_admits_pr_when_reset_label_also_present(nightwing, monkeypatch):
    """`nightwing:reset` is the operator's `try again` signal; when both
    labels are set, the PR must enter the pool so the inner reset
    handler can clear state and Nightwing can attempt the comments."""
    monkeypatch.setenv("ALFRED_NIGHTWING_REPOS", "backend")

    fake_comment = {
        "id": 1,
        "user": {"login": "coderabbitai[bot]"},
        "body": "P0: a real issue",
        "path": "src/x.py",
        "line": 1,
    }

    def fake_gh_json(cmd, *, default):
        if "pr" in cmd and "list" in cmd:
            return [
                _pick_target_pr_row(
                    99,
                    labels=["agent:authored", "nightwing:human-needed", "nightwing:reset"],
                )
            ]
        # /pulls/.../comments → inline review comments (return the bot
        # P0 once); /issues/.../comments → issue-comment thread
        # (empty for this test).
        if "pulls/" in str(cmd):
            return [fake_comment]
        if "issues/" in str(cmd):
            return []
        return default

    monkeypatch.setattr(nightwing, "gh_json", fake_gh_json)
    monkeypatch.setattr(nightwing, "is_repo_paused", lambda _repo: False)
    monkeypatch.setattr(nightwing, "WATCH_REPOS", ["backend"])

    repo, pr, comments = nightwing.pick_target(fixed_ids=set())
    # `agent:authored` PR with one bot P0 comment must be re-admitted when
    # the operator has dual-labelled it with `nightwing:reset`.
    assert repo == "backend"
    assert pr is not None and pr["number"] == 99
    assert [c["id"] for c in comments] == [1]
