from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from planning_actions import mark_followup_handled  # noqa: E402
from server.reader import FilesystemReader, PlanDraft  # noqa: E402
from slack_control import (  # noqa: E402
    RunResult,
    SlackControlHandler,
    is_control_message,
    is_valid_codename,
    is_valid_memory_id,
    parse_control_command,
    render_plan_detail,
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
        if len(argv) >= 3 and argv[1] == "brain":
            return self._brain(argv[2:])
        if "status" in argv and "--json" in argv:
            return RunResult(returncode=0, stdout=self.status)
        # pause/resume
        if self.mutate_rc == 0:
            return RunResult(returncode=0, stdout=f"  {argv[1]}d {argv[2]}")
        return RunResult(returncode=self.mutate_rc, stderr="unknown agent 'x'")

    def _brain(self, argv: list[str]) -> RunResult:
        if not argv:
            return RunResult(returncode=1, stderr="missing brain command")
        if argv[0] == "candidates":
            if "--status" in argv and argv[argv.index("--status") + 1] not in {
                "candidate",
                "pending",
            }:
                return RunResult(returncode=2, stderr="bad status")
            return RunResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": "cand-1",
                            "codename": "lucius",
                            "repo": "luminik-io/alfred-os",
                            "body": "Keep Slack memory candidates reviewable.",
                            "confidence": 0.82,
                            "status": "candidate",
                        }
                    ]
                ),
            )
        if argv[0] == "promotions":
            return RunResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "candidate_id": "cand-2",
                            "codename": "drake",
                            "repo": "luminik-io/alfred-os",
                            "body": "Specs need acceptance criteria before implementation.",
                            "score": 0.91,
                        }
                    ]
                ),
            )
        if argv[0] == "redis-status":
            return RunResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "base_url": "http://127.0.0.1:8000",
                        "namespace": "alfred",
                        "error": "connection refused",
                    }
                ),
            )
        if argv[0] == "redis-sync":
            return RunResult(
                returncode=0,
                stdout=json.dumps(
                    {"dry_run": "--dry-run" in argv, "matched": 3, "synced": 3, "failed": []}
                ),
            )
        if argv[0] == "propose":
            if "--agent" in argv:
                repo = argv[argv.index("--repo") + 1]
                body = argv[argv.index("--body") + 1]
            elif "--" in argv:
                separator = argv.index("--")
                repo = argv[separator - 1]
                body = argv[separator + 1]
            else:
                repo = argv[2]
                body = argv[3]
                if body.startswith("--"):
                    return RunResult(returncode=2, stderr="unrecognized arguments")
            return RunResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "id": "cand-new",
                        "repo": repo,
                        "body": body,
                        "status": "candidate",
                    }
                ),
            )
        if argv[0] in {"promote", "reject"}:
            return RunResult(returncode=0, stdout=json.dumps({"id": argv[1], "status": argv[0]}))
        return RunResult(returncode=1, stderr=f"unknown brain command {argv[0]}")


# ---------------------------------------------------------------------------
# codename validation (injection guard)
# ---------------------------------------------------------------------------


def test_valid_codenames() -> None:
    for good in ("lucius", "bane", "all", "ra-s-al-ghul", "agent.01", "a_b", "X9"):
        assert is_valid_codename(good), good


