"""Tests for the pennyworth agent_runner primitives.

Covers the deterministic helpers: PreflightSpec / preflight, doctor_mode,
load_prompt, commit_trailer, HandoffTable, EventLog. Skips anything that
shells out to gh / aws / claude / git.

Run via `pytest tests/`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a clean tmp dir before importing agent_runner.

    State files (locks, spend, slack-cache, event logs) all live under
    HERMES_HOME, so this fixture is what keeps tests from polluting the
    operator's real ~/.hermes/.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    # Force a fresh import so module-level constants pick up the env vars.
    for mod in list(sys.modules):
        if mod == "agent_runner" or mod.startswith("agent_runner."):
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def test_preflight_passes_when_env_and_bins_resolve(monkeypatch):
    import agent_runner as ar

    spec = ar.PreflightSpec(
        agent="test",
        env_vars=["HERMES_HOME"],
        bins=["python3"],  # always present in CI
    )
    # Should not raise
    ar.preflight(spec)


def test_preflight_raises_on_missing_env(monkeypatch):
    import agent_runner as ar
    monkeypatch.delenv("HERMES_HOME", raising=False)

    spec = ar.PreflightSpec(agent="test", env_vars=["HERMES_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)


def test_preflight_raises_on_missing_binary():
    import agent_runner as ar

    spec = ar.PreflightSpec(agent="test", bins=["__definitely_not_a_real_command__"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)


def test_doctor_mode_default_false(monkeypatch):
    import agent_runner as ar
    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
    assert ar.doctor_mode() is False


@pytest.mark.parametrize("val,expected", [
    ("1", True),
    ("yes", True),
    ("true", True),
    ("0", False),
    ("false", False),
    ("False", False),
    ("", False),
])
def test_doctor_mode_env_values(monkeypatch, val, expected):
    import agent_runner as ar
    monkeypatch.setenv("HERMES_DOCTOR", val)
    assert ar.doctor_mode() is expected


def test_load_prompt_substitutes_env_and_extras(monkeypatch, tmp_path):
    import agent_runner as ar
    monkeypatch.setenv("OPERATOR_GH_HANDLE", "alice")

    p = tmp_path / "prompt.md"
    p.write_text("Hello ${OPERATOR_GH_HANDLE}, your repo is ${REPO_SLUG}.")

    out = ar.load_prompt(p, extra_vars={"REPO_SLUG": "myorg/backend"})
    assert out == "Hello alice, your repo is myorg/backend."


def test_load_prompt_leaves_unset_vars_as_literal(tmp_path):
    """A missing var should NOT silently substitute empty string — that's
    a foot-gun for `gh repo view ${REPO_SLUG}` calls. Use preflight to
    surface missing config explicitly."""
    import agent_runner as ar

    p = tmp_path / "prompt.md"
    p.write_text("Repo: ${THIS_VAR_IS_NOT_SET_ANYWHERE}")
    out = ar.load_prompt(p)
    assert out == "Repo: ${THIS_VAR_IS_NOT_SET_ANYWHERE}"


def test_commit_trailer_is_git_interpret_trailers_compatible():
    import agent_runner as ar

    t = ar.commit_trailer("lucius", "2026-04-29-1647-bf3a")
    assert "Agent-Codename: lucius" in t
    assert "Agent-Firing-Id: 2026-04-29-1647-bf3a" in t
    # Trailer keys: "Word-Word: value" - parseable by `git interpret-trailers --parse`.
    for line in t.splitlines():
        key, _, value = line.partition(": ")
        assert key and value
        assert " " not in key  # no embedded spaces in trailer keys


def test_commit_trailer_extra_keys():
    import agent_runner as ar

    t = ar.commit_trailer(
        "lucius", "2026-04-29",
        extra={"issue": "myorg/backend#275", "model_used": "claude-opus-4-7"},
    )
    assert "Issue: myorg/backend#275" in t
    # underscore in key gets PascalCased per the trailer convention
    assert "Model-Used: claude-opus-4-7" in t


def test_handoff_table_round_trip():
    import agent_runner as ar

    ht = ar.HandoffTable()
    ht.add("drake", "issue_filed", "lucius")
    ht.add("lucius", "pr_opened", "rasalghul")
    ht.add("rasalghul", "review_p1", "nightwing")

    assert ht.consumers("drake") == ["issue_filed"]
    assert sorted(ht.producers("rasalghul")) == [("lucius", "pr_opened")]


def test_handoff_table_validate_flags_unknowns():
    import agent_runner as ar

    ht = ar.HandoffTable()
    ht.add("drake", "issue_filed", "lucius")
    ht.add("lucius", "pr_opened", "rasalghul")

    issues = ht.validate(known_codenames={"drake", "lucius"})
    # rasalghul missing; drake known; lucius known
    assert any("rasalghul" in m for m in issues)
    assert not any("drake" in m for m in issues)


def test_event_log_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # Re-import after env update
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar

    ev = ar.EventLog(agent="lucius", firing_id="test-firing-1")
    ev.emit("preflight_passed")
    ev.emit("issue_picked", repo="myorg/backend", number=275)
    ev.emit("pr_opened", url="https://example.com/pr/1", files_changed=12)

    lines = ev.path.read_text().strip().splitlines()
    assert len(lines) == 3

    records = [json.loads(line) for line in lines]
    for r in records:
        assert r["agent"] == "lucius"
        assert r["firing_id"] == "test-firing-1"
        assert "ts" in r and r["ts"].endswith("Z")

    assert records[0]["event"] == "preflight_passed"
    assert records[1]["repo"] == "myorg/backend"
    assert records[1]["number"] == 275
    assert records[2]["files_changed"] == 12


def test_event_log_default_path_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar

    ev = ar.EventLog(agent="bane")
    # Path should be under {HERMES_HOME}/state/bane/events/<firing-id>.jsonl
    assert "state/bane/events" in str(ev.path)
    assert ev.firing_id  # auto-generated


def test_event_log_swallows_oserror(tmp_path, monkeypatch, capsys):
    """A broken event log shouldn't kill an agent firing."""
    import agent_runner as ar

    # Path that can't be written: under a file (not a directory).
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    impossible = blocker / "events.jsonl"

    ev = ar.EventLog(agent="x", firing_id="y", path=impossible)
    ev.emit("test")  # must not raise

    err = capsys.readouterr().err
    assert "[event-log]" in err


def test_full_repo_helper(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("GH_ORG", "myorg")
    # Re-import to pick up GH_ORG
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar2

    assert ar2._full_repo("backend") == "myorg/backend"
    assert ar2._full_repo("other-org/other-repo") == "other-org/other-repo"


def test_full_repo_raises_when_org_unset(monkeypatch):
    monkeypatch.delenv("GH_ORG", raising=False)
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar

    with pytest.raises(RuntimeError, match="GH_ORG"):
        ar._full_repo("bare-slug")
