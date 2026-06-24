"""Tests for the alfred-os agent_runner primitives.

Covers the deterministic helpers: PreflightSpec / preflight, doctor_mode,
load_prompt, commit_trailer, HandoffTable, EventLog. Skips anything that
shells out to gh / aws / claude / git.

Run via `pytest tests/`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    """Point ALFRED_HOME at a clean tmp dir before importing agent_runner.

    State files (locks, spend, slack-cache, event logs) all live under
    ALFRED_HOME, so this fixture is what keeps tests from polluting the
    operator's real ~/.alfred/.
    """
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
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
        env_vars=["ALFRED_HOME"],
        bins=["python3"],  # always present in CI
    )
    # Should not raise
    ar.preflight(spec)


def test_preflight_raises_on_missing_env(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="test", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_raises_on_missing_binary(monkeypatch):
    import agent_runner as ar

    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="test", bins=["__definitely_not_a_real_command__"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_requires_claude_credential_when_unreachable(monkeypatch, capsys):
    """With require_claude_credential set and no token in env or
    $ALFRED_HOME/.env, preflight must fail and name authentication as the
    cause with the remedy. This is the silent-401 guard."""
    import agent_runner as ar

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: None)

    spec = ar.PreflightSpec(agent="test", require_claude_credential=True)
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    out = capsys.readouterr().out
    assert "Claude credential unreachable" in out
    assert "alfred setup-token" in out


def test_preflight_passes_when_credential_in_env(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fromenv")
    spec = ar.PreflightSpec(agent="test", require_claude_credential=True)
    ar.preflight(spec)  # should not raise


def test_preflight_passes_when_credential_in_env_file(monkeypatch):
    """A token that lives only in $ALFRED_HOME/.env (not the process env)
    satisfies the check, mirroring how agent-launch loads it at runtime."""
    import os

    import agent_runner as ar

    home = Path(os.environ["ALFRED_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    spec = ar.PreflightSpec(agent="test", require_claude_credential=True)
    ar.preflight(spec)  # should not raise


def test_preflight_suppresses_slack_when_not_under_launchd(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("ALFRED_PREFLIGHT_FORCE_SLACK", raising=False)
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_suppresses_slack_when_launchd_env_is_placeholder(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.delenv("ALFRED_PREFLIGHT_FORCE_SLACK", raising=False)
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_posts_slack_under_launchd(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "alfred.lucius")
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert len(posted) == 1
    assert "lucius preflight failed" in posted[0]


@pytest.mark.parametrize("doctor_value", ["0", "false", "no", "off"])
def test_preflight_posts_slack_when_doctor_env_is_false(monkeypatch, doctor_value):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "alfred.lucius")
    monkeypatch.setenv("ALFRED_DOCTOR", doctor_value)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert len(posted) == 1


def test_preflight_force_slack_env_overrides_manual_suppression(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setenv("ALFRED_PREFLIGHT_FORCE_SLACK", "1")
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    monkeypatch.delenv("ALFRED_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["ALFRED_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert len(posted) == 1


def test_preflight_reports_gh_auth_timeout(monkeypatch):
    import agent_runner as ar

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    monkeypatch.setattr(ar, "slack_post", lambda *a, **kw: True)

    with pytest.raises(ar.PreflightFailed):
        ar.preflight(ar.PreflightSpec(agent="test", require_gh_auth=True))


def test_preflight_reports_aws_timeout(monkeypatch):
    import agent_runner as ar

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    monkeypatch.setattr(ar, "slack_post", lambda *a, **kw: True)

    with pytest.raises(ar.PreflightFailed):
        ar.preflight(ar.PreflightSpec(agent="test", aws_profile="alfred-test"))


def test_maybe_set_global_block_for_provider_limits(monkeypatch):
    import agent_runner as ar

    calls = []
    monkeypatch.setattr(ar, "set_global_block", lambda **kw: calls.append(kw) or "until")

    result = type("Result", (), {"subtype": "error_rate_limit"})()

    assert ar.maybe_set_global_block_for_result("robin", result) == "until"
    assert calls == [{"hours": 1, "reason": "robin-error_rate_limit"}]


def test_maybe_set_global_block_ignores_non_provider_errors(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "set_global_block", lambda **kw: pytest.fail("unexpected block"))
    result = type("Result", (), {"subtype": "error"})()

    assert ar.maybe_set_global_block_for_result("robin", result) is None


def test_maybe_set_global_block_ignores_codex_provider_limits(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "set_global_block", lambda **kw: pytest.fail("unexpected block"))
    result = type("Result", (), {"subtype": "error_rate_limit"})()

    assert ar.maybe_set_global_block_for_result("robin", result, engine_used="codex") is None
    assert (
        ar.maybe_set_global_block_for_result("robin", result, engine_used="codex-fallback") is None
    )


def test_doctor_mode_default_false(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)
    assert ar.doctor_mode() is False


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("yes", True),
        ("true", True),
        ("0", False),
        ("false", False),
        ("False", False),
        ("no", False),
        ("off", False),
        ("OFF", False),
        ("", False),
    ],
)
def test_doctor_mode_env_values(monkeypatch, val, expected):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_DOCTOR", val)
    assert ar.doctor_mode() is expected


def test_load_prompt_substitutes_env_and_extras(monkeypatch, tmp_path):
    import agent_runner as ar

    monkeypatch.setenv("OPERATOR_GH_HANDLE", "alice")

    p = tmp_path / "prompt.md"
    p.write_text("Hello ${OPERATOR_GH_HANDLE}, your repo is ${REPO_SLUG}.")

    out = ar.load_prompt(p, extra_vars={"REPO_SLUG": "myorg/backend"})
    assert out == "Hello alice, your repo is myorg/backend."


def test_load_prompt_leaves_unset_vars_as_literal(tmp_path):
    """A missing var should NOT silently substitute empty string, that's
    a foot-gun for `gh repo view ${REPO_SLUG}` calls. Use preflight to
    surface missing config explicitly."""
    import agent_runner as ar

    p = tmp_path / "prompt.md"
    p.write_text("Repo: ${THIS_VAR_IS_NOT_SET_ANYWHERE}")
    out = ar.load_prompt(p)
    assert out == "Repo: ${THIS_VAR_IS_NOT_SET_ANYWHERE}"


def test_agent_role_returns_empty_when_unset(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("ALFRED_LUCIUS_ROLE", raising=False)
    assert ar.agent_role("lucius") == ""
    assert ar.agent_role("") == ""


def test_agent_role_reads_alfred_codename_role_env(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Single-repo feature engineer")
    assert ar.agent_role("lucius") == "Single-repo feature engineer"


def test_agent_role_translates_dash_to_underscore_for_compound_codenames(monkeypatch):
    """`alfred-nightly` and `brand-mention-scanner` use ALFRED_<CODENAME>_ROLE
    where dashes in the codename become underscores in the env-var key."""
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_ALFRED_NIGHTLY_ROLE", "Overnight orchestrator (Sun 22:00)")
    monkeypatch.setenv("ALFRED_BRAND_MENTION_SCANNER_ROLE", "Brand mention monitor")
    assert ar.agent_role("alfred-nightly") == "Overnight orchestrator (Sun 22:00)"
    assert ar.agent_role("brand-mention-scanner") == "Brand mention monitor"


def test_codename_with_role_formats_when_role_set(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Single-repo feature engineer")
    assert ar.codename_with_role("lucius") == "lucius (Single-repo feature engineer)"


def test_codename_with_role_falls_back_to_bare_codename_when_no_role(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("ALFRED_LUCIUS_ROLE", raising=False)
    assert ar.codename_with_role("lucius") == "lucius"


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
        "lucius",
        "2026-04-29",
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
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
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


def test_event_log_default_path_under_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar

    ev = ar.EventLog(agent="bane")
    # Path should be under {ALFRED_HOME}/state/bane/events/<firing-id>.jsonl
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
    # A valid (closed-set) event type whose WRITE fails: the OSError is swallowed
    # and logged, never raised, so a broken event log cannot kill a firing.
    ev.emit("firing_started")  # must not raise

    err = capsys.readouterr().err
    assert "[event-log]" in err


def test_full_repo_helper(monkeypatch):

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


def test_route_llm_codex_dispatches_without_claude(monkeypatch):
    import agent_runner as ar

    calls: list[tuple[str, dict]] = []

    def fake_codex(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return ar.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="codex-session",
            result_text="codex ok",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    monkeypatch.setattr(ar, "codex_invoke", fake_codex)
    monkeypatch.setattr(ar, "claude_invoke", lambda *a, **k: pytest.fail("Claude was called"))

    out = ar.route_llm("codex", "review this", workdir=Path("/tmp"), agent="reviewer")

    assert out.success is True
    assert out.result_text == "codex ok"
    assert calls == [("review this", {"workdir": Path("/tmp"), "agent": "reviewer"})]


def test_agent_engine_reads_state_file_before_default():
    import agent_runner as ar

    target = ar.STATE_ROOT / "engines" / "batman"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("codex\n")

    assert ar.agent_engine("batman", default="hybrid") == "codex"


def test_agent_engine_env_override_wins(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_BATMAN_ENGINE", "claude")
    assert ar.agent_engine("batman", default="hybrid") == "claude"


def test_engine_preflight_bins_treats_hybrid_as_claude_first():
    import agent_runner as ar

    assert ar.engine_preflight_bins("claude") == [ar.CLAUDE_BIN]
    assert ar.engine_preflight_bins("codex") == [ar.CODEX_BIN]
    assert ar.engine_preflight_bins("hybrid") == [ar.CLAUDE_BIN]


def test_codex_sandbox_for_agent_honors_write_flag():
    import agent_runner as ar

    env = {"ALFRED_NIGHTWING_CODEX_WRITE": "1"}
    assert ar.codex_sandbox_for_agent("nightwing", environ=env) == "workspace-write"


def test_invoke_agent_engine_codex_skips_claude():
    import agent_runner as ar

    calls: list[str] = []
    extra_dir = Path("/tmp/source.git")

    def fake_claude(*args, **kwargs):
        calls.append("claude")
        pytest.fail("Claude was called")

    def fake_codex(*args, **kwargs):
        calls.append("codex")
        assert kwargs["sandbox"] == "workspace-write"
        assert kwargs["add_dirs"] == [extra_dir]
        return ar.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="codex-session",
            result_text="codex ok",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    out, engine_used = ar.invoke_agent_engine(
        "hi",
        engine="codex",
        agent="batman",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        codex_sandbox="workspace-write",
        codex_add_dirs=[extra_dir],
        claude_fn=fake_claude,
        codex_fn=fake_codex,
    )

    assert out.success is True
    assert out.result_text == "codex ok"
    assert engine_used == "codex"
    assert calls == ["codex"]


def test_invoke_agent_engine_hybrid_transient_retries_claude_no_fallback(monkeypatch):
    """A provider rate-limit is TRANSIENT: retry the SAME engine, never
    burn the codex fallback. This is the core behavior change of the
    reliability foundation."""
    import agent_runner as ar

    # One bounded retry on claude, no real sleeps.
    monkeypatch.setenv("ALFRED_TRANSIENT_MAX_RETRIES", "1")

    calls: list[str] = []
    fallback_seen: list[str] = []

    def fake_claude(*args, **kwargs):
        calls.append("claude")
        return ar.ClaudeResult(
            success=False,
            subtype="error_rate_limit",
            num_turns=2,
            cost_usd=0.0,
            session_id="claude-session",
            result_text="limit",
            raw={},
            stop_reason="error",
            error_message="limit",
        )

    def fake_codex(*args, **kwargs):  # pragma: no cover - must not run
        calls.append("codex")
        raise AssertionError("codex fallback must not fire on a transient failure")

    out, engine_used = ar.invoke_agent_engine(
        "hi",
        engine="hybrid",
        agent="batman",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        codex_timeout=45,
        claude_fn=fake_claude,
        codex_fn=fake_codex,
        on_fallback=lambda result: fallback_seen.append(result.subtype),
    )

    # claude was retried once (2 total) and the transient failure is surfaced.
    assert out.subtype == "error_rate_limit"
    assert engine_used == "claude"
    assert calls == ["claude", "claude"]
    assert fallback_seen == []


def test_invoke_agent_engine_hybrid_falls_back_on_capability_gap():
    """The fallback fires ONLY on a capability failure (engine ran but
    produced nothing useful)."""
    import agent_runner as ar

    calls: list[str] = []
    fallback_seen: list[str] = []

    def fake_claude(*args, **kwargs):
        calls.append("claude")
        return ar.ClaudeResult(
            success=False,
            subtype="error_max_turns",
            num_turns=999,
            cost_usd=0.0,
            session_id="claude-session",
            result_text="ran out of turns with no result",
            raw={},
            stop_reason="error",
            error_message="max turns",
        )

    def fake_codex(*args, **kwargs):
        calls.append("codex")
        assert kwargs["timeout"] == 45
        return ar.ClaudeResult(
            success=True,
            subtype="success",
            num_turns=1,
            cost_usd=0.0,
            session_id="codex-session",
            result_text="codex ok",
            raw={},
            stop_reason="end_turn",
            error_message=None,
        )

    out, engine_used = ar.invoke_agent_engine(
        "hi",
        engine="hybrid",
        agent="batman",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        codex_timeout=45,
        claude_fn=fake_claude,
        codex_fn=fake_codex,
        on_fallback=lambda result: fallback_seen.append(result.subtype),
    )

    assert out.success is True
    assert out.result_text == "codex ok"
    assert engine_used == "codex-fallback"
    assert out.fallback_from_subtype == "error_max_turns"
    assert calls == ["claude", "codex"]
    assert fallback_seen == ["error_max_turns"]


def test_invoke_agent_engine_hybrid_auth_is_fatal_no_fallback():
    """A Claude auth failure is FATAL: surface honestly, never burn the
    codex fallback. The credentials remedy is the #291 preflight's job."""
    import agent_runner as ar

    calls: list[str] = []
    fallback_seen: list[str] = []

    def fake_claude(*args, **kwargs):
        calls.append("claude")
        return ar.ClaudeResult(
            success=False,
            subtype="error_authentication",
            num_turns=1,
            cost_usd=0.0,
            session_id=None,
            result_text="401 invalid authentication credentials",
            raw={},
            stop_reason="error",
            error_message="401 invalid authentication credentials",
        )

    def fake_codex(*args, **kwargs):  # pragma: no cover - must not run
        calls.append("codex")
        raise AssertionError("codex fallback must not fire on a fatal auth failure")

    out, engine_used = ar.invoke_agent_engine(
        "hi",
        engine="hybrid",
        agent="robin",
        firing_id="f1",
        workdir=Path("/tmp"),
        claude_allowed_tools="Read",
        timeout=30,
        claude_fn=fake_claude,
        codex_fn=fake_codex,
        on_fallback=lambda result: fallback_seen.append(result.subtype),
    )

    assert out.success is False
    assert out.subtype == "error_authentication"
    assert engine_used == "claude"
    assert calls == ["claude"]
    assert fallback_seen == []