def test_valid_memory_ids() -> None:
    for good in ("123", "cand-1", "lesson:abc123", "01HXYZABC123"):
        assert is_valid_memory_id(good), good
    for bad in ("", "-1", "../secret", "with space", "x" * 97):
        assert not is_valid_memory_id(bad), bad


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
    assert parse_control_command("plans").verb == "plans"
    assert parse_control_command("memory").verb == "memory"
    cmd = parse_control_command("memory promote cand-1")
    assert cmd is not None and cmd.verb == "memory" and cmd.arg == "promote cand-1"
    cmd = parse_control_command("remember luminik-io/alfred-os: keep it reviewable")
    assert cmd is not None and cmd.verb == "remember"
    cmd = parse_control_command("pause lucius")
    assert cmd is not None and cmd.verb == "pause" and cmd.arg == "lucius"
    cmd = parse_control_command("/resume bane")
    assert cmd is not None and cmd.verb == "resume" and cmd.arg == "bane"
    cmd = parse_control_command("plan followup-1")
    assert cmd is not None and cmd.verb == "plan" and cmd.arg == "followup-1"
    cmd = parse_control_command("draft followup-1")
    assert cmd is not None and cmd.verb == "draft" and cmd.arg == "followup-1"
    cmd = parse_control_command("handled followup-1")
    assert cmd is not None and cmd.verb == "handled" and cmd.arg == "followup-1"
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


def test_plan_commands_require_single_safe_id() -> None:
    assert parse_control_command("plan") is None
    assert parse_control_command("plan ../secret") is None
    assert parse_control_command("draft .hidden") is None
    assert parse_control_command("handled followup-1 extra") is None


def test_is_control_message_detects_leading_verb() -> None:
    assert is_control_message("status")
    assert is_control_message("<@U1> pause lucius")
    assert is_control_message("pause")  # bare verb still detected (-> usage)
    assert is_control_message("plans")
    assert is_control_message("plan followup-1")
    assert is_control_message("memory")
    assert is_control_message("remember luminik-io/alfred-os: keep it reviewable")
    assert is_control_message("trusted")
    assert is_control_message("trust <@U2DEF>")
    assert not is_control_message("ship the docs")
    assert not is_control_message("")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _handler(runner: FakeRunner) -> SlackControlHandler:
    return SlackControlHandler(alfred_bin="/fake/alfred", runner=runner)


