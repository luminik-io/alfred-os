from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_listener import (  # noqa: E402
    SlackPlanningListener,
    draft_from_slack_text,
    render_bridge_outcome_ack,
)
from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry  # noqa: E402


class Poster:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True}


def test_bridge_ack_for_not_ready_draft_is_actionable() -> None:
    text = render_bridge_outcome_ack(
        SimpleNamespace(
            created=False,
            status="refused_not_ready",
            detail="draft readiness is 42/100; required 80/100. Answer first: What should ship?",
            issue_url=None,
            repo=None,
        )
    )

    assert "Draft still needs scope" in text
    assert "42/100" in text
    assert "acceptance criteria" in text
    assert "Nothing was created" in text


def _thread_reply(text: str, *, event_id: str = "Ev1", user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "C1",
            "user": user,
            "text": text,
            "ts": "1716480001.000001",
            "thread_ts": "1716480000.000000",
        },
    }


def test_known_plan_thread_reply_is_captured_and_acknowledged(tmp_path: Path) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(SlackThreadRecord(kind="plan", channel="C1", thread_ts="1716480000.000000"))
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(
        _thread_reply("acceptance: the Slack thread shows the current plan scope")
    )

    assert result.handled is True
    assert result.action == "captured_plan_feedback"
    assert poster.messages[0]["thread_ts"] == "1716480000.000000"
    assert "Plan feedback captured" in poster.messages[0]["text"]
    feedback_files = list((tmp_path / "threads" / "feedback").glob("*.jsonl"))
    assert feedback_files
    assert "current plan scope" in feedback_files[0].read_text(encoding="utf-8")


def test_report_thread_reply_writes_followup_context(tmp_path: Path) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(
        SlackThreadRecord(
            kind="pr",
            channel="C1",
            thread_ts="1716480000.000000",
            codename="batman",
            firing_id="firing-1",
            title="Improve planning loop",
            parent_repo="luminik-io/alfred-os",
            parent_issue=120,
            metadata={"created": ["https://github.com/luminik-io/alfred-os/pull/12"]},
        )
    )
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(_thread_reply("change: add a manual docs smoke test"))

    assert result.handled is True
    assert result.action == "captured_followup"
    assert "Follow-up feedback captured" in poster.messages[0]["text"]
    followups = list((tmp_path / "followups").glob("*.md"))
    assert len(followups) == 1
    text = followups[0].read_text(encoding="utf-8")
    assert "Slack Follow-up Feedback" in text
    assert "https://github.com/luminik-io/alfred-os/pull/12" in text
    assert "manual docs smoke test" in text
    record = registry.lookup("C1", "1716480000.000000")
    assert record is not None
    assert record.status == "followup_waiting"
    assert record.metadata["followup_path"] == str(followups[0])


def test_report_thread_followup_write_failure_is_acknowledged(tmp_path: Path, monkeypatch) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(
        SlackThreadRecord(
            kind="pr",
            channel="C1",
            thread_ts="1716480000.000000",
            codename="batman",
            firing_id="firing-1",
            title="Improve planning loop",
            parent_repo="luminik-io/alfred-os",
            parent_issue=120,
            metadata={"created": ["https://github.com/luminik-io/alfred-os/pull/12"]},
        )
    )
    original_write_text = Path.write_text

    def fail_followup_write(self: Path, *args, **kwargs):
        if self.parent.name == "followups" and self.suffix == ".md":
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_followup_write)
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(_thread_reply("change: add a manual docs smoke test"))

    assert result.handled is True
    assert result.action == "captured_followup"
    assert "Follow-up feedback captured" in poster.messages[0]["text"]
    assert not list((tmp_path / "followups").glob("*.md"))


def test_duplicate_events_are_ignored(tmp_path: Path) -> None:
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(SlackThreadRecord(kind="plan", channel="C1", thread_ts="1716480000.000000"))
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
    )

    assert listener.handle_payload(_thread_reply("acceptance: first")).handled is True
    second = listener.handle_payload(_thread_reply("acceptance: duplicate"))

    assert second.handled is False
    assert second.action == "duplicate"