def test_codex_invoke_rejects_unsupported_claude_controls():
    import agent_runner as ar

    out = ar.codex_invoke(
        "review",
        workdir=Path("/tmp"),
        agent="reviewer",
        allowed_tools="Read",
        max_turns=5,
        resume_session="abc",
    )

    assert out.success is False
    assert out.stop_reason == "error"
    msg = (out.error_message or "").lower()
    assert "allowed_tools" in msg
    assert "max_turns" in msg
    assert "resume_session" in msg


def test_codex_invoke_reads_last_message_and_writes_artifacts(tmp_path, monkeypatch):
    import agent_runner as ar
    from agent_runner import process as process_mod

    root = tmp_path / "codex"
    commands = []

    def fake_run(cmd, *, cwd=None, timeout=None, capture=None, env=None, input_text=None):
        commands.append(cmd)
        last_path = Path(cmd[cmd.index("--output-last-message") + 1])
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text("Codex review body")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="session id: codex-session-1\ntokens used\n1,234\n",
            stderr="",
        )

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", root)
    monkeypatch.setattr(process_mod, "_popen_run_text", fake_run)

    out = ar.codex_invoke(
        "review",
        workdir=tmp_path,
        agent="reviewer",
        firing_id="fire-1",
        timeout=30,
        sandbox="read-only",
    )

    assert out.success is True
    assert out.result_text == "Codex review body"
    assert out.session_id == "codex-session-1"
    assert out.raw["tokens_used"] == 1234
    assert out.raw["sandbox"] == "read-only"
    assert Path(out.raw["stdout_path"]).read_text().startswith("session id:")
    assert out.raw["last_message_path"].endswith("fire-1.last.md")
    assert "--skip-git-repo-check" in commands[0]


