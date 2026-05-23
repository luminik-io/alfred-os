"""Focused tests for ``lib.agent_runner.transcripts``."""

from __future__ import annotations


def test_transcript_path_under_transcripts_root(fresh_agent_runner):
    """transcript_path resolves under ``${ALFRED_HOME}/state/transcripts``."""
    ar = fresh_agent_runner
    p = ar.transcript_path("lucius", "f-123")
    assert p.name == "f-123.jsonl"
    assert ar.TRANSCRIPTS_ROOT in p.parents
    assert "lucius" in p.parts


def test_codex_artifact_paths_creates_dir(fresh_agent_runner):
    """codex_artifact_paths returns three paths and creates the parent dir."""
    ar = fresh_agent_runner
    paths = ar.codex_artifact_paths("lucius", "f-456")
    assert set(paths.keys()) == {"last_message", "stdout", "stderr"}
    assert paths["last_message"].parent.exists()
    assert paths["last_message"].name == "f-456.last.md"
    assert paths["stdout"].name == "f-456.stdout.txt"
    assert paths["stderr"].name == "f-456.stderr.txt"


def test_extract_codex_session_id_present(fresh_agent_runner):
    """Reads the ``session id: <value>`` line from Codex output."""
    ar = fresh_agent_runner
    output = "some prelude\nsession id: abc-123\nmore"
    assert ar._extract_codex_session_id(output) == "abc-123"
    assert ar._extract_codex_session_id("no marker here") is None


def test_extract_codex_tokens_present(fresh_agent_runner):
    """Reads the integer that follows ``tokens used`` on the next line."""
    ar = fresh_agent_runner
    text = "tokens used\n1,234,567\n"
    assert ar._extract_codex_tokens(text) == 1234567
    assert ar._extract_codex_tokens("unrelated content") == 0
