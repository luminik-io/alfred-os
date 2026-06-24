"""Tests for the Batman lifecycle doctor."""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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


def test_lifecycle_doctor_uses_configured_bundle_slug_prefix() -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={
            "SLACK_BOT_TOKEN": "xoxb-test",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
            "BATMAN_BUNDLE_SLUG_PREFIX": "prod",
        },
        slack_client=FakeSlack(),
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 0
    assert 'parsed bundle slug: "prod-doctor-hello"' in out
    assert 'full label: "agent:bundle:prod-doctor-hello"' in out


def test_lifecycle_doctor_fails_prefixed_label_that_is_too_long() -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={
            "SLACK_BOT_TOKEN": "xoxb-test",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-test",
            "BATMAN_BUNDLE_SLUG_PREFIX": "very-long-production-fleet-prefix",
        },
        slack_client=FakeSlack(),
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 1
    assert 'parsed bundle slug: "very-long-production-fleet-prefix-doctor-hello"' in out
    assert "bundle label generation" in out
    assert "length: 59 chars" in out
    assert "Keep bundle slugs short enough for GitHub label names." in out


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


def test_lifecycle_doctor_requires_claude_oauth_token(tmp_path) -> None:
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    stream = io.StringIO()
    # ALFRED_HOME points at an empty dir so the .env credential fallback
    # finds nothing; otherwise the doctor would resolve a token from the
    # real ~/.alfred/.env on the host running the suite.
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={"SLACK_BOT_TOKEN": "xoxb-test", "ALFRED_HOME": str(tmp_path)},
        slack_client=FakeSlack(),
        command_runner=fake_claude_ok,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 1
    assert "CLAUDE_CODE_OAUTH_TOKEN reachable (env or $ALFRED_HOME/.env): no" in out
    assert "alfred setup-token" in out


def test_lifecycle_doctor_resolves_token_from_env_file(tmp_path) -> None:
    """A token in $ALFRED_HOME/.env (not the process env) is honored, the
    same way the runtime loader resolves it."""
    from agent_runner.lifecycle_doctor import FIXTURE_PATH, run_lifecycle_doctor

    (tmp_path / ".env").write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-fromdotenv\n", encoding="utf-8"
    )

    seen_env: dict[str, str] = {}

    def capture_claude(cmd, *, input_text, timeout_s, env):
        seen_env.update(env)
        return subprocess.CompletedProcess(list(cmd), 0, stdout="hi\n", stderr="")

    stream = io.StringIO()
    rc = run_lifecycle_doctor(
        fixture=FIXTURE_PATH,
        env={"SLACK_BOT_TOKEN": "xoxb-test", "ALFRED_HOME": str(tmp_path)},
        slack_client=FakeSlack(),
        command_runner=capture_claude,
        stream=stream,
    )

    out = stream.getvalue()
    assert rc == 0
    assert "CLAUDE_CODE_OAUTH_TOKEN reachable: yes" in out
    # The live probe must run with the token loaded from .env.
    assert seen_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-fromdotenv"


def test_lifecycle_doctor_fails_bad_parent_body() -> None:
    from agent_runner.lifecycle_doctor import check_parent_parser

    result, plan = check_parent_parser("not a valid parent issue")

    assert plan is not None
    assert result.ok is False
    assert "parsed 0 children" in result.lines


# --------------------------------------------------------------------------
# Shared .env unquoting: the Python readers must agree with the bash loaders
# (decode_env_value in bin/agent-launch and bin/doctor.sh) for every quoted
# form, including the edge cases a naive strip('"').strip("'") would mangle.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Unquoted plain token: returned verbatim.
        ("sk-ant-oat01-plain", "sk-ant-oat01-plain"),
        # shlex.quote leaves a token with no metachars unquoted.
        (shlex.quote("sk-ant-oat01-plain"), "sk-ant-oat01-plain"),
        # shlex.quote wraps a metachar token in single quotes; one pair only.
        (shlex.quote("sk-ant-oat01-$DANGEROUS"), "sk-ant-oat01-$DANGEROUS"),
        # shlex.quote escapes an embedded single quote via the '"'"' splice.
        (shlex.quote("ab'cd"), "ab'cd"),
        (shlex.quote("'leading-quote"), "'leading-quote"),
        (shlex.quote("trailing-quote'"), "trailing-quote'"),
        # Double-quoted value: one pair stripped, inner left intact.
        ('"sk-ant-oat01-dq"', "sk-ant-oat01-dq"),
        ('"has spaces and $stuff"', "has spaces and $stuff"),
        # A bare single quote char is not a matching pair: returned verbatim.
        ("'", "'"),
        ('"', '"'),
        # A value that merely starts with a quote but is not wrapped: verbatim.
        ("'unterminated", "'unterminated"),
        ('unterminated"', 'unterminated"'),
        # Empty value.
        ("", ""),
    ],
)
def test_decode_env_value_matches_quoting_contract(raw: str, expected: str) -> None:
    """The shared decoder unwraps exactly one matching quote pair and undoes
    the shlex single-quote escape splice, matching the bash loaders rather
    than peeling every leading/trailing quote like a naive strip would."""
    from agent_runner.paths import decode_env_value

    assert decode_env_value(raw) == expected


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-oat01-plain",
        "sk-ant-oat01-$DANGEROUS",
        "ab'cd",
        "'leading-quote",
        "trailing-quote'",
        'embedded"double',
        "has spaces",
    ],
)
def test_decode_env_value_round_trips_shlex_quote(token: str) -> None:
    """A token written exactly the way write_token persists it (shlex.quote)
    decodes back to the original token through the shared helper. This is the
    write/read contract the silent-401 fix depends on."""
    from agent_runner.paths import decode_env_value

    assert decode_env_value(shlex.quote(token)) == token


@pytest.mark.parametrize(
    "token",
    [
        "sk-ant-oat01-plain",
        "sk-ant-oat01-$DANGEROUS",
        "ab'cd",
        "'leading-quote",
        "trailing-quote'",
    ],
)
def test_decode_env_value_agrees_with_bash_loader(token: str, tmp_path: Path) -> None:
    """Cross-check the Python decoder against the real bash decode_env_value
    in bin/agent-launch: write the token with shlex.quote (as write_token
    does), load it through the actual shell loader, and assert the exported
    value matches what the Python helper decodes. Keeps the two in sync."""
    from agent_runner.paths import decode_env_value

    agent_launch = REPO / "bin" / "agent-launch"
    home = tmp_path
    (home / ".env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(token)}\n", encoding="utf-8")
    target = tmp_path / "echo-token.sh"
    target.write_text(
        '#!/usr/bin/env bash\nprintf "TOKEN[%s]\\n" "${CLAUDE_CODE_OAUTH_TOKEN:-unset}"\n',
        encoding="utf-8",
    )
    target.chmod(0o755)

    env = dict(os.environ)
    env["ALFRED_HOME"] = str(home)
    env["HOME"] = str(home.parent)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env.pop("ALFRED_PYTHON", None)

    proc = subprocess.run(
        ["bash", str(agent_launch), str(target)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    bash_value = proc.stdout.split("TOKEN[", 1)[1].rsplit("]", 1)[0]
    assert bash_value == token
    assert decode_env_value(shlex.quote(token)) == bash_value