def test_codex_invoke_can_bypass_approvals_and_sandbox(tmp_path, monkeypatch):
    import agent_runner as ar
    from agent_runner import process as process_mod

    root = tmp_path / "codex"
    commands = []

    def fake_run(cmd, *, cwd=None, timeout=None, capture=None, env=None, input_text=None):
        commands.append(cmd)
        last_path = Path(cmd[cmd.index("--output-last-message") + 1])
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text("Codex implementation body")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", root)
    monkeypatch.setattr(process_mod, "_popen_run_text", fake_run)

    out = ar.codex_invoke(
        "implement",
        workdir=tmp_path,
        agent="lucius",
        firing_id="fire-1",
        timeout=30,
        sandbox="workspace-write",
        bypass_approvals_and_sandbox=True,
    )

    cmd = commands[0]
    assert out.success is True
    assert out.raw["sandbox"] == "danger-full-access"
    assert out.raw["bypass_approvals_and_sandbox"] is True
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--sandbox" not in cmd


def test_codex_invoke_usage_limit_gets_rate_limit_subtype(tmp_path, monkeypatch):
    import agent_runner as ar
    from agent_runner import process as process_mod

    def fake_run(cmd, *, cwd=None, timeout=None, capture=None, env=None, input_text=None):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", tmp_path / "codex")
    monkeypatch.setattr(process_mod, "_popen_run_text", fake_run)

    out = ar.codex_invoke("review", workdir=tmp_path, agent="reviewer", firing_id="fire-1")

    assert out.success is False
    assert out.subtype == "error_rate_limit"
    assert out.stop_reason == "error"


