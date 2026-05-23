"""Focused tests for ``lib.agent_runner.metadata``."""

from __future__ import annotations


def test_agent_role_reads_env(fresh_agent_runner, monkeypatch):
    """agent_role reads ALFRED_<CODENAME>_ROLE and strips whitespace."""
    ar = fresh_agent_runner
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "  feature dev  ")
    assert ar.agent_role("lucius") == "feature dev"
    assert ar.agent_role("ghost") == ""


def test_codename_with_role_formatting(fresh_agent_runner, monkeypatch):
    """codename_with_role formats <codename> (<role>) when set."""
    ar = fresh_agent_runner
    monkeypatch.delenv("ALFRED_LUCIUS_ROLE", raising=False)
    assert ar.codename_with_role("lucius") == "lucius"
    monkeypatch.setenv("ALFRED_LUCIUS_ROLE", "feature dev")
    assert ar.codename_with_role("lucius") == "lucius (feature dev)"


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
