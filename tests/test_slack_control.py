from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_control import (  # noqa: E402
    RunResult,
    SlackControlHandler,
    is_control_message,
    is_valid_codename,
    parse_control_command,
)
from slack_trust import SlackTrustStore  # noqa: E402

STATUS_JSON = json.dumps(
    {
        "ts": "2026-05-30T00:00:00Z",
        "global": {"locks": [{"agent": "lucius"}], "paused_repos": ["acme-org/api"]},
        "agents": [
            {
                "codename": "lucius",
                "loaded": True,
                "paused": False,
                "last_fired": "2026-05-30T11:00:00Z",
                "today_firings": 4,
                "today_successes": 3,
                "today_failures": 1,
            },
            {
                "codename": "bane",
                "loaded": False,
                "paused": True,
                "last_fired": "2026-05-29T09:00:00Z",
                "today_firings": 0,
                "today_successes": 0,
                "today_failures": 0,
            },
        ],
    }
)


class FakeRunner:
    """Records argv vectors and replays scripted RunResults by verb."""

    def __init__(self, *, status: str = STATUS_JSON, mutate_rc: int = 0) -> None:
        self.status = status
        self.mutate_rc = mutate_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> RunResult:
        self.calls.append(argv)
        if "status" in argv and "--json" in argv:
            return RunResult(returncode=0, stdout=self.status)
        # pause/resume
        if self.mutate_rc == 0:
            return RunResult(returncode=0, stdout=f"  {argv[1]}d {argv[2]}")
        return RunResult(returncode=self.mutate_rc, stderr="unknown agent 'x'")


# ---------------------------------------------------------------------------
# codename validation (injection guard)
# ---------------------------------------------------------------------------


def test_valid_codenames() -> None:
    for good in ("lucius", "bane", "all", "ra-s-al-ghul", "agent.01", "a_b", "X9"):
        assert is_valid_codename(good), good


def test_invalid_codenames_rejected() -> None:
    for bad in (
        "",
        "-rf",  # leading hyphen -> could read as a flag
        "--force",
        "lucius;rm -rf /",
        "lucius rm",
        "a b",
        "$(whoami)",
        "`id`",
        "lucius|cat",
        "lucius&&echo",
        "name/with/slash",
        "x" * 65,
    ):
        assert not is_valid_codename(bad), bad


# ---------------------------------------------------------------------------
# parsing: leading verb only, prose never triggers
# ---------------------------------------------------------------------------


def test_leading_verb_parses() -> None:
    assert parse_control_command("status").verb == "status"
    assert parse_control_command("help").verb == "help"
    assert parse_control_command("runs").verb == "runs"
    cmd = parse_control_command("pause lucius")
    assert cmd is not None and cmd.verb == "pause" and cmd.arg == "lucius"
    cmd = parse_control_command("/resume bane")
    assert cmd is not None and cmd.verb == "resume" and cmd.arg == "bane"
    cmd = parse_control_command("trust <@U2DEF>")
    assert cmd is not None and cmd.verb == "trust" and cmd.arg == "U2DEF"
    cmd = parse_control_command("<@UALFRED> untrust <@U2DEF|neha>")
    assert cmd is not None and cmd.verb == "untrust" and cmd.arg == "U2DEF"


def test_mentions_are_stripped_before_parse() -> None:
    cmd = parse_control_command("<@UALFRED> pause lucius")
    assert cmd is not None and cmd.verb == "pause" and cmd.arg == "lucius"


def test_prose_is_not_a_command() -> None:
    for prose in (
        "can you pause everything later?",
        "the build status looks bad",
        "please resume work on the planner",
        "I want to status check the repo",  # 'I' is the leading token
        "let's run the tests",
        "pause the project for the holidays",  # extra words -> not a command
        "resume lucius and bane",  # two args -> not a clean command
    ):
        assert parse_control_command(prose) is None, prose


def test_pause_requires_single_valid_codename() -> None:
    assert parse_control_command("pause") is None
    assert parse_control_command("pause -rf") is None
    assert parse_control_command("pause lucius extra") is None
    assert parse_control_command("pause name/with/slash") is None