@pytest.mark.parametrize(
    "stderr",
    [
        "API Error: 429 Too Many Requests",
        "provider quota exceeded for this account",
        '{"type":"error","message":"rate_limit_exceeded"}',
    ],
)
def test_codex_invoke_provider_limits_get_rate_limit_subtype(tmp_path, monkeypatch, stderr):
    import agent_runner as ar
    from agent_runner import process as process_mod

    def fake_run(cmd, *, cwd=None, timeout=None, capture=None, env=None, input_text=None):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", tmp_path / "codex")
    monkeypatch.setattr(process_mod, "_popen_run_text", fake_run)

    out = ar.codex_invoke("review", workdir=tmp_path, agent="reviewer", firing_id="fire-1")

    assert out.success is False
    assert out.subtype == "error_rate_limit"
    assert out.stop_reason == "error"


def test_codex_invoke_timeout_preserves_partial_artifacts(tmp_path, monkeypatch):
    import agent_runner as ar
    from agent_runner import process as process_mod

    root = tmp_path / "codex"

    def fake_run(cmd, *, cwd=None, timeout=None, capture=None, env=None, input_text=None):
        last_path = Path(cmd[cmd.index("--output-last-message") + 1])
        last_path.parent.mkdir(parents=True, exist_ok=True)
        last_path.write_text("partial final message")
        return subprocess.CompletedProcess(
            cmd,
            124,
            stdout="session id: codex-timeout-1\npartial stdout",
            stderr="partial stderr",
        )

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", root)
    monkeypatch.setattr(process_mod, "_popen_run_text", fake_run)

    out = ar.codex_invoke("review", workdir=tmp_path, agent="reviewer", firing_id="fire-1")

    assert out.success is False
    assert out.subtype == "error_timeout"
    assert out.session_id == "codex-timeout-1"
    assert out.result_text == "partial final message"
    assert out.raw["returncode"] == 124
    assert Path(out.raw["stdout_path"]).read_text() == "session id: codex-timeout-1\npartial stdout"
    assert Path(out.raw["stderr_path"]).read_text() == "partial stderr"