def test_untrusted_thread_reply_is_ignored(tmp_path: Path) -> None:
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(SlackThreadRecord(kind="plan", channel="C1", thread_ts="1716480000.000000"))
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(_thread_reply("remove repo: everything", user="U2"))

    assert result.handled is False
    assert result.action == "ignored"
    assert "untrusted" in result.detail


def test_listener_requires_trusted_users(tmp_path: Path) -> None:
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=(),
    )

    result = listener.handle_payload(_thread_reply("acceptance: trust nobody by default"))

    assert result.handled is False
    assert result.action == "ignored"
    assert "trusted" in result.detail


def _dm(text: str, *, event_id: str = "EvCtl", user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "D1",
            "channel_type": "im",
            "user": user,
            "text": text,
            "ts": "1716480020.000001",
        },
    }


def test_trusted_control_command_routes_to_control_not_draft(tmp_path: Path) -> None:
    from slack_control import RunResult, SlackControlHandler

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> RunResult:
        calls.append(argv)
        return RunResult(returncode=0, stdout="  paused lucius")

    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=SlackControlHandler(alfred_bin="/fake/alfred", runner=runner),
    )

    result = listener.handle_payload(_dm("pause lucius"))

    assert result.handled is True
    assert result.action == "control_pause"
    assert calls[-1] == ["/fake/alfred", "pause", "lucius"]
    assert "Paused" in poster.messages[-1]["text"]
    # No planning draft should have been created for a control command.
    assert not list((tmp_path / "planning-drafts").glob("*.json"))


def test_operator_can_trust_collaborator_without_listener_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
    )

    trust_result = listener.handle_payload(
        _dm("trust <@UTEAM1>", event_id="EvTrust", user="UOPERATOR")
    )
    assert trust_result.handled is True
    assert trust_result.action == "control_trust"

    draft_result = listener.handle_payload(
        _dm("Build a cleaner onboarding checklist", event_id="EvTeam", user="UTEAM1")
    )

    assert draft_result.handled is True
    assert draft_result.action == "draft_created"
    assert list((tmp_path / "planning-drafts").glob("*.json"))


def test_trusted_collaborator_cannot_trust_another_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
    )
    listener.handle_payload(_dm("trust <@UTEAM1>", event_id="EvTrust", user="UOPERATOR"))

    result = listener.handle_payload(_dm("trust <@UTEAM2>", event_id="EvTrust2", user="UTEAM1"))

    assert result.handled is True
    assert result.action == "control_trust_rejected"
    assert "Only the operator" in poster.messages[-1]["text"]


def test_prose_dm_still_creates_planning_draft(tmp_path: Path) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    result = listener.handle_payload(
        _dm("title: Build a CSV export\nrepo: acme-org/api\ndesired: users can export rows")
    )
    assert result.action == "draft_created"
    assert list((tmp_path / "planning-drafts").glob("*.json"))


def test_app_mention_creates_planning_draft(tmp_path: Path) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    payload = {
        "event_id": "EvDraft",
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "user": "U1",
            "text": (
                "<@UALFRED> title: Improve Slack planning\n"
                "problem: Users need a safer way to describe work before agents build.\n"
                "repo: luminik-io/alfred-os\n"
                "desired: Alfred saves a draft and asks for missing acceptance criteria."
            ),
            "ts": "1716480010.000001",
        },
    }

    result = listener.handle_payload(payload)

    assert result.handled is True
    assert result.action == "draft_created"
    assert result.draft_path
    draft = json.loads(Path(result.draft_path).read_text(encoding="utf-8"))
    assert draft["draft"]["title"] == "Improve Slack planning"
    assert draft["draft"]["repos"] == ["luminik-io/alfred-os"]
    assert "Planning draft saved" in poster.messages[0]["text"]


def test_app_mention_save_failure_is_acknowledged(
    tmp_path: Path,
    monkeypatch,
) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    original_write_text = Path.write_text

    def fail_tmp_write(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_tmp_write)

    result = listener.handle_payload(
        {
            "event_id": "EvDraftWriteFail",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Users need a safer way to describe work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "desired: Alfred saves a draft and asks for missing acceptance criteria."
                ),
                "ts": "1716480010.000001",
            },
        }
    )

    assert result.handled is True
    assert result.action == "draft_save_failed"
    assert not result.draft_path
    assert "could not be saved" in poster.messages[0]["text"]
    assert not list((tmp_path / "planning-drafts").glob("*.json"))