def _write_followup(state_root: Path, name: str = "slack-C1-1716480000") -> Path:
    followups = state_root / "followups"
    followups.mkdir(parents=True, exist_ok=True)
    path = followups / f"{name}.md"
    path.write_text(
        "\n".join(
            [
                "# Follow-up for PR feedback",
                "",
                "- Parent: https://github.com/luminik-io/alfred-os/pull/123",
                "- Thread: C1 / 1716480000.000000",
                "",
                "Please tighten the docs and add a smoke test before we call this done.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


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


def test_plans_command_lists_local_planning_inbox(tmp_path: Path) -> None:
    followup = _write_followup(tmp_path)
    runner = FakeRunner()
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=runner,
        state_root=tmp_path,
    )

    result = handler.handle("plans", trusted=True)

    assert result.action == "plans"
    assert followup.stem in result.text
    assert "Planning inbox" in result.text
    assert runner.calls == []


def test_plan_command_shows_followup_detail(tmp_path: Path) -> None:
    followup = _write_followup(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        state_root=tmp_path,
    )

    result = handler.handle(f"plan {followup.stem}", trusted=True)

    assert result.action == "plan"
    assert "captured follow-up" in result.text
    assert f"draft {followup.stem}" in result.text
    assert "https://github.com/luminik-io/alfred-os/pull/123" in result.text


def test_trusted_user_can_convert_followup_to_local_draft(tmp_path: Path) -> None:
    followup = _write_followup(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        state_root=tmp_path,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle(f"draft {followup.stem}", trusted=True, actor_user_id="UTEAM")

    assert result.action == "draft"
    assert "Planning draft created" in result.text
    drafts = list((tmp_path / "planning-drafts").glob("followup-*.json"))
    assert len(drafts) == 1
    payload = json.loads(drafts[0].read_text(encoding="utf-8"))
    assert payload["converted_from"]["plan_id"] == followup.stem
    archived = list((tmp_path / "followups" / "handled").glob(f"{followup.name}"))
    assert len(archived) == 1
    assert not followup.exists()


def test_non_operator_cannot_mark_followup_handled(tmp_path: Path) -> None:
    followup = _write_followup(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        state_root=tmp_path,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle(f"handled {followup.stem}", trusted=True, actor_user_id="UTEAM")

    assert result.action == "handled_rejected"
    assert "Only the operator" in result.text
    assert followup.exists()


def test_operator_can_mark_followup_handled(tmp_path: Path) -> None:
    followup = _write_followup(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        state_root=tmp_path,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle(
        f"handled {followup.stem}",
        trusted=True,
        actor_user_id="UOPERATOR",
    )

    assert result.action == "handled"
    archived = tmp_path / "followups" / "handled" / followup.name
    assert archived.exists()
    assert not followup.exists()


def test_followup_archive_read_failure_preserves_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    followup = _write_followup(tmp_path)
    plan = FilesystemReader(tmp_path).get_plan(followup.stem)
    assert plan is not None
    real_read_text = Path.read_text

    def fail_followup_read(self: Path, *args, **kwargs):
        if self == followup:
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_followup_read)

    with pytest.raises(OSError):
        mark_followup_handled(plan)

    assert followup.exists()
    assert not (tmp_path / "followups" / "handled").exists()


def test_plan_detail_distinguishes_unknown_readiness() -> None:
    plan = PlanDraft(
        plan_id="draft-1",
        title="Draft with score only",
        status="draft",
        parent=None,
        affected_repos=None,
        updated_at=None,
        path="/tmp/draft-1.json",
        preview="Awaiting review.",
        content="Awaiting review.",
        source="planning",
        readiness_score=7,
        readiness_ok=None,
    )

    text = render_plan_detail(plan)

    assert "not checked (7/100)" in text
    assert "needs scope" not in text


def test_bad_plan_id_returns_usage_not_fallthrough() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("plan ../secrets", trusted=True)
    assert result.action == "usage"
    assert "plan <plan-id>" in result.text
    assert runner.calls == []


def test_natural_plan_and_draft_phrases_fall_through_to_intake() -> None:
    runner = FakeRunner()
    handler = _handler(runner)

    plan_result = handler.handle("plan the billing migration", trusted=True)
    draft_result = handler.handle("draft a spec for memory review", trusted=True)

    assert plan_result.handled is False
    assert plan_result.action == "not_a_command"
    assert draft_result.handled is False
    assert draft_result.action == "not_a_command"
    assert runner.calls == []


def test_memory_command_lists_review_queue() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("memory", trusted=True)

    assert result.action == "memory"
    assert "Memory review" in result.text
    assert "cand-1" in result.text
    assert "cand-2" in result.text
    assert runner.calls == [
        ["/fake/alfred", "brain", "candidates", "--status", "candidate", "--limit", "5", "--json"],
        ["/fake/alfred", "brain", "promotions", "--limit", "5", "--json"],
    ]


def test_remember_queues_reviewable_memory_candidate() -> None:
    runner = FakeRunner()
    handler = _handler(runner)

    result = handler.handle(
        "remember luminik-io/alfred-os: Slack memory stays reviewable.",
        trusted=True,
        actor_user_id="UTEAM",
    )

    assert result.action == "remember"
    assert "Memory candidate queued" in result.text
    assert "cand-new" in result.text
    assert runner.calls[-1][-4:] == [
        "operator",
        "luminik-io/alfred-os",
        "--",
        "Slack memory stays reviewable.",
    ]


def test_remember_body_starting_with_dash_is_queued() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle(
        "remember global: --dry-run all integration tests before promotion.",
        trusted=True,
        actor_user_id="UTEAM",
    )

    assert result.action == "remember"
    assert "cand-new" in result.text
    assert runner.calls[-1][-2:] == ["--", "--dry-run all integration tests before promotion."]


def test_remember_without_repo_uses_global_scope() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle(
        "remember Keep candidates out of prompt context until promoted.",
        trusted=True,
    )

    assert result.action == "remember"
    assert runner.calls[-1][-3] == "global"


def test_remember_global_word_without_colon_stays_in_body() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle(
        "remember global search copy should stay explicit.",
        trusted=True,
    )

    assert result.action == "remember"
    assert runner.calls[-1][-3:] == [
        "global",
        "--",
        "global search copy should stay explicit.",
    ]


def test_remember_rejects_pathlike_repo_scope() -> None:
    runner = FakeRunner()
    handler = _handler(runner)

    colon = handler.handle(
        "remember ../etc: keep this out of argv",
        trusted=True,
    )
    equals = handler.handle(
        "remember repo=../etc keep this out of argv",
        trusted=True,
    )

    assert colon.action == "usage"
    assert equals.action == "usage"
    assert runner.calls == []


def test_untrusted_memory_commands_are_ignored() -> None:
    runner = FakeRunner()
    handler = _handler(runner)

    memory = handler.handle("memory promote cand-1", trusted=False, actor_user_id="UTEAM")
    remember = handler.handle(
        "remember global: this should not be staged",
        trusted=False,
        actor_user_id="UTEAM",
    )

    assert memory.handled is False
    assert remember.handled is False
    assert runner.calls == []


def test_memory_promote_requires_operator() -> None:
    runner = FakeRunner()
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=runner,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle("memory promote cand-1", trusted=True, actor_user_id="UTEAM")

    assert result.action == "memory_promote_rejected"
    assert "Only the operator" in result.text
    assert runner.calls == []


def test_operator_can_promote_memory_candidate() -> None:
    runner = FakeRunner()
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=runner,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle(
        "memory promote cand-1",
        trusted=True,
        actor_user_id="UOPERATOR",
    )

    assert result.action == "memory_promote"
    assert runner.calls[-1] == [
        "/fake/alfred",
        "brain",
        "promote",
        "cand-1",
        "--reviewer",
        "UOPERATOR",
        "--json",
    ]


def test_operator_reject_memory_note_starting_with_dash_is_literal() -> None:
    runner = FakeRunner()
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=runner,
        operator_user_id="UOPERATOR",
    )

    result = handler.handle(
        "memory reject cand-1 --too vague for future recall",
        trusted=True,
        actor_user_id="UOPERATOR",
    )

    assert result.action == "memory_reject"
    assert "--note=--too vague for future recall" in runner.calls[-1]


def test_memory_redis_status_is_slack_readable() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("memory redis", trusted=True)

    assert result.action == "memory_redis"
    assert "Redis Agent Memory Server" in result.text
    assert "unavailable" in result.text


def test_memory_sync_defaults_to_dry_run() -> None:
    runner = FakeRunner()
    result = _handler(runner).handle("memory sync", trusted=True)

    assert result.action == "memory_sync"
    assert "preview" in result.text
    assert runner.calls[-1] == ["/fake/alfred", "brain", "redis-sync", "--json", "--dry-run"]


def test_memory_sync_json_failure_is_reported() -> None:
    class FailingJsonSyncRunner(FakeRunner):
        def _brain(self, argv: list[str]) -> RunResult:
            if argv and argv[0] == "redis-sync":
                return RunResult(
                    returncode=2,
                    stdout=json.dumps({"dry_run": True, "matched": 1, "synced": 0}),
                    stderr="redis unavailable",
                )
            return super()._brain(argv)

    result = _handler(FailingJsonSyncRunner()).handle("memory sync", trusted=True)

    assert result.action == "memory_sync_failed"
    assert "redis unavailable" in result.text


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


def test_trusted_listing_includes_env_users_with_injected_store(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UENV1")
    store = SlackTrustStore.from_state_root(tmp_path)
    handler = SlackControlHandler(
        alfred_bin="/fake/alfred",
        runner=FakeRunner(),
        trust_store=store,
        operator_user_id="UOPERATOR",
    )

    listed = handler.handle("trusted", trusted=True, actor_user_id="UOPERATOR")

    assert listed.action == "trusted"
    assert "UOPERATOR" in listed.text
    assert "UENV1" in listed.text


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