def test_get_tier_from_labels_accepts_codex():
    import agent_runner as ar

    assert ar.get_tier_from_labels([{"name": "llm-tier:codex"}]) == "codex"


# ---------- Slack severity routing ----------


def test_slack_post_default_severity_is_info(monkeypatch):
    """Existing callers passing no kwarg keep their previous behaviour."""
    import agent_runner as ar

    sent: list[str] = []
    monkeypatch.setattr(
        ar, "_post_slack_webhook", lambda hook, text: (sent.append(text), True)[1], raising=False
    )


def test_slack_post_warn_prefix(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    sent: list[str] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, *a, **kw):
        sent.append(req.data.decode("utf-8") if hasattr(req, "data") else "")
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert ar.slack_post("the build broke", severity="warn") is True
    assert sent, "no payload was posted"
    payload = json.loads(sent[-1])
    assert "⚠️" in payload["text"]
    assert "the build broke" in payload["text"]


def test_slack_post_alert_prefix_and_here_ping(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    sent: list[str] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, *a, **kw):
        sent.append(req.data.decode("utf-8") if hasattr(req, "data") else "")
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert ar.slack_post("staging is down", severity="alert") is True
    assert sent, "no payload was posted"
    payload = json.loads(sent[-1])
    assert "🚨" in payload["text"]
    assert "<!here>" in payload["text"]
    assert "staging is down" in payload["text"]


def test_slack_post_alert_does_not_double_prefix(monkeypatch):
    """An alert text that already starts with 🚨 or has <!here> isn't double-prefixed."""
    import agent_runner as ar

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    sent: list[str] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, *a, **kw):
        sent.append(req.data.decode("utf-8") if hasattr(req, "data") else "")
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ar.slack_post("🚨 already prefixed", severity="alert")
    payload = json.loads(sent[-1])
    # Exactly one 🚨 at the start (no double prefix)
    assert payload["text"].count("🚨") == 1


def test_slack_post_unknown_severity_falls_back_to_info(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example.test/x")
    sent: list[str] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, *a, **kw):
        sent.append(req.data.decode("utf-8") if hasattr(req, "data") else "")
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ar.slack_post("just a status", severity="not-a-real-tier")
    payload = json.loads(sent[-1])
    # Fell back to info: no prefix, no <!here>
    assert "🚨" not in payload["text"]
    assert "⚠️" not in payload["text"]
    assert "<!here>" not in payload["text"]


# ---------- Repo pause / resume ----------


def test_repo_pause_resume_roundtrip(tmp_path, monkeypatch):
    import agent_runner as ar

    assert ar.list_paused_repos() == []
    assert ar.is_repo_paused("backend") is False

    out = ar.set_repo_paused("backend", paused=True)
    assert out == ["backend"]
    assert ar.is_repo_paused("backend") is True
    assert ar.list_paused_repos() == ["backend"]

    out = ar.set_repo_paused("frontend", paused=True)
    assert sorted(out) == ["backend", "frontend"]

    out = ar.set_repo_paused("backend", paused=False)
    assert out == ["frontend"]
    assert ar.is_repo_paused("backend") is False
    assert ar.is_repo_paused("frontend") is True