def test_is_control_message_detects_leading_verb() -> None:
    assert is_control_message("status")
    assert is_control_message("<@U1> pause lucius")
    assert is_control_message("pause")  # bare verb still detected (-> usage)
    assert is_control_message("trusted")
    assert is_control_message("trust <@U2DEF>")
    assert not is_control_message("ship the docs")
    assert not is_control_message("")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _handler(runner: FakeRunner) -> SlackControlHandler:
    return SlackControlHandler(alfred_bin="/fake/alfred", runner=runner)


def test_untrusted_user_never_acts() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("pause lucius", trusted=False)
    assert result.handled is False
    assert result.action == "ignored_untrusted"
    assert runner.calls == []


def test_status_command_renders_snapshot() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("status", trusted=True)
    assert result.handled is True
    assert result.action == "status"
    assert "Fleet status" in result.text
    assert "lucius" in result.text
    assert runner.calls == [["/fake/alfred", "status", "--json"]]


def test_runs_command_lists_recent_firings() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("runs", trusted=True)
    assert result.action == "runs"
    assert "Recent firings" in result.text
    assert "lucius" in result.text
    assert "last fired 2026-05-30T11:00:00Z" in result.text


def test_pause_invokes_cli_with_exact_argv() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("pause lucius", trusted=True)
    assert result.action == "pause"
    assert result.handled is True
    assert runner.calls[-1] == ["/fake/alfred", "pause", "lucius"]
    assert "Paused" in result.text


def test_resume_invokes_cli_with_exact_argv() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("<@U1> resume bane", trusted=True)
    assert result.action == "resume"
    assert runner.calls[-1] == ["/fake/alfred", "resume", "bane"]


def test_pause_failure_is_reported() -> None:
    runner = FakeRunner(mutate_rc=1)
    result = _handler(runner).handle("pause lucius", trusted=True)
    assert result.action == "pause_failed"
    assert "Could not pause" in result.text


def test_help_lists_commands_without_running_anything() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("help", trusted=True)
    assert result.action == "help"
    assert "control commands" in result.text.lower()
    assert "trust <@user>" in result.text
    assert runner.calls == []


def test_bare_pause_returns_usage_not_fallthrough() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("pause", trusted=True)
    assert result.handled is True
    assert result.action == "usage"
    assert "Usage:" in result.text
    assert runner.calls == []  # never shelled out


def test_prose_falls_through_unhandled() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("build me a new dashboard", trusted=True)
    assert result.handled is False
    assert result.action == "not_a_command"
    assert runner.calls == []


def test_status_unavailable_when_cli_fails() -> None:
    def bad_runner(argv: list[str]) -> RunResult:
        return RunResult(returncode=1, stderr="boom")

    handler = SlackControlHandler(alfred_bin="/fake/alfred", runner=bad_runner)
    result = handler.handle("status", trusted=True)
    assert result.action == "status_unavailable"
    assert "unavailable" in result.text.lower()


def test_operator_can_add_and_remove_trusted_collaborator(tmp_path: Path) -> None:
    runner = FakeRunner()
    store = SlackTrustStore.from_state_root(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=runner,
        trust_store=store,
        operator_user_id="UOPERATOR",
    )

    added = handler.handle("trust <@U2DEF>", trusted=True, actor_user_id="UOPERATOR")
    assert added.action == "trust"
    assert "Trusted collaborator added" in added.text
    assert [user.user_id for user in store.list_local()] == ["U2DEF"]
    assert runner.calls == []

    listed = handler.handle("trusted", trusted=True, actor_user_id="UOPERATOR")
    assert listed.action == "trusted"
    assert "U2DEF" in listed.text

    removed = handler.handle("untrust U2DEF", trusted=True, actor_user_id="UOPERATOR")
    assert removed.action == "untrust"
    assert store.list_local() == ()


def test_non_operator_cannot_change_trusted_collaborators(tmp_path: Path) -> None:
    store = SlackTrustStore.from_state_root(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        trust_store=store,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle("trust <@U2DEF>", trusted=True, actor_user_id="UTEAM1")

    assert result.action == "trust_rejected"
    assert "Only the operator" in result.text
    assert store.list_local() == ()


def test_trust_usage_for_bad_target() -> None:
    result = _handler(FakeRunner()).handle("trust not-a-user", trusted=True)
    assert result.action == "usage"
    assert "trust <@user>" in result.text