def test_draft_thread_reply_revises_saved_draft(tmp_path: Path) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    created = listener.handle_payload(
        {
            "event_id": "EvDraft",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Users need a safer way to describe work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "repo: luminik-io/mobile\n"
                    "desired: Alfred saves a draft and asks for missing acceptance criteria."
                ),
                "ts": "1716480010.000001",
            },
        }
    )

    revised = listener.handle_payload(
        {
            "event_id": "EvDraftReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "remove repo: mobile\n"
                    "acceptance: follow-up replies update the saved draft\n"
                    "test: run listener revision tests"
                ),
                "ts": "1716480012.000001",
                "thread_ts": "1716480010.000001",
            },
        }
    )

    assert created.action == "draft_created"
    assert revised.handled is True
    assert revised.action == "draft_revised"
    assert revised.draft_path == created.draft_path
    payload = json.loads(Path(created.draft_path).read_text(encoding="utf-8"))
    assert payload["revision_count"] == 1
    assert payload["draft"]["repos"] == ["luminik-io/alfred-os"]
    assert payload["draft"]["acceptance_criteria"] == ["follow-up replies update the saved draft"]
    assert "listener revision tests" in payload["draft"]["test_plan"]
    assert payload["readiness"]["score"] == revised.readiness_score
    assert "Planning draft revised" in poster.messages[-1]["text"]
    record = SlackThreadRegistry(tmp_path / "slack-threads").lookup("C1", "1716480010.000001")
    assert record is not None
    assert record.metadata["revision_count"] == 1
    assert record.metadata["readiness_score"] == revised.readiness_score


def test_draft_revision_history_is_capped(tmp_path: Path) -> None:
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
    )
    created = listener.handle_payload(
        {
            "event_id": "EvDraftCap",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Users need a safer way to describe work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "desired: Alfred saves a draft and asks for missing acceptance criteria."
                ),
                "ts": "1716480020.000001",
            },
        }
    )
    path = Path(created.draft_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["revision_count"] = 55
    payload["revisions"] = [{"text": f"old revision {idx}"} for idx in range(55)]
    path.write_text(json.dumps(payload), encoding="utf-8")

    revised = listener.handle_payload(
        {
            "event_id": "EvDraftCapReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "acceptance: capped revisions still preserve the latest change",
                "ts": "1716480021.000001",
                "thread_ts": "1716480020.000001",
            },
        }
    )

    assert revised.action == "draft_revised"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["revision_count"] == 56
    assert len(saved["revisions"]) == 50
    assert saved["revisions"][0]["text"] == "old revision 6"
    assert (
        saved["revisions"][-1]["text"]
        == "acceptance: capped revisions still preserve the latest change"
    )


def test_draft_thread_reply_can_resolve_open_questions(tmp_path: Path) -> None:
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
    )
    created = listener.handle_payload(
        {
            "event_id": "EvQuestionDraft",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Operators need a safe way to discuss Alfred work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "desired: Alfred saves a draft and keeps work paused until questions are resolved.\n"
                    "acceptance: unresolved questions keep the draft in needs-scope state\n"
                    "test: run listener revision tests\n"
                    "question: should this include PR follow-up replies too?"
                ),
                "ts": "1716480040.000001",
            },
        }
    )

    revised = listener.handle_payload(
        {
            "event_id": "EvQuestionDraftReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "open questions: none",
                "ts": "1716480041.000001",
                "thread_ts": "1716480040.000001",
            },
        }
    )

    assert created.readiness_ok is False
    assert revised.action == "draft_revised"
    assert revised.readiness_ok is True
    payload = json.loads(Path(created.draft_path).read_text(encoding="utf-8"))
    assert payload["draft"]["open_questions"] == "None."
    assert payload["readiness"]["ok"] is True


