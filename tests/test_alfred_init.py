"""Tests for alfred-init wizard helpers.

Covers the deterministic, side-effect-free helpers:
    - discover_agents (filesystem walk over bin/*.py + .sh)
    - render_agents_conf (TSV emission)
    - env_assignments_for (per-role env-var map)
    - read_alfredrc / upsert_alfredrc (idempotent rc append)
    - _resolve_repo_selection (repo selection grammar)
    - main() with ALFRED_DOCTOR=1 (short-circuit sentinel)

The interactive prompt path, subprocess shells, and Slack/AWS HTTP calls
are NOT exercised — those need a live operator + accounts.

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
# Module loader — bin/alfred-init.py has a hyphen, so import via spec.
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
    (bin_dir / "unknown.py").write_text("# not in catalog\n")
    out = init_mod.discover_agents(bin_dir)
    assert "feature_dev" in out
    assert "planner" in out
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
    assert out["ALFRED_LUCIUS_REPOS"] == "acme/foo,acme/bar"


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
    assert second.count("alfred-init — generated") == 1


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
