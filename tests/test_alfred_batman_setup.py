"""Tests for ``bin/alfred-batman-setup.py``.

The wizard has live Slack and Claude branches, but the regression
surface here is local and deterministic: .env idempotency, validation,
check-only reporting, and lifecycle-doctor invocation.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_module(monkeypatch: pytest.MonkeyPatch | None = None, tmp_path: Path | None = None):
    if monkeypatch is not None and tmp_path is not None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ALFRED_OPERATOR_SLACK_USER_ID", raising=False)
        monkeypatch.delenv("BATMAN_PARENT_REPO", raising=False)
        monkeypatch.delenv("BATMAN_AUTO_EXECUTE", raising=False)
        monkeypatch.delenv("BATMAN_APPROVAL_MODE", raising=False)
    spec = importlib.util.spec_from_file_location(
        "alfred_batman_setup", REPO / "bin" / "alfred-batman-setup.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["alfred_batman_setup"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_read_env_file_parses_exports_and_quotes(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "export GH_ORG=acme\n"
        "BATMAN_PARENT_REPO='acme/specs'\n"
        'BATMAN_SLACK_CHANNEL="alfred"\n'
    )
    out = mod.read_env_file(env_file)
    assert out["GH_ORG"] == "acme"
    assert out["BATMAN_PARENT_REPO"] == "acme/specs"
    assert out["BATMAN_SLACK_CHANNEL"] == "alfred"


def test_upsert_batman_block_is_idempotent_and_preserves_other_blocks(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# operator preamble\n"
        "export GH_ORG=acme\n\n"
        "# alfred-init, generated below this line. Safe to re-run.\n"
        "GH_ORG=acme\n"
    )
    kvs = {
        "BATMAN_AUTO_EXECUTE": "approval-gate",
        "BATMAN_PARENT_REPO": "acme/specs",
        "SLACK_BOT_TOKEN": "xoxb-1234567890-abcdef",
    }
    mod.upsert_batman_block(env_file, kvs)
    first = env_file.read_text()
    mod.upsert_batman_block(env_file, kvs)
    second = env_file.read_text()

    assert first == second
    assert second.count("alfred-batman-setup, generated") == 1
    assert "alfred-init, generated" in second
    assert "BATMAN_PARENT_REPO=acme/specs" in second
    assert "export BATMAN_PARENT_REPO" not in second
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_check_only_reports_missing_required_values(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    alfred_home.mkdir()
    env_file.write_text("GH_ORG=acme\n")
    out = mod.main(["--check-only", "--alfred-home", str(alfred_home)])

    captured = capsys.readouterr()
    assert out == 1
    assert "missing CLAUDE_CODE_OAUTH_TOKEN" in captured.out
    assert "BATMAN_PARENT_REPO" in captured.err


def test_check_only_ignores_process_only_claude_token(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    alfred_home.mkdir()
    env_file.write_text("BATMAN_PARENT_REPO=acme/specs\n")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token-present-only-in-shell")

    out = mod.main(["--check-only", "--alfred-home", str(alfred_home)])

    captured = capsys.readouterr()
    assert out == 1
    assert "missing CLAUDE_CODE_OAUTH_TOKEN" in captured.out
    assert "CLAUDE_CODE_OAUTH_TOKEN" in captured.err


def test_check_only_approval_gate_requires_slack_token_and_user(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    alfred_home.mkdir()
    env_file.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=token-present\n"
        "BATMAN_AUTO_EXECUTE=approval-gate\n"
        "BATMAN_PARENT_REPO=acme/specs\n"
    )
    out = mod.main(["--check-only", "--alfred-home", str(alfred_home)])

    captured = capsys.readouterr()
    assert out == 1
    assert "SLACK_BOT_TOKEN" in captured.err
    assert "ALFRED_OPERATOR_SLACK_USER_ID" in captured.err


def test_check_only_file_approval_mode_does_not_require_slack(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    alfred_home.mkdir()
    env_file.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=token-present\n"
        "BATMAN_AUTO_EXECUTE=approval-gate\n"
        "BATMAN_APPROVAL_MODE=file\n"
        "BATMAN_PARENT_REPO=acme/specs\n"
    )
    out = mod.main(["--check-only", "--alfred-home", str(alfred_home)])

    captured = capsys.readouterr()
    assert out == 0
    assert "SLACK_BOT_TOKEN" in captured.out
    assert "SLACK_BOT_TOKEN" not in captured.err
    assert "ALFRED_OPERATOR_SLACK_USER_ID" not in captured.err


def test_non_interactive_writes_supplied_values_without_live_calls(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    token = "xoxb-1234567890"
    out = mod.main(
        [
            "--non-interactive",
            "--skip-token-setup",
            "--skip-doctor",
            "--alfred-home",
            str(alfred_home),
            "--mode",
            "approval-gate",
            "--slack-bot-token",
            token,
            "--operator-user-id",
            "U123ABC",
            "--slack-channel",
            "#alfred",
            "--parent-repo",
            "acme/specs",
            "--picker",
            "newest",
            "--approval-timeout-s",
            "120",
        ]
    )

    assert out == 0
    text = env_file.read_text()
    assert "BATMAN_AUTO_EXECUTE=approval-gate" in text
    assert "BATMAN_APPROVAL_MODE=slack-or-file" in text
    assert f"SLACK_BOT_TOKEN={token}" in text
    assert "ALFRED_OPERATOR_SLACK_USER_ID=U123ABC" in text
    assert "BATMAN_SLACK_CHANNEL=alfred" in text
    assert "BATMAN_PARENT_REPO=acme/specs" in text
    assert "BATMAN_PICKER=newest" in text
    assert "BATMAN_APPROVAL_TIMEOUT_S=120" in text
    assert "export BATMAN_AUTO_EXECUTE" not in text
    assert "Skipping lifecycle doctor" in capsys.readouterr().err


def test_non_interactive_file_approval_mode_skips_slack_values(tmp_path, monkeypatch, capsys):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    out = mod.main(
        [
            "--non-interactive",
            "--skip-token-setup",
            "--skip-doctor",
            "--alfred-home",
            str(alfred_home),
            "--mode",
            "approval-gate",
            "--approval-mode",
            "file",
            "--parent-repo",
            "acme/specs",
        ]
    )

    assert out == 0
    text = env_file.read_text()
    assert "BATMAN_AUTO_EXECUTE=approval-gate" in text
    assert "BATMAN_APPROVAL_MODE=file" in text
    assert "SLACK_BOT_TOKEN" not in text
    assert "ALFRED_OPERATOR_SLACK_USER_ID" not in text
    captured = capsys.readouterr()
    assert "Skipping Slack approval setup" in captured.err
    assert "Approve or decline Batman plans from the Alfred client" in captured.out


def test_non_interactive_blank_channel_preserves_runtime_fallback(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    env_file = alfred_home / ".env"
    out = mod.main(
        [
            "--non-interactive",
            "--skip-token-setup",
            "--skip-doctor",
            "--alfred-home",
            str(alfred_home),
            "--mode",
            "0",
            "--parent-repo",
            "acme/specs",
        ]
    )

    assert out == 0
    assert "BATMAN_SLACK_CHANNEL" not in env_file.read_text()


def test_invalid_slack_token_is_rejected(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    assert mod.validate_slack_bot_token("not-a-token") is not None
    assert mod.validate_slack_bot_token("xoxb-1234567890-abcdef") is None


def test_lifecycle_doctor_invoked_when_not_skipped(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    alfred_home = tmp_path / "alfred-home"
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "doctor.sh").write_text("#!/usr/bin/env bash\n")
    calls: list[list[str]] = []

    def fake_run(cmd, *, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[doctor-ok]\n", stderr="")

    monkeypatch.setattr(mod, "run_cmd", fake_run)
    out = mod.main(
        [
            "--non-interactive",
            "--skip-token-setup",
            "--repo-root",
            str(repo),
            "--alfred-home",
            str(alfred_home),
            "--mode",
            "0",
            "--parent-repo",
            "acme/specs",
        ]
    )

    assert out == 0
    assert calls == [["bash", str(repo / "bin" / "doctor.sh"), "--lifecycle"]]


def test_infer_parent_repo_requires_explicit_parent_repo(tmp_path, monkeypatch):
    mod = _load_module(monkeypatch, tmp_path)
    removed_scan_key = "BATMAN" + "_SCAN_REPOS"
    assert mod.infer_parent_repo({}, {"GH_ORG": "acme", "ALFRED_LUCIUS_REPOS": "backend"}) == ""
    assert mod.infer_parent_repo({}, {"GH_ORG": "acme", removed_scan_key: "backend"}) == ""
    assert mod.infer_parent_repo({}, {"BATMAN_PARENT_REPO": "acme/specs"}) == "acme/specs"
