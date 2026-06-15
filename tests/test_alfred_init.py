"""Tests for alfred-init wizard helpers.

Covers the deterministic, side-effect-free helpers:
    - discover_agents (filesystem walk over bin/*.py + .sh)
    - render_agents_conf (TSV emission)
    - env_assignments_for (per-role env-var map)
    - read_alfredrc / upsert_alfredrc (idempotent rc append)
    - _resolve_repo_selection (repo selection grammar)
    - main() with ALFRED_DOCTOR=1 (short-circuit sentinel)

The interactive prompt path, subprocess shells, and Slack/AWS HTTP calls
are NOT exercised, those need a live operator + accounts.

Run via `pytest tests/test_alfred_init.py`.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loader, bin/alfred-init.py has a hyphen, so import via spec.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def init_mod():
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "bin" / "alfred-init.py"
    spec = importlib.util.spec_from_file_location("alfred_init", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_init"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# discover_agents
# ---------------------------------------------------------------------------


def test_discover_agents_empty(tmp_path, init_mod):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    assert init_mod.discover_agents(bin_dir) == []


def test_discover_agents_finds_known_codenames(tmp_path, init_mod):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "lucius.py").write_text("# lucius runner\n")
    (bin_dir / "drake.py").write_text("# drake runner\n")
    (bin_dir / "batman.py").write_text("# batman runner\n")
    (bin_dir / "unknown.py").write_text("# not in catalog\n")
    out = init_mod.discover_agents(bin_dir)
    assert "feature_dev" in out
    assert "planner" in out
    assert "cross_repo_coordinator" in out
    # Catalog order is preserved: feature_dev < planner.
    assert out.index("feature_dev") < out.index("planner")


def test_discover_agents_handles_fleet_recap_sh(tmp_path, init_mod):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fleet-recap.sh").write_text("#!/bin/sh\n")
    out = init_mod.discover_agents(bin_dir)
    assert "fleet_recap_morning" in out
    assert "fleet_recap_evening" in out


def test_discover_agents_handles_scheduled_utilities(tmp_path, init_mod):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "fleet-doctor.py").write_text("# doctor\n")
    (bin_dir / "shipped-summary-daily.sh").write_text("#!/bin/sh\n")
    (bin_dir / "shipped-summary-weekly.sh").write_text("#!/bin/sh\n")
    out = init_mod.discover_agents(bin_dir)
    assert "fleet_doctor" in out
    assert "shipped_summary_daily" in out
    assert "shipped_summary_weekly" in out


def test_discover_agents_returns_empty_for_missing_dir(tmp_path, init_mod):
    assert init_mod.discover_agents(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# render_agents_conf
# ---------------------------------------------------------------------------


def _state_with(
    init_mod,
    tmp_path,
    *,
    roles=("feature_dev",),
    gh_org="acme",
    slack_url="",
    repos=None,
    codenames=None,
    schedules=None,
):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path / "repo",
        gh_org=gh_org,
        slack_webhook=slack_url,
    )
    state.enabled_roles = list(roles)
    if codenames:
        state.role_to_codename.update(codenames)
    if repos:
        state.role_to_repos.update(repos)
    if schedules:
        state.role_to_schedule.update(schedules)
    return state


def test_render_agents_conf_basic(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path, roles=("feature_dev", "planner"))
    text = init_mod.render_agents_conf(state)
    assert "alfred.lucius\tlucius.py\tinterval:1200\tno\talfred.lucius\tfeature dev" in text
    assert "alfred.drake\tdrake.py\tinterval:7200\tno\talfred.drake\tissue planner" in text
    # Header comment present.
    assert text.startswith("# agents.conf")


def test_render_agents_conf_fleet_recap_shares_script(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path, roles=("fleet_recap_morning", "fleet_recap_evening"))
    text = init_mod.render_agents_conf(state)
    # Both rows point at fleet-recap.sh and share the log stem.
    assert text.count("fleet-recap.sh") == 2
    # Both rows write to the same /tmp/alfred.fleet-recap.{stdout,stderr}.
    assert text.count("\talfred.fleet-recap\tfleet recap") == 2
    # The labels stay distinct so launchd doesn't collide them.
    assert "alfred.fleet-recap-morning\t" in text
    assert "alfred.fleet-recap-evening\t" in text


def test_render_agents_conf_schedules_health_and_shipped_reports(init_mod, tmp_path):
    state = _state_with(
        init_mod,
        tmp_path,
        roles=("fleet_doctor", "shipped_summary_daily", "shipped_summary_weekly"),
    )
    text = init_mod.render_agents_conf(state)
    assert "alfred.fleet-doctor\tfleet-doctor.py\tcron:7:30" in text
    assert "alfred.shipped-summary-daily\tshipped-summary-daily.sh\tcron:7:35" in text
    assert "alfred.shipped-summary-weekly\tshipped-summary-weekly.sh\tcron:1:7:35" in text
    assert text.count("\talfred.shipped-summary\tshipped summary") == 2


def test_render_agents_conf_custom_codename(init_mod, tmp_path):
    state = _state_with(
        init_mod, tmp_path, roles=("feature_dev",), codenames={"feature_dev": "robin-hood"}
    )
    text = init_mod.render_agents_conf(state)
    assert "alfred.robin-hood\tlucius.py" in text


def test_render_agents_conf_includes_batman(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path, roles=("cross_repo_coordinator",))
    text = init_mod.render_agents_conf(state)
    assert (
        "alfred.batman\tbatman.py\tinterval:3600\tno\talfred.batman\tcross-repo coordinator" in text
    )


# ---------------------------------------------------------------------------
# env_assignments_for
# ---------------------------------------------------------------------------


def test_env_assignments_includes_codenames_and_repos(init_mod, tmp_path):
    state = _state_with(
        init_mod,
        tmp_path,
        roles=("feature_dev",),
        repos={"feature_dev": ["acme/foo", "acme/bar"]},
    )
    out = init_mod.env_assignments_for(state)
    assert out["GH_ORG"] == "acme"
    assert out["AGENT_CODENAME_FEATURE_DEV"] == "lucius"
    assert out["ALFRED_LUCIUS_REPOS"] == "foo,bar"


def test_env_assignments_batman_uses_scan_repos(init_mod, tmp_path):
    state = _state_with(
        init_mod,
        tmp_path,
        roles=("cross_repo_coordinator",),
        repos={"cross_repo_coordinator": ["acme/api", "acme/web"]},
    )
    out = init_mod.env_assignments_for(state)
    assert out["AGENT_CODENAME_CROSS_REPO_COORDINATOR"] == "batman"
    assert out["BATMAN_SCAN_REPOS"] == "api,web"
    assert out["BATMAN_ROLLOUT_ORDER"] == "api,web"
    assert "ALFRED_BATMAN_REPOS" not in out


def test_env_assignments_slack_env(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path, slack_url="https://hooks.slack.com/services/X/Y/Z")
    state.slack_storage = "env"
    out = init_mod.env_assignments_for(state)
    assert out["SLACK_WEBHOOK_URL"].startswith("https://hooks.slack.com/")


def test_env_assignments_slack_aws(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path)
    state.slack_storage = "aws"
    state.aws_region = "us-east-2"
    out = init_mod.env_assignments_for(state)
    assert out["SLACK_WEBHOOK_SECRET_ID"] == "alfred/slack-webhook"
    assert out["SLACK_WEBHOOK_SECRET_REGION"] == "us-east-2"


def test_env_assignments_aws_profile_per_agent(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path, roles=("smoke_runner",))
    state.use_aws = True
    state.aws_agent_profiles = {"huntress": "huntress-cron"}
    out = init_mod.env_assignments_for(state)
    assert out["ALFRED_HUNTRESS_AWS_PROFILE"] == "huntress-cron"


# ---------------------------------------------------------------------------
# telemetry opt-in (off by default)
# ---------------------------------------------------------------------------


def test_env_assignments_telemetry_off_by_default(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path)
    out = init_mod.env_assignments_for(state)
    assert "ALFRED_TELEMETRY_ENABLED" not in out
    assert "ALFRED_TELEMETRY_URL" not in out


def test_env_assignments_telemetry_needs_both_flag_and_url(init_mod, tmp_path):
    # Opt-in flag set but no URL: still nothing written (reporter would no-op).
    state = _state_with(init_mod, tmp_path)
    state.telemetry_enabled = True
    state.telemetry_url = ""
    out = init_mod.env_assignments_for(state)
    assert "ALFRED_TELEMETRY_ENABLED" not in out


def test_env_assignments_telemetry_written_when_opted_in(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path)
    state.telemetry_enabled = True
    state.telemetry_url = "https://worker.example.com/ingest"
    out = init_mod.env_assignments_for(state)
    assert out["ALFRED_TELEMETRY_ENABLED"] == "1"
    assert out["ALFRED_TELEMETRY_URL"] == "https://worker.example.com/ingest"


def test_telemetry_step_non_interactive_stays_off(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path)
    init_mod.step_8b_telemetry(state, non_interactive=True)
    assert state.telemetry_enabled is False
    assert state.telemetry_url == ""


def test_config_override_telemetry_opt_in(init_mod, tmp_path):
    state = _state_with(init_mod, tmp_path)
    init_mod.apply_config_overrides(
        state,
        {"telemetry_enabled": True, "telemetry_url": "https://w.example.com/ingest"},
    )
    assert state.telemetry_enabled is True
    assert state.telemetry_url == "https://w.example.com/ingest"


# ---------------------------------------------------------------------------
# alfredrc IO
# ---------------------------------------------------------------------------


def test_read_alfredrc_missing_returns_empty(tmp_path, init_mod):
    assert init_mod.read_alfredrc(tmp_path / ".alfredrc") == {}


def test_read_alfredrc_parses_kv_and_strips_quotes(tmp_path, init_mod):
    rc = tmp_path / ".alfredrc"
    rc.write_text("# comment\nGH_ORG=acme\nexport OPERATOR_NAME=Alice\nQUOTED='hello world'\n\n")
    out = init_mod.read_alfredrc(rc)
    assert out["GH_ORG"] == "acme"
    assert out["OPERATOR_NAME"] == "Alice"
    assert out["QUOTED"] == "hello world"


def test_upsert_alfredrc_idempotent(tmp_path, init_mod):
    rc = tmp_path / ".alfredrc"
    rc.write_text("# pre-existing\nGH_ORG=acme\n")
    init_mod.upsert_alfredrc(rc, {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/A/B/C"})
    first = rc.read_text()
    init_mod.upsert_alfredrc(rc, {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/A/B/C"})
    second = rc.read_text()
    assert first == second
    # The pre-existing block survived.
    assert "GH_ORG=acme" in second
    # The marker block exists exactly once.
    assert second.count("alfred-init, generated") == 1


def test_upsert_alfredrc_updates_values(tmp_path, init_mod):
    rc = tmp_path / ".alfredrc"
    rc.write_text("GH_ORG=acme\n")
    init_mod.upsert_alfredrc(rc, {"FOO": "1"})
    init_mod.upsert_alfredrc(rc, {"FOO": "2"})
    text = rc.read_text()
    assert "FOO=1" not in text
    assert "FOO=2" in text


# ---------------------------------------------------------------------------
# _resolve_repo_selection
# ---------------------------------------------------------------------------


def test_resolve_repo_selection_all(init_mod):
    repos = ["acme/api", "acme/web", "acme/specs"]
    assert init_mod._resolve_repo_selection("all", repos) == repos
    assert init_mod._resolve_repo_selection("", repos) == repos


def test_resolve_repo_selection_engineering_excludes_specs_and_docs(init_mod):
    repos = ["acme/api", "acme/web", "acme/specs", "acme/docs", "acme/wiki"]
    out = init_mod._resolve_repo_selection("engineering", repos)
    assert "acme/api" in out
    assert "acme/web" in out
    assert "acme/specs" not in out
    assert "acme/docs" not in out
    assert "acme/wiki" not in out


def test_resolve_repo_selection_by_number(init_mod):
    repos = ["acme/api", "acme/web", "acme/mobile"]
    assert init_mod._resolve_repo_selection("1,3", repos) == ["acme/api", "acme/mobile"]


def test_resolve_repo_selection_by_name(init_mod):
    repos = ["acme/api", "acme/web"]
    assert init_mod._resolve_repo_selection("acme/web", repos) == ["acme/web"]
    assert init_mod._resolve_repo_selection("web", repos) == ["acme/web"]


def test_resolve_repo_selection_allows_external_repo_for_cli(init_mod):
    repos = ["acme/api"]
    out = init_mod._resolve_repo_selection(
        "other/web,worker", repos, gh_org="acme", allow_external=True
    )
    assert out == ["other/web", "acme/worker"]


def test_resolve_repo_selection_drops_garbage(init_mod):
    repos = ["acme/api"]
    assert init_mod._resolve_repo_selection("99,nope", repos) == []


# ---------------------------------------------------------------------------
# slack webhook regex
# ---------------------------------------------------------------------------


def test_slack_webhook_regex_accepts_real(init_mod):
    assert init_mod.SLACK_WEBHOOK_RE.match("https://hooks.slack.com/services/T0/B0/abc")


def test_slack_webhook_regex_rejects_other(init_mod):
    assert not init_mod.SLACK_WEBHOOK_RE.match("http://example.com/webhook")
    assert not init_mod.SLACK_WEBHOOK_RE.match("https://discord.com/services/foo")


# ---------------------------------------------------------------------------
# codename regex
# ---------------------------------------------------------------------------


def test_codename_regex(init_mod):
    assert init_mod.CODENAME_RE.match("lucius")
    assert init_mod.CODENAME_RE.match("agent-cleanup")
    assert init_mod.CODENAME_RE.match("a1")
    assert not init_mod.CODENAME_RE.match("Lucius")
    assert not init_mod.CODENAME_RE.match("1lucius")
    assert not init_mod.CODENAME_RE.match("with space")
    assert not init_mod.CODENAME_RE.match("with_underscore")


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


def test_load_config_round_trip(tmp_path, init_mod):
    cfg = tmp_path / "answers.json"
    cfg.write_text('{"gh_org": "acme", "agents": ["lucius"]}')
    loaded = init_mod.load_config(cfg)
    assert loaded == {"gh_org": "acme", "agents": ["lucius"]}


def test_apply_config_overrides(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    init_mod.apply_config_overrides(
        state,
        {
            "gh_org": "acme",
            "slack_webhook": "https://hooks.slack.com/services/X/Y/Z",
            "slack_storage": "aws",
            "use_aws": True,
            "aws_agent_profiles": {"huntress": "huntress-cron"},
            "agents": ["lucius", "drake"],
        },
    )
    assert state.gh_org == "acme"
    assert state.slack_storage == "aws"
    assert state.use_aws is True
    assert state.aws_agent_profiles == {"huntress": "huntress-cron"}
    assert "feature_dev" in state.enabled_roles
    assert "planner" in state.enabled_roles


def test_apply_config_overrides_role_repos_by_codename_and_role_key(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    init_mod.apply_config_overrides(
        state,
        {
            "role_repos": {
                "lucius": ["acme/api", "acme/web"],
                "feature_dev": ["acme/api", "acme/web"],  # role-key alias for the same role
                "drake": "acme/api",  # string singleton is accepted
                "BANE": ["acme/api"],  # case-insensitive
            },
        },
    )
    assert state.role_to_repos["feature_dev"] == ["acme/api", "acme/web"]
    assert state.role_to_repos["planner"] == ["acme/api"]
    assert state.role_to_repos["test_coverage"] == ["acme/api"]


def test_apply_config_overrides_role_codename_and_schedule(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    init_mod.apply_config_overrides(
        state,
        {
            "role_codename": {"feature_dev": "implementer"},
            "role_schedule": {"lucius": "interval:1800", "drake": "cron:7:30"},
        },
    )
    assert state.role_to_codename["feature_dev"] == "implementer"
    assert state.role_to_schedule["feature_dev"] == "interval:1800"
    assert state.role_to_schedule["planner"] == "cron:7:30"


def test_apply_config_overrides_ignores_unknown_agent_keys(init_mod, tmp_path, capsys):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    init_mod.apply_config_overrides(
        state,
        {
            "role_repos": {"not-a-real-agent": ["acme/api"]},
            "role_codename": {"also-not-real": "ghost"},
            "role_schedule": {"phantom": "interval:60"},
        },
    )
    assert "not-a-real-agent" not in state.role_to_repos
    assert "also-not-real" not in state.role_to_codename
    # All three warnings should hit stderr.
    err = capsys.readouterr().err
    assert "not-a-real-agent" in err
    assert "also-not-real" in err
    assert "phantom" in err


def test_apply_config_overrides_rejects_invalid_codename(init_mod, tmp_path, capsys):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    init_mod.apply_config_overrides(
        state, {"role_codename": {"feature_dev": "Bad Name With Spaces"}}
    )
    assert "feature_dev" not in state.role_to_codename
    assert "Bad Name With Spaces" in capsys.readouterr().err


def test_step_7_repos_preserves_role_repos_from_config(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
        gh_org="acme",
    )
    state.enabled_roles = ["feature_dev", "planner"]
    state.repos = ["acme/api", "acme/web", "acme/mobile"]
    state.role_to_repos["feature_dev"] = ["acme/api"]
    # planner has no preset; in non-interactive mode it must fall back to
    # repos_arg / state.repos default behaviour, not silently overwrite
    # feature_dev's preset.
    init_mod.step_7_repos(state, repos_arg="acme/web,acme/mobile", non_interactive=True)
    assert state.role_to_repos["feature_dev"] == ["acme/api"]
    assert state.role_to_repos["planner"] == ["acme/web", "acme/mobile"]


def test_step_6_codenames_preserves_config_overrides(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    state.enabled_roles = ["feature_dev", "planner"]
    state.role_to_codename["feature_dev"] = "implementer"
    init_mod.step_6_codenames(state, non_interactive=True)
    assert state.role_to_codename["feature_dev"] == "implementer"
    assert state.role_to_codename["planner"] == "drake"  # default fill


def test_step_6_codenames_fails_on_config_collision(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    state.enabled_roles = ["feature_dev", "planner"]
    state.role_to_codename["feature_dev"] = "drake"
    state.role_to_codename["planner"] = "drake"  # collision with feature_dev
    with pytest.raises(SystemExit):
        init_mod.step_6_codenames(state, non_interactive=True)


def test_step_8_schedule_preserves_config_overrides(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    state.enabled_roles = ["feature_dev", "planner"]
    state.role_to_schedule["feature_dev"] = "interval:1800"
    init_mod.step_8_schedule(state, non_interactive=True)
    assert state.role_to_schedule["feature_dev"] == "interval:1800"
    # Planner gets the catalog default.
    assert state.role_to_schedule["planner"] == init_mod.AGENT_CATALOG["planner"][3]


def test_pick_agents_keeps_configured_agents(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    state.enabled_roles = ["bug_triage"]
    init_mod.step_5_pick_agents(
        state,
        ["feature_dev", "planner", "bug_triage"],
        agents_arg=None,
        non_interactive=True,
    )
    assert state.enabled_roles == ["bug_triage"]


def test_pick_agents_lists_opt_in_marker(init_mod, tmp_path, capsys):
    """Issue #104: opt-in roles need a visible `(opt-in)` marker so operators
    can tell at a glance which agents need a follow-up `alfred enable` to fire."""
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
    )
    available = [
        "feature_dev",
        "planner",
        "bug_triage",
        "agent_cleanup",
        "cross_repo_coordinator",
    ]
    init_mod.step_5_pick_agents(state, available, agents_arg=None, non_interactive=True)
    out = capsys.readouterr().out
    assert "(opt-in)" in out, "Expected `(opt-in)` marker in the agent picker output"
    # Marker must sit on Batman's line and NOT on a starter line.
    batman_line = next(
        (line for line in out.splitlines() if "batman" in line and "(opt-in)" in line),
        "",
    )
    assert batman_line, "Expected the (opt-in) marker on Batman's row specifically"
    starter_lines = [
        line
        for line in out.splitlines()
        if any(name in line for name in ("lucius", "drake", "rasalghul"))
    ]
    assert starter_lines, "Expected at least one starter agent row in the output"
    for line in starter_lines:
        assert "(opt-in)" not in line, f"Starter agent row should not carry (opt-in): {line!r}"


def test_pick_agents_offers_batman_when_multi_repo(init_mod, tmp_path, capsys, monkeypatch):
    """Issue #104: multi-repo fleets should be offered Batman explicitly rather
    than relying on the operator to spot it in the catalog. Default-no preserves
    prior behaviour for operators who decline."""
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
        gh_org="acme",
    )
    state.repos = ["acme/frontend", "acme/backend", "acme/mobile"]
    available = ["feature_dev", "planner", "bug_triage", "cross_repo_coordinator"]

    # Capture every prompt input() saw and the operator's reply.
    answers_decline = iter(["", "n"])
    prompts_seen: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts_seen.append(prompt)
        return next(answers_decline)

    monkeypatch.setattr("builtins.input", fake_input)
    init_mod.step_5_pick_agents(state, available, agents_arg=None, non_interactive=False)

    out = capsys.readouterr().out
    assert "Your org has 3 visible repos" in out
    assert any("Add Batman to this fleet?" in p for p in prompts_seen), prompts_seen
    assert "cross_repo_coordinator" not in state.enabled_roles

    # Now the operator accepts.
    state2 = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
        gh_org="acme",
    )
    state2.repos = ["acme/frontend", "acme/backend"]
    answers_accept = iter(["", "y"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_accept))
    init_mod.step_5_pick_agents(state2, available, agents_arg=None, non_interactive=False)
    assert "cross_repo_coordinator" in state2.enabled_roles


def test_pick_agents_skips_batman_offer_for_single_repo(init_mod, tmp_path, capsys, monkeypatch):
    """No Batman nudge when the fleet has a single repo; it adds no value."""
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
        gh_org="acme",
    )
    state.repos = ["acme/api"]
    available = ["feature_dev", "planner", "bug_triage", "cross_repo_coordinator"]
    prompts_seen: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts_seen.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", fake_input)
    init_mod.step_5_pick_agents(state, available, agents_arg=None, non_interactive=False)
    out = capsys.readouterr().out
    assert "Add Batman to this fleet?" not in out
    assert not any("Add Batman to this fleet?" in p for p in prompts_seen), prompts_seen
    assert "cross_repo_coordinator" not in state.enabled_roles


def test_repos_arg_rejects_repos_outside_gh_org(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path,
        gh_org="acme",
    )
    state.enabled_roles = ["feature_dev"]
    state.repos = ["acme/api"]
    with pytest.raises(SystemExit):
        init_mod.step_7_repos(
            state,
            repos_arg="other/api",
            non_interactive=True,
        )


def test_noninteractive_single_repo_starter_main(monkeypatch, tmp_path, init_mod):
    repo_root = tmp_path / "repo"
    bin_dir = repo_root / "bin"
    prompts_dir = repo_root / "prompts"
    bin_dir.mkdir(parents=True)
    prompts_dir.mkdir()
    for name in ["lucius.py", "drake.py", "rasalghul.py", "agent-cleanup.py"]:
        (bin_dir / name).write_text("# runner\n")
    for name in ["feature-dev.md", "planner.md", "code-review.md"]:
        (prompts_dir / name).write_text(f"{name} template\n")
    (repo_root / "deploy.sh").write_text("#!/bin/sh\n")
    (repo_root / "launchd").mkdir()
    (bin_dir / "doctor.sh").write_text("#!/bin/sh\n")

    alfred_home = tmp_path / "alfred"
    alfred_home.mkdir()
    alfredrc = tmp_path / ".alfredrc"
    alfredrc.write_text("GH_ORG=acme\n")
    monkeypatch.setenv("ALFRED_HOME", str(alfred_home))
    monkeypatch.setenv("ALFREDRC", str(alfredrc))
    monkeypatch.delenv("ALFRED_NONINTERACTIVE", raising=False)
    monkeypatch.delenv("ALFRED_DOCTOR", raising=False)

    label_repos: list[str] = []
    subprocesses: list[tuple[str, ...]] = []

    def fake_have(_name):
        return True

    def fake_run(cmd, **_kwargs):
        subprocesses.append(tuple(str(part) for part in cmd))
        if cmd[:2] == ["claude", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1.0.0\n", stderr="")
        if cmd[:2] == ["claude", "-p"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="hi\n", stderr="")
        if cmd[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["gh", "repo", "list"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='[{"nameWithOwner":"acme/palette"}]', stderr=""
            )
        if cmd[:3] == ["gh", "label", "create"]:
            label_repos.append(cmd[cmd.index("-R") + 1])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "bash":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(init_mod, "have", fake_have)
    monkeypatch.setattr(init_mod, "run", fake_run)

    rc = init_mod.main(
        [
            "--repo-root",
            str(repo_root),
            "--non-interactive",
            "--agents",
            "starter",
            "--repos",
            "acme/palette",
            "--slack-webhook",
            "skip",
        ]
    )

    assert rc == 0
    generated_rc = alfredrc.read_text()
    assert "ALFRED_LUCIUS_REPOS=palette\n" in generated_rc
    assert "ALFRED_DRAKE_REPOS=palette\n" in generated_rc
    assert "ALFRED_RASALGHUL_REPOS=palette\n" in generated_rc
    assert "acme/palette" in set(label_repos)
    assert (alfred_home / "prompts" / "lucius.md").exists()
    assert (alfred_home / "prompts" / "drake.md").exists()
    assert (alfred_home / "prompts" / "rasalghul.md").exists()
    assert any(cmd[0] == "bash" and cmd[1].endswith("deploy.sh") for cmd in subprocesses)
    assert any(cmd[0] == "bash" and cmd[1].endswith("doctor.sh") for cmd in subprocesses)


def test_starter_roles_and_agents_arg(init_mod):
    available = ["feature_dev", "planner", "cross_repo_coordinator", "pr_review", "agent_cleanup"]
    assert init_mod.starter_roles(available) == [
        "planner",
        "feature_dev",
        "pr_review",
        "agent_cleanup",
    ]
    assert init_mod.roles_from_agents_arg("starter", available) == init_mod.starter_roles(available)
    assert init_mod.roles_from_agents_arg("all", available) == available
    assert init_mod.roles_from_agents_arg("batman,lucius", available) == [
        "feature_dev",
        "cross_repo_coordinator",
    ]
    assert init_mod.roles_from_agents_arg("starter,batman", available) == [
        "planner",
        "feature_dev",
        "pr_review",
        "agent_cleanup",
        "cross_repo_coordinator",
    ]


def test_seed_prompt_templates_does_not_overwrite(init_mod, tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "prompts").mkdir(parents=True)
    (repo_root / "prompts" / "planner.md").write_text("planner template\n")
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=repo_root,
    )
    state.enabled_roles = ["planner"]
    created = init_mod.seed_prompt_templates(state)
    assert created == [tmp_path / "alfred" / "prompts" / "drake.md"]
    assert created[0].read_text() == "planner template\n"
    created[0].write_text("custom\n")
    assert init_mod.seed_prompt_templates(state) == []
    assert created[0].read_text() == "custom\n"


def test_seed_prompt_templates_copies_shared_compose_prompt(init_mod, tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / "prompts").mkdir(parents=True)
    (repo_root / "prompts" / "spec-interrogator.md").write_text("compose prompt\n")
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=repo_root,
    )

    created = init_mod.seed_prompt_templates(state)

    prompt = tmp_path / "alfred" / "prompts" / "spec-interrogator.md"
    assert created == [prompt]
    assert prompt.read_text() == "compose prompt\n"
    prompt.write_text("custom compose prompt\n")
    assert init_mod.seed_prompt_templates(state) == []
    assert prompt.read_text() == "custom compose prompt\n"


def test_write_opt_in_gate_for_batman(init_mod, tmp_path):
    state = init_mod.WizardState(
        alfred_home=tmp_path / "alfred",
        alfredrc=tmp_path / ".alfredrc",
        repo_root=tmp_path / "repo",
    )
    state.enabled_roles = ["cross_repo_coordinator"]
    written = init_mod.write_opt_in_gate(state)
    assert written == ["batman"]
    assert "batman" in (tmp_path / "alfred" / "state" / "fleet" / "enabled.txt").read_text()


# ---------------------------------------------------------------------------
# main() doctor short-circuit (sentinel for bin/doctor.sh)
# ---------------------------------------------------------------------------


def test_doctor_sentinel(monkeypatch, capsys, init_mod):
    monkeypatch.setenv("ALFRED_DOCTOR", "1")
    rc = init_mod.main([])
    captured = capsys.readouterr()
    assert "[ALFRED-INIT-DOCTOR-OK]" in captured.out
    assert rc == 0


# ---------------------------------------------------------------------------
# Subprocess invocation also honours ALFRED_DOCTOR (the path doctor.sh hits).
# ---------------------------------------------------------------------------


def test_doctor_sentinel_via_subprocess():
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "bin" / "alfred-init.py"
    env = dict(os.environ, ALFRED_DOCTOR="1")
    cp = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, env=env, timeout=10
    )
    assert cp.returncode == 0
    assert "[ALFRED-INIT-DOCTOR-OK]" in cp.stdout