def test_is_repo_paused_fail_open_on_missing_file(monkeypatch):
    """If the paused-repos file doesn't exist, is_repo_paused returns False."""
    import agent_runner as ar

    # Ensure the file does not exist
    if ar.PAUSED_REPOS_FILE.exists():
        ar.PAUSED_REPOS_FILE.unlink()
    assert ar.is_repo_paused("anything") is False


def test_is_repo_paused_fail_open_on_corrupt_file(tmp_path):
    """A garbage paused-repos file is treated as 'no repos paused' (fail-open)."""
    import agent_runner as ar

    ar.PAUSED_REPOS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ar.PAUSED_REPOS_FILE.write_text("{not json")
    assert ar.is_repo_paused("backend") is False


# ---------- Claim comment parsing ----------


def test_parse_claim_comment_round_trip():
    import agent_runner as ar

    body = "<!-- agent-claim:codename=lucius firing_id=20260501-194217-643a ts=2026-05-01T19:42:33Z -->"
    meta = ar._parse_claim_comment(body)
    assert meta["codename"] == "lucius"
    assert meta["firing_id"] == "20260501-194217-643a"
    assert meta["ts"] == "2026-05-01T19:42:33Z"


def test_parse_release_comment_carries_outcome():
    import agent_runner as ar

    body = "<!-- agent-release:codename=lucius firing_id=abc outcome=success pr=https://github.com/foo/bar/pull/42 ts=2026-05-01T20:00:00Z -->"
    meta = ar._parse_claim_comment(body)
    assert meta["codename"] == "lucius"
    assert meta["outcome"] == "success"
    assert meta["pr"] == "https://github.com/foo/bar/pull/42"


def test_force_release_stale_claim_preserves_original_claim_identity(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: comments.append(a) or True)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )
    monkeypatch.setattr(ar, "now_iso", lambda: "2026-05-09T10:00:00Z")

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="firing-1",
    )

    assert edits == [
        (
            ("myrepo", 42),
            {"add_labels": ["agent:implement"], "remove_labels": ["agent:in-flight"]},
        )
    ]
    assert comments == [
        (
            "myrepo",
            42,
            "<!-- agent-release:codename=lucius firing_id=firing-1 "
            "outcome=stale-swept swept_by=sweep-1 ts=2026-05-09T10:00:00Z -->",
        )
    ]


def test_force_release_stale_claim_reports_comment_failure(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a, **kw: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert not ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="firing-1",
    )


def test_force_release_stale_claim_keeps_fresh_in_flight(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: comments.append(a) or True)
    monkeypatch.setenv("ALFRED_CLAIM_MAX_AGE_HOURS", "4")
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [
                {
                    "createdAt": "2026-05-09T13:34:44Z",
                    "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
                },
                {
                    "createdAt": "2099-01-01T00:00:00Z",
                    "body": "<!-- agent-claim:codename=batman firing_id=new-fid ts=2099-01-01T00:00:00Z -->",
                },
            ],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="old-fid",
    )
    assert edits == []
    assert comments and "codename=lucius" in comments[0][2]


def test_force_release_stale_claim_uses_sweep_age_window(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    staleish_ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: comments.append(a) or True)
    monkeypatch.setenv("ALFRED_CLAIM_MAX_AGE_HOURS", "4")
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [
                {
                    "createdAt": "2026-05-09T13:34:44Z",
                    "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
                },
                {
                    "createdAt": staleish_ts,
                    "body": f"<!-- agent-claim:codename=batman firing_id=staleish-fid ts={staleish_ts} -->",
                },
            ],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="old-fid",
        max_age_hours=1,
    )
    assert edits == [
        (
            ("myrepo", 42),
            {"add_labels": ["agent:implement"], "remove_labels": ["agent:in-flight"]},
        )
    ]
    assert comments and "codename=lucius" in comments[0][2]


def test_force_release_stale_claim_does_not_keep_malformed_timestamp(monkeypatch):
    import agent_runner as ar

    edits = []
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: True)
    monkeypatch.setenv("ALFRED_CLAIM_MAX_AGE_HOURS", "4")
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [
                {
                    "createdAt": "2026-05-09T13:34:44Z",
                    "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
                },
                {
                    "createdAt": "not-a-date",
                    "body": "<!-- agent-claim:codename=batman firing_id=broken-fid ts=not-a-date -->",
                },
            ],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="old-fid",
    )
    assert edits == [
        (
            ("myrepo", 42),
            {"add_labels": ["agent:implement"], "remove_labels": ["agent:in-flight"]},
        )
    ]


def test_force_release_stale_claim_label_drift_keeps_in_flight(monkeypatch):
    import agent_runner as ar

    edits = []
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: True)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [
                {
                    "createdAt": "2026-05-09T13:34:44Z",
                    "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
                }
            ],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="old-fid",
        label_drift=True,
    )
    assert edits == []


