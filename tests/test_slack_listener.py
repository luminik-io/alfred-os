from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_listener import SlackPlanningListener, draft_from_slack_text  # noqa: E402
from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry  # noqa: E402


class Poster:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True}


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
