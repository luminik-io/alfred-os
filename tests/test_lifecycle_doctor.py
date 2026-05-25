"""Tests for the Batman lifecycle doctor."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


class FakeSlack:
    def __init__(self, *, reaction_ok: bool = True) -> None:
        self.reaction_ok = reaction_ok
        self.deleted: list[tuple[str, str]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "channel": "C0DOCTOR", "ts": "1700000000.123"}

    def reactions_get(self, *, channel: str, timestamp: str, full: bool = True) -> dict[str, Any]:
        if self.reaction_ok:
            return {"ok": True, "message": {"reactions": []}}
        return {"ok": False, "error": "missing_scope", "needed": "reactions:read"}

    def chat_delete(self, *, channel: str, ts: str) -> dict[str, Any]:
        self.deleted.append((channel, ts))
        return {"ok": True}


def fake_claude_ok(
    cmd: list[str] | tuple[str, ...],
    *,
    input_text: str,
    timeout_s: int,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(cmd), 0, stdout="hi\n", stderr="")


def test_lifecycle_doctor_happy_path() -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={
            "SLACK_BOT_TOKEN": "xoxb-test",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
            "BATMAN_SLACK_CHANNEL": "alfred-test",
        },
        slack_client=FakeSlack(),
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 0
    assert 'parsed bundle slug: "doctor-hello"' in out
    assert "parsed 3 children" in out
    assert "lifecycle preflight: 4 passed, 0 failed" in out


def test_lifecycle_doctor_reports_missing_slack_scope() -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    slack = FakeSlack(reaction_ok=False)
    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={"SLACK_BOT_TOKEN": "xoxb-test", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test"},
        slack_client=slack,
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 1
    assert "reactions.get failed: missing_scope" in out
    assert "reactions:read" in out
    assert slack.deleted == [("C0DOCTOR", "1700000000.123")]


def test_lifecycle_doctor_requires_claude_oauth_token() -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={"SLACK_BOT_TOKEN": "xoxb-test"},
        slack_client=FakeSlack(),
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN present in env: no" in out
    assert "alfred setup-token" in out


def test_lifecycle_doctor_fails_bad_parent_body() -> None:
    from agent_runner.lifecycle_doctor import check_parent_parser

    result, plan = check_parent_parser("not a valid parent issue")

    assert plan is not None
    assert result.ok is False
    assert "parsed 0 children" in result.lines