def test_force_release_stale_claim_missing_claim_strips_in_flight(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a: comments.append(a) or True)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "comments": [],
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
        },
    )

    assert ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="?",
        released_firing_id="?",
    )
    assert comments == []
    assert edits == [
        (
            ("myrepo", 42),
            {"add_labels": ["agent:implement"], "remove_labels": ["agent:in-flight"]},
        )
    ]


def test_stale_unreleased_claim_comment_does_not_block_forever(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_CLAIM_MAX_AGE_HOURS", "4")
    comments_data = [
        {
            "createdAt": "2026-05-09T13:34:44Z",
            "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
        },
        {
            "createdAt": "2026-05-28T19:17:08Z",
            "body": "<!-- agent-claim:codename=batman firing_id=new-fid ts=2026-05-28T19:17:07Z -->",
        },
    ]
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {"comments": comments_data, "labels": [], "state": "OPEN"},
    )

    assert ar._detect_contested_claim("myrepo", 42, codename="batman", firing_id="new-fid") is None


def test_fresh_unreleased_claim_still_wins_race(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("ALFRED_CLAIM_MAX_AGE_HOURS", "4")
    comments_data = [
        {
            "createdAt": "2099-01-01T00:00:00Z",
            "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2099-01-01T00:00:00Z -->",
        },
        {
            "createdAt": "2099-01-01T00:01:00Z",
            "body": "<!-- agent-claim:codename=batman firing_id=new-fid ts=2099-01-01T00:01:00Z -->",
        },
    ]
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {"comments": comments_data, "labels": [], "state": "OPEN"},
    )

    assert (
        ar._detect_contested_claim("myrepo", 42, codename="batman", firing_id="new-fid")
        == "lucius:old-fid"
    )


def test_find_stale_claims_catches_label_drift(monkeypatch):
    import agent_runner as ar

    def fake_gh_json(cmd, default=None):
        if "--label" in cmd and cmd[cmd.index("--label") + 1] == "agent:implement":
            return [{"number": 42, "title": "stale drift"}]
        return []

    monkeypatch.setattr(ar, "GH_ORG", "example")
    monkeypatch.setattr(ar, "gh_json", fake_gh_json)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "state": "OPEN",
            "labels": [{"name": "agent:implement"}],
            "comments": [
                {
                    "createdAt": "2026-05-09T13:34:44Z",
                    "body": "<!-- agent-claim:codename=lucius firing_id=old-fid ts=2026-05-09T13:34:43Z -->",
                },
            ],
        },
    )

    stale = ar.find_stale_claims("myrepo", max_age_hours=4)
    assert len(stale) == 1
    assert stale[0]["repo"] == "myrepo"
    assert stale[0]["number"] == 42
    assert stale[0]["title"] == "stale drift"
    assert stale[0]["codename"] == "lucius"
    assert stale[0]["firing_id"] == "old-fid"
    assert stale[0]["max_age_hours"] == 4
    assert stale[0]["label_drift"] is True


def test_claim_issue_rolls_back_when_claim_comment_fails(monkeypatch):
    import agent_runner as ar

    edits = []
    monkeypatch.setattr(ar, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "labels": [{"name": "agent:implement"}],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    monkeypatch.setattr(ar, "ensure_labels", lambda *a, **kw: None)
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a, **kw: False)

    assert not ar.claim_issue("myrepo", 42, codename="lucius", firing_id="fid-1")
    assert edits == [
        (
            ("myrepo", 42),
            {"add_labels": ["agent:in-flight"], "remove_labels": ["agent:implement"]},
        ),
        (
            ("myrepo", 42),
            {"add_labels": ["agent:implement"], "remove_labels": ["agent:in-flight"]},
        ),
    ]


