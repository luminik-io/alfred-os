"""Focused tests for ``lib.agent_runner.metadata``."""

from __future__ import annotations


def test_agent_role_reads_env(fresh_agent_runner, monkeypatch):
    """agent_role reads ALFRED_<CODENAME>_ROLE and strips whitespace."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "  feature dev  ")
    assert ar.agent_role("lucius") == "feature dev"
    assert ar.agent_role("ghost") == ""


def test_codename_with_role_formatting(fresh_agent_runner, monkeypatch):
    """codename_with_role formats the user-facing ``Display · Role`` label.

    Known codenames carry a default display name and role title; an unknown
    codename title-cases its codename and uses the env ``_ROLE`` fallback.
    """
    ar = fresh_agent_runner
    # A known profile uses its default display name + role title.
    monkeypatch.delenv("ALFRED_LUCIUS_ROLE", raising=False)
    assert ar.codename_with_role("lucius") == "Lucius · Senior Developer"
    # An explicit ROLE_TITLE override wins for the role half of the label.
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE_TITLE", "Feature Dev")
    assert ar.codename_with_role("lucius") == "Lucius · Feature Dev"
    # An unknown codename title-cases and uses the legacy _ROLE env fallback.
    monkeypatch.delenv("ALFRED_TESTUNSET_ROLE", raising=False)
    assert ar.codename_with_role("testunset") == "Testunset"
    monkeypatch.setenv("ALFRED_TESTUNSET_ROLE", "feature dev")
    assert ar.codename_with_role("testunset") == "Testunset · feature dev"


def test_known_profile_honors_legacy_role_env(fresh_agent_runner, monkeypatch):
    """A known profile keeps its operator-configured legacy ``_ROLE``.

    ``launchd/render.sh`` still emits ``ALFRED_<CODENAME>_ROLE`` from
    agents.conf column 7, so a configured role for a standard codename like
    ``lucius`` must win over the built-in default role. Precedence is
    ``ROLE_TITLE`` > legacy ``_ROLE`` > built-in default.
    """
    ar = fresh_agent_runner
    monkeypatch.delenv("ALFRED_LUCIUS_ROLE_TITLE", raising=False)
    # No env: the built-in default role for the known profile.
    monkeypatch.delenv("ALFRED_LUCIUS_ROLE", raising=False)
    assert ar.agent_profile("lucius").role_title == "Senior Developer"
    # Legacy _ROLE configured: it overrides the built-in default.
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "Platform Lead")
    assert ar.agent_profile("lucius").role_title == "Platform Lead"
    # An explicit ROLE_TITLE still wins over the legacy _ROLE.
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE_TITLE", "Feature Dev")
    assert ar.agent_profile("lucius").role_title == "Feature Dev"


def test_agent_profile_supports_theme_override(fresh_agent_runner, monkeypatch):
    """agent_profile applies a known display name + role and theme overrides."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_LUCIUS_THEME", "orbit")
    profile = ar.agent_profile("lucius")
    assert profile.label == "Lucius · Senior Developer"
    assert profile.theme.theme_id == "orbit"


def test_commit_trailer_pascal_cases_extras(fresh_agent_runner):
    """commit_trailer rewrites extra keys to PascalCase."""
    ar = fresh_agent_runner
    out = ar.commit_trailer(
        "lucius", "abc-123", extra={"issue_number": "275", "pr_url": "https://x"}
    )
    assert "Agent-Codename: lucius" in out
    assert "Agent-Firing-Id: abc-123" in out
    assert "Issue-Number: 275" in out
    assert "Pr-Url: https://x" in out


def test_handoff_table_round_trip(fresh_agent_runner):
    """HandoffTable.add / consumers / producers behave as documented."""
    ar = fresh_agent_runner
    t = ar.HandoffTable()
    t.add("drake", "issue_filed", "lucius")
    t.add("lucius", "pr_opened", "rasalghul")
    assert "issue_filed" in t.consumers("drake")
    assert ("drake", "issue_filed") in t.producers("lucius")
    misses = t.validate({"drake", "lucius"})
    assert any("rasalghul" in m for m in misses)


def test_load_prompt_substitutes_env(fresh_agent_runner, tmp_path, monkeypatch):
    """load_prompt expands ${VAR} from os.environ and leaves unknowns intact."""
    ar = fresh_agent_runner
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("Repo: ${REPO}, Missing: ${NOT_SET}\n")
    monkeypatch.setenv("REPO", "acme/backend")
    out = ar.load_prompt(prompt_file)
    assert "Repo: acme/backend" in out
    assert "${NOT_SET}" in out
