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
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="test", env_vars=["HERMES_HOME"])
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


def test_preflight_suppresses_slack_when_not_under_launchd(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.delenv("ALFRED_PREFLIGHT_FORCE_SLACK", raising=False)
    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["HERMES_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_suppresses_slack_when_launchd_env_is_placeholder(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "0")
    monkeypatch.delenv("ALFRED_PREFLIGHT_FORCE_SLACK", raising=False)
    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["HERMES_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert posted == []


def test_preflight_posts_slack_under_launchd(monkeypatch):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "alfred.lucius")
    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["HERMES_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert len(posted) == 1
    assert "lucius preflight failed" in posted[0]


@pytest.mark.parametrize("doctor_value", ["0", "false", "no", "off"])
def test_preflight_posts_slack_when_doctor_env_is_false(monkeypatch, doctor_value):
    import agent_runner as ar

    monkeypatch.setenv("XPC_SERVICE_NAME", "alfred.lucius")
    monkeypatch.setenv("HERMES_DOCTOR", doctor_value)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["HERMES_HOME"])
    with pytest.raises(ar.PreflightFailed):
        ar.preflight(spec)
    assert len(posted) == 1


def test_preflight_force_slack_env_overrides_manual_suppression(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    monkeypatch.setenv("ALFRED_PREFLIGHT_FORCE_SLACK", "1")
    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    posted: list[str] = []
    monkeypatch.setattr(ar, "slack_post", lambda msg, *a, **kw: posted.append(msg))

    spec = ar.PreflightSpec(agent="lucius", env_vars=["HERMES_HOME"])
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


def test_doctor_mode_default_false(monkeypatch):
    import agent_runner as ar

    monkeypatch.delenv("HERMES_DOCTOR", raising=False)
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

    root = tmp_path / "codex"

    def fake_run(cmd, input=None, cwd=None, timeout=None, capture_output=None, text=None):
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
    monkeypatch.setattr(ar.subprocess, "run", fake_run)

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


def test_codex_invoke_usage_limit_gets_rate_limit_subtype(tmp_path, monkeypatch):
    import agent_runner as ar

    def fake_run(cmd, input=None, cwd=None, timeout=None, capture_output=None, text=None):
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="ERROR: You've hit your usage limit. Try again later.",
        )

    monkeypatch.setattr(ar, "CODEX_TRANSCRIPTS_ROOT", tmp_path / "codex")
    monkeypatch.setattr(ar.subprocess, "run", fake_run)

    out = ar.codex_invoke("review", workdir=tmp_path, agent="reviewer", firing_id="fire-1")

    assert out.success is False
    assert out.subtype == "error_rate_limit"
    assert out.stop_reason == "error"


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

    assert not ar.force_release_stale_claim(
        "myrepo",
        42,
        sweep_id="sweep-1",
        released_codename="lucius",
        released_firing_id="firing-1",
    )


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