def test_draft_revision_write_failure_is_acknowledged(tmp_path: Path, monkeypatch) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    created = listener.handle_payload(
        {
            "event_id": "EvDraftWriteFail",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Users need a safer way to describe work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "desired: Alfred saves a draft and asks for missing acceptance criteria."
                ),
                "ts": "1716480030.000001",
            },
        }
    )
    path = Path(created.draft_path)
    before = path.read_text(encoding="utf-8")
    original_write_text = Path.write_text

    def fail_tmp_write(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_tmp_write)

    revised = listener.handle_payload(
        {
            "event_id": "EvDraftWriteFailReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "acceptance: write failures are acknowledged",
                "ts": "1716480031.000001",
                "thread_ts": "1716480030.000001",
            },
        }
    )

    assert revised.handled is True
    assert revised.action == "captured_draft_feedback"
    assert "could not save the revised draft" in poster.messages[-1]["text"]
    assert path.read_text(encoding="utf-8") == before


def test_draft_thread_reply_uses_memory_provider(tmp_path: Path) -> None:
    class Provider:
        name = "test"

        def recall(self, *, repo=None, query=None, limit=3):
            assert repo == "luminik-io/alfred-os"
            return [{"repo": repo, "body": "Keep Slack planning replies concise."}]

    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
        memory_provider=Provider(),
    )
    created = listener.handle_payload(
        {
            "event_id": "EvMemoryDraft",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Improve Slack planning\n"
                    "problem: Users need a safer way to describe work before agents build.\n"
                    "repo: luminik-io/alfred-os\n"
                    "desired: Alfred saves a draft and asks for missing acceptance criteria."
                ),
                "ts": "1716480013.000001",
            },
        }
    )

    listener.handle_payload(
        {
            "event_id": "EvMemoryDraftReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "acceptance: replies preserve planning memory hints",
                "ts": "1716480014.000001",
                "thread_ts": "1716480013.000001",
            },
        }
    )

    payload = json.loads(Path(created.draft_path).read_text(encoding="utf-8"))
    assert payload["memory"][0]["body"] == "Keep Slack planning replies concise."
    assert "Planning Memory" in payload["spec_body"]


def test_threaded_app_mention_does_not_register_parent_thread(tmp_path: Path) -> None:
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
    )
    payload = {
        "event_id": "EvThreadDraft",
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "user": "U1",
            "text": (
                "<@UALFRED> title: Scope this safely\n"
                "problem: This mention lives inside a human-owned thread.\n"
                "desired: Alfred saves a draft without making the parent thread actionable."
            ),
            "ts": "1716480011.000001",
            "thread_ts": "1716480000.000000",
        },
    }

    result = listener.handle_payload(payload)

    assert result.handled is True
    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    assert registry.lookup("C1", "1716480000.000000") is None
    assert registry.lookup("C1", "1716480011.000001") is not None


def test_draft_from_slack_text_extracts_repos_and_title() -> None:
    draft = draft_from_slack_text(
        "title: Add memory review\nrepo: luminik-io/alfred-os\n"
        "problem: Operators need memory candidates before lessons affect future runs."
    )

    assert draft.title == "Add memory review"
    assert draft.repos == ["luminik-io/alfred-os"]


def test_draft_from_slack_text_keeps_repeated_acceptance_lines() -> None:
    draft = draft_from_slack_text(
        "title: Add planning guardrails\n"
        "acceptance: vague requests are held for scope\n"
        "acceptance: test plans are required before implementation"
    )

    assert draft.acceptance_criteria == [
        "vague requests are held for scope",
        "test plans are required before implementation",
    ]


def test_listener_once_uses_env_trusted_users_by_default(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "U1")
    monkeypatch.delenv("ALFRED_TRUSTED_SLACK_USER_IDS", raising=False)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "event_id": "EvCliOnce",
                "event": {
                    "type": "app_mention",
                    "channel": "C1",
                    "user": "U1",
                    "text": (
                        "<@UALFRED> title: Improve Slack planning\n"
                        "problem: Operators need local listener tests to match the live listener.\n"
                        "repo: luminik-io/alfred-os\n"
                        "desired: The once command honors the configured operator by default."
                    ),
                    "ts": "1716480099.000001",
                },
            }
        ),
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location(
        "alfred_slack_listener_cli", REPO / "bin" / "alfred-slack-listener.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rc = module.main(["once", str(payload_path), "--state-root", str(tmp_path), "--no-post"])

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["handled"] is True
    assert result["action"] == "draft_created"
    assert list((tmp_path / "planning-drafts").glob("*.json"))