def test_claim_issue_refuses_product_label_for_lucius_claim(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "labels": [{"name": "agent:implement"}, {"name": "feature"}],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    monkeypatch.setattr(
        ar,
        "gh_issue_edit",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not edit")),
    )

    assert not ar.claim_issue(
        "myrepo",
        63,
        codename="custom-feature-dev",
        firing_id="fid-1",
        role="feature-dev",
    )


def test_claim_issue_allows_robin_promoted_product_label_for_feature_dev(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    monkeypatch.setattr(ar, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "labels": [
                {"name": "agent:implement"},
                {"name": "feature"},
                {"name": "severity:p2"},
            ],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    monkeypatch.setattr(ar, "ensure_labels", lambda *a, **kw: None)
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a, **kw: comments.append((a, kw)) or True)
    monkeypatch.setattr(ar, "_detect_contested_claim", lambda *a, **kw: None)

    assert ar.claim_issue(
        "myrepo",
        64,
        codename="custom-feature-dev",
        firing_id="fid-1",
        role="feature-dev",
    )
    assert edits == [
        (
            ("myrepo", 64),
            {"add_labels": ["agent:in-flight"], "remove_labels": ["agent:implement"]},
        )
    ]
    assert comments


def test_claim_issue_refuses_large_feature_for_feature_dev_role(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "labels": [
                {"name": "agent:implement"},
                {"name": "agent:large-feature"},
                {"name": "severity:p2"},
            ],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    monkeypatch.setattr(
        ar,
        "gh_issue_edit",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not edit")),
    )

    assert not ar.claim_issue(
        "myrepo",
        65,
        codename="custom-feature-dev",
        firing_id="fid-1",
        role="feature-dev",
    )


def test_claim_issue_allows_bundle_labels_for_batman_claim(monkeypatch):
    import agent_runner as ar

    edits = []
    comments = []
    monkeypatch.setattr(ar, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(
        ar,
        "_issue_state",
        lambda repo, num: {
            "labels": [
                {"name": "agent:implement"},
                {"name": "agent:bundle:checkout"},
                {"name": "agent:plan-pending-approval"},
                {"name": "enhancement"},
            ],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    monkeypatch.setattr(ar, "ensure_labels", lambda *a, **kw: None)
    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: edits.append((a, kw)) or True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a, **kw: comments.append((a, kw)) or True)
    monkeypatch.setattr(ar, "_detect_contested_claim", lambda *a, **kw: None)

    assert ar.claim_issue("myrepo", 62, codename="batman", firing_id="fid-1")
    assert edits == [
        (
            ("myrepo", 62),
            {"add_labels": ["agent:in-flight"], "remove_labels": ["agent:implement"]},
        )
    ]
    assert comments


def test_release_issue_reports_comment_failure(monkeypatch):
    import agent_runner as ar

    monkeypatch.setattr(ar, "gh_issue_edit", lambda *a, **kw: True)
    monkeypatch.setattr(ar, "gh_issue_comment", lambda *a, **kw: False)

    assert not ar.release_issue(
        "myrepo",
        42,
        codename="lucius",
        firing_id="fid-1",
        outcome="success",
    )


# ---------- issue_dedup_check (with mocked gh) ----------


def test_issue_dedup_check_claimable_when_open_and_no_blockers(monkeypatch):
    monkeypatch.setenv("GH_ORG", "myorg")
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar2

    monkeypatch.setattr(
        ar2,
        "_issue_state",
        lambda repo, num: {
            "labels": [{"name": "agent:implement"}],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    out = ar2.issue_dedup_check("myrepo", 42)
    assert out["claimable"] is True
    assert out["in_flight"] is False
    assert out["pr_open"] is False


def test_issue_dedup_check_blocks_when_in_flight(monkeypatch):
    monkeypatch.setenv("GH_ORG", "myorg")
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar2

    monkeypatch.setattr(
        ar2,
        "_issue_state",
        lambda repo, num: {
            "labels": [{"name": "agent:in-flight"}],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    out = ar2.issue_dedup_check("myrepo", 42)
    assert out["claimable"] is False
    assert out["in_flight"] is True


def test_issue_dedup_check_blocks_on_repo_pause(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_ORG", "myorg")
    for m in list(sys.modules):
        if m == "agent_runner":
            del sys.modules[m]
    import agent_runner as ar2

    ar2.set_repo_paused("myrepo", paused=True)
    monkeypatch.setattr(
        ar2,
        "_issue_state",
        lambda repo, num: {
            "labels": [{"name": "agent:implement"}],
            "state": "OPEN",
            "comments": [],
            "number": num,
        },
    )
    out = ar2.issue_dedup_check("myrepo", 42)
    assert out["claimable"] is False
    assert out["repo_paused"] is True


def test_run_helper_coerces_bytes_stdout_on_timeout(monkeypatch, tmp_path):
    """The process wrapper decodes bytes captured from timeout exceptions."""
    for m in list(sys.modules):
        if m == "agent_runner" or m.startswith("agent_runner."):
            del sys.modules[m]
    from agent_runner import process as process_mod

    class FakePopen:
        pid = 999999
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(
                    cmd=["fake"],
                    timeout=1,
                    output=b"partial-bytes-output",
                    stderr=b"",
                )
            return "", ""

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 124
            return self.returncode

    monkeypatch.setattr(process_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(process_mod, "_terminate_process_group", lambda proc: None)

    res = process_mod._popen_run_text(["any"], timeout=1)

    assert res.returncode == 124
    assert isinstance(res.stdout, str)
    assert "partial-bytes-output" in res.stdout
    # ``Path.write_text`` must accept this without TypeError (the original
    # crash site in rasalghul.py:295).
    target = tmp_path / "diff.txt"
    target.write_text(res.stdout)
    assert target.read_text() == "partial-bytes-output"
