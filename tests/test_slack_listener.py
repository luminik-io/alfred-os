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


class MemoryProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def propose_memory(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"cand-{len(self.calls)}")


class LegacyMemoryProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def propose_memory(
        self,
        *,
        agent: str | None,
        topic: str,
        body: str,
        repo: str | None = None,
        evidence: list[dict] | None = None,
        source: str = "agent",
    ):
        self.calls.append(
            {
                "agent": agent,
                "topic": topic,
                "body": body,
                "repo": repo,
                "evidence": evidence,
                "source": source,
            }
        )
        return len(self.calls)


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


def test_bridge_ack_links_created_issue_for_slack() -> None:
    text = render_bridge_outcome_ack(
        SimpleNamespace(
            created=True,
            status="created",
            detail="",
            is_bundle=False,
            repo="acme-org/api",
            issue_url="https://github.com/acme-org/api/issues/42",
        ),
        summary="Fixes the checkout retry banner.",
    )

    assert "Fixes the checkout retry banner." in text
    assert "*Issue:* <https://github.com/acme-org/api/issues/42|acme-org/api#42>" in text


def test_bridge_ack_links_bundle_issues_for_slack() -> None:
    text = render_bridge_outcome_ack(
        SimpleNamespace(
            created=True,
            status="created",
            detail="",
            is_bundle=True,
            bundle_label="agent:bundle:checkout",
            issue_urls=(),
            issues_by_repo={
                "acme-org/api": "https://github.com/acme-org/api/issues/42",
                "acme-org/web": "https://github.com/acme-org/web/issues/43",
            },
        )
    )

    assert "`acme-org/api`: <https://github.com/acme-org/api/issues/42|acme-org/api#42>" in text
    assert "`acme-org/web`: <https://github.com/acme-org/web/issues/43|acme-org/web#43>" in text


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


def test_known_plan_thread_reply_is_captured_and_acknowledged(tmp_path: Path) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Batman Plan\n", encoding="utf-8")
    registry.register(
        SlackThreadRecord(
            kind="plan",
            channel="C1",
            thread_ts="1716480000.000000",
            title="Improve Slack planning",
            parent_repo="example-org/alfred",
            parent_issue=120,
            plan_path=str(plan_path),
            metadata={
                "affected_repos": ["example-org/alfred", "luminik-io/alfred-os"],
                "child_count": 2,
                "children_by_repo": {
                    "example-org/alfred": 1,
                    "luminik-io/alfred-os": 1,
                },
            },
        )
    )
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(
        _thread_reply(
            "remove repo: alfred-os\n"
            "acceptance: the Slack thread shows the current plan scope\n"
            "open question: Should the docs mention Redis?"
        )
    )

    assert result.handled is True
    assert result.action == "plan_revised"
    assert poster.messages[0]["thread_ts"] == "1716480000.000000"
    assert "Plan revised" in poster.messages[0]["text"]
    assert (
        "Execution scope if approved now (1 repo, 1 child issue(s))" in poster.messages[0]["text"]
    )
    assert "`example-org/alfred`" in poster.messages[0]["text"]
    assert "Alfred will not execute" in poster.messages[0]["text"]
    feedback_files = list((tmp_path / "threads" / "feedback").glob("*.jsonl"))
    assert feedback_files
    assert "current plan scope" in feedback_files[0].read_text(encoding="utf-8")
    record = registry.lookup("C1", "1716480000.000000")
    assert record is not None
    assert record.status == "needs_resolution"
    assert record.metadata["revised_repos"] == ["example-org/alfred"]
    revision_path = Path(record.metadata["plan_revision_path"])
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    assert revision["latest"]["requires_resolution"] is True
    assert revision["latest"]["revised_repos"] == ["example-org/alfred"]
    assert revision["record"]["plan_path"] == str(plan_path)

    resolved = listener.handle_payload(_thread_reply("open questions: none", event_id="Ev2"))

    assert resolved.handled is True
    assert resolved.action == "plan_revised"
    assert resolved.readiness_ok is True
    updated = registry.lookup("C1", "1716480000.000000")
    assert updated is not None
    assert updated.status == "revised"
    assert updated.metadata["plan_revision_count"] == 2
    latest = json.loads(Path(updated.metadata["plan_revision_path"]).read_text(encoding="utf-8"))
    assert latest["latest"]["requires_resolution"] is False


def test_known_plan_thread_question_is_answered_without_revising_plan(tmp_path: Path) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "\n".join(
            [
                "*Alfred plan ready* · `onboarding`",
                "*Parent:* <https://github.com/example-org/alfred/issues/779|example-org/alfred#779>",
                "*Work:* Improve onboarding state persistence",
                "*Readiness:* ready for approval",
                "",
                "*Scope if approved now:* 2 repos, 2 child issues",
                "  - `backend`: persist orchestrator events",
                "  - `frontend`: show connection status",
            ]
        ),
        encoding="utf-8",
    )
    registry.register(
        SlackThreadRecord(
            kind="plan",
            channel="C1",
            thread_ts="1716480000.000000",
            title="Improve onboarding state persistence",
            parent_repo="example-org/alfred",
            parent_issue=779,
            plan_path=str(plan_path),
            metadata={
                "affected_repos": ["luminik-io/backend", "luminik-io/frontend"],
                "child_count": 2,
            },
        )
    )
    questions: list[str] = []

    def answerer(record, question, plan_markdown):
        questions.append(question)
        assert record.parent_issue == 779
        assert "Improve onboarding state persistence" in plan_markdown
        return "This plan stores onboarding progress and shows it clearly in the client."

    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        plan_answerer=answerer,
    )

    result = listener.handle_payload(
        _thread_reply("question: explain this planned feature in detail")
    )

    assert result.handled is True
    assert result.action == "plan_question_answered"
    assert result.readiness_ok is True
    assert questions == ["explain this planned feature in detail"]
    assert poster.messages[0]["thread_ts"] == "1716480000.000000"
    assert "*Answer*" in poster.messages[0]["text"]
    assert "Plan revised" not in poster.messages[0]["text"]
    assert "will not execute" not in poster.messages[0]["text"]
    record = registry.lookup("C1", "1716480000.000000")
    assert record is not None
    assert record.status == "open"
    assert record.metadata["last_plan_question"] == "explain this planned feature in detail"
    assert not list((tmp_path / "plan-revisions").glob("*.json"))


def test_plan_thread_structured_command_ending_in_question_mark_revises_plan(
    tmp_path: Path,
) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("*Work:* Improve onboarding\n", encoding="utf-8")
    registry.register(
        SlackThreadRecord(
            kind="plan",
            channel="C1",
            thread_ts="1716480000.000000",
            title="Improve onboarding",
            parent_repo="example-org/alfred",
            parent_issue=779,
            plan_path=str(plan_path),
            metadata={
                "affected_repos": ["example-org/alfred"],
                "child_count": 1,
            },
        )
    )
    answered: list[str] = []
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        plan_answerer=lambda _record, question, _plan: answered.append(question) or "answer",
    )

    result = listener.handle_payload(_thread_reply("test: does the empty state render?"))

    assert result.handled is True
    assert result.action == "plan_revised"
    assert answered == []
    assert "Plan revised" in poster.messages[0]["text"]
    assert "does the empty state render?" in poster.messages[0]["text"]
    record = registry.lookup("C1", "1716480000.000000")
    assert record is not None
    assert record.status == "revised"
    assert record.metadata["plan_revision_count"] == 1


def test_plan_thread_resolved_question_ending_in_question_mark_clears_blocker(
    tmp_path: Path,
) -> None:
    poster = Poster()
    registry = SlackThreadRegistry(tmp_path / "threads")
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("*Work:* Improve onboarding\n", encoding="utf-8")
    registry.register(
        SlackThreadRecord(
            kind="plan",
            channel="C1",
            thread_ts="1716480000.000000",
            title="Improve onboarding",
            parent_repo="example-org/alfred",
            parent_issue=779,
            plan_path=str(plan_path),
            metadata={
                "affected_repos": ["example-org/alfred"],
                "child_count": 1,
            },
        )
    )
    answered: list[str] = []
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        plan_answerer=lambda _record, question, _plan: answered.append(question) or "answer",
    )

    blocked = listener.handle_payload(
        _thread_reply("open question: Should we include docs?", event_id="Ev1")
    )
    resolved = listener.handle_payload(
        _thread_reply("resolved question: Should we include docs?", event_id="Ev2")
    )

    assert blocked.action == "plan_revised"
    assert blocked.readiness_ok is False
    assert resolved.action == "plan_revised"
    assert resolved.readiness_ok is True
    assert answered == []
    record = registry.lookup("C1", "1716480000.000000")
    assert record is not None
    assert record.status == "revised"


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
            parent_repo="example-org/alfred",
            parent_issue=120,
            metadata={"created": ["https://github.com/example-org/alfred/pull/12"]},
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
    assert "https://github.com/example-org/alfred/pull/12" in text
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
            parent_repo="example-org/alfred",
            parent_issue=120,
            metadata={"created": ["https://github.com/example-org/alfred/pull/12"]},
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


def test_ready_slack_draft_queues_reviewable_memory_candidate(tmp_path: Path) -> None:
    provider = MemoryProvider()
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        memory_provider=provider,
    )

    result = listener.handle_payload(
        _dm(
            "title: Queue Slack planning memories\n"
            "problem: Operators need Alfred to preserve useful Slack planning decisions without manual notes.\n"
            "desired: Alfred queues a reviewable memory candidate after a scoped Slack draft is saved.\n"
            "repo: example-org/alfred\n"
            "acceptance: the local draft records the memory candidate id for review\n"
            "test: run the Slack listener unit test and verify no lesson is promoted automatically\n"
            "out of scope: automatic promotion\n"
            "open questions: none",
            event_id="EvMemory",
        )
    )

    assert result.handled is True
    assert result.action == "draft_created"
    assert provider.calls
    assert provider.calls[0]["repo"] == "example-org/alfred"
    assert provider.calls[0]["source"] == "slack-draft"
    assert provider.calls[0]["tags"] == ["slack", "planning"]
    draft = json.loads(Path(result.draft_path).read_text(encoding="utf-8"))
    assert draft["memory_candidate_ids"] == ["cand-1"]
    assert draft["memory_candidate_keys"] == ["slack-planning:example-org/alfred"]


def test_ready_slack_revision_reuses_existing_memory_candidate(tmp_path: Path) -> None:
    provider = MemoryProvider()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
        memory_provider=provider,
    )
    created = listener.handle_payload(
        {
            "event_id": "EvMemoryDraft",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "<@UALFRED> title: Queue Slack planning memories\n"
                    "problem: Operators need Alfred to preserve useful Slack planning decisions without manual notes.\n"
                    "desired: Alfred queues a reviewable memory candidate after a scoped Slack draft is saved.\n"
                    "repo: example-org/alfred\n"
                    "acceptance: the local draft records the memory candidate id for review\n"
                    "test: run the Slack listener unit test and verify no lesson is promoted automatically\n"
                    "out of scope: automatic promotion\n"
                    "open questions: none"
                ),
                "ts": "1716480100.000001",
            },
        }
    )

    revised = listener.handle_payload(
        {
            "event_id": "EvMemoryDraftReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": (
                    "acceptance: repeated ready revisions do not queue duplicate memory candidates\n"
                    "test: run listener memory tests\n"
                    "open questions: none"
                ),
                "ts": "1716480102.000001",
                "thread_ts": "1716480100.000001",
            },
        }
    )

    assert created.action == "draft_created"
    assert revised.action == "draft_revised"
    assert len(provider.calls) == 1
    payload = json.loads(Path(created.draft_path).read_text(encoding="utf-8"))
    assert payload["memory_candidate_ids"] == ["cand-1"]
    assert payload["memory_candidate_keys"] == ["slack-planning:example-org/alfred"]


def test_slack_memory_candidate_queue_requires_ready_draft(tmp_path: Path) -> None:
    provider = MemoryProvider()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
        memory_provider=provider,
    )

    result = listener.handle_payload(_dm("build something useful", event_id="EvVague"))

    assert result.handled is True
    assert result.action == "draft_created"
    assert result.readiness_ok is False
    assert provider.calls == []


def test_slack_memory_candidate_queue_can_be_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALFRED_SLACK_MEMORY_CANDIDATES", "0")
    provider = MemoryProvider()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
        memory_provider=provider,
    )

    result = listener.handle_payload(
        _dm(
            "title: Queue Slack planning memories\n"
            "problem: Operators need Alfred to preserve useful Slack planning decisions without manual notes.\n"
            "desired: Alfred queues a reviewable memory candidate after a scoped Slack draft is saved.\n"
            "repo: example-org/alfred\n"
            "acceptance: the local draft records the memory candidate id for review\n"
            "test: run the Slack listener unit test and verify no lesson is promoted automatically\n"
            "out of scope: automatic promotion\n"
            "open questions: none",
            event_id="EvMemoryOff",
        )
    )

    assert result.handled is True
    assert result.readiness_ok is True
    assert provider.calls == []
    draft = json.loads(Path(result.draft_path).read_text(encoding="utf-8"))
    assert "memory_candidate_ids" not in draft


def test_slack_memory_candidate_queue_supports_legacy_writer(tmp_path: Path) -> None:
    provider = LegacyMemoryProvider()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=Poster(),
        trusted_user_ids=("U1",),
        memory_provider=provider,
    )

    result = listener.handle_payload(
        _dm(
            "title: Queue Slack planning memories\n"
            "problem: Operators need Alfred to preserve useful Slack planning decisions without manual notes.\n"
            "desired: Alfred queues a reviewable memory candidate after a scoped Slack draft is saved.\n"
            "repo: example-org/alfred\n"
            "acceptance: the local draft records the memory candidate id for review\n"
            "test: run the Slack listener unit test and verify no lesson is promoted automatically\n"
            "out of scope: automatic promotion\n"
            "open questions: none",
            event_id="EvLegacyMemory",
        )
    )

    assert result.handled is True
    assert provider.calls
    assert provider.calls[0]["agent"] == "planning"
    assert provider.calls[0]["topic"] == "slack-planning"
    draft = json.loads(Path(result.draft_path).read_text(encoding="utf-8"))
    assert draft["memory_candidate_ids"] == ["1"]


def test_app_mention_save_failure_is_acknowledged(tmp_path: Path, monkeypatch) -> None:
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
                    "repo: example-org/alfred\n"
                    "desired: Alfred saves a draft and keeps work paused until questions are resolved.\n"
                    "acceptance: unresolved questions keep the draft in needs-scope state\n"
                    "test: run listener revision tests\n"
                    "open question: should this include PR follow-up replies too?"
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
                        "repo: example-org/alfred\n"
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


def test_default_control_handler_reads_local_planning_inbox(tmp_path: Path) -> None:
    followups = tmp_path / "followups"
    followups.mkdir(parents=True)
    (followups / "slack-C1-1716480000.md").write_text(
        "# Follow-up for PR feedback\n\nPlease add the missing docs check.\n",
        encoding="utf-8",
    )
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(_dm("plans"))

    assert result.handled is True
    assert result.action == "control_plans"
    assert "Planning inbox" in poster.messages[-1]["text"]
    assert "slack-C1-1716480000" in poster.messages[-1]["text"]


def test_plan_prefixed_prose_still_creates_planning_draft(tmp_path: Path) -> None:
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )

    result = listener.handle_payload(_dm("plan the billing migration with clearer retry copy"))

    assert result.handled is True
    assert result.action == "draft_created"
    assert list((tmp_path / "planning-drafts").glob("*.json"))


def test_operator_can_trust_collaborator_without_listener_restart(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.delenv("ALFRED_TRUSTED_SLACK_USER_IDS", raising=False)
    poster = Poster()
    listener = SlackPlanningListener(state_root=tmp_path, poster=poster)

    trusted = listener.handle_payload(
        {
            "event_id": "EvTrustTeam",
            "event": {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "UOPERATOR",
                "text": "trust <@UTEAM1>",
                "ts": "1716480100.000001",
            },
        }
    )

    assert trusted.handled is True
    assert trusted.action == "control_trust"
    assert "UTEAM1" in poster.messages[-1]["text"]

    created = listener.handle_payload(
        {
            "event_id": "EvTeamDraft",
            "event": {
                "type": "message",
                "channel": "D2",
                "channel_type": "im",
                "user": "UTEAM1",
                "text": (
                    "title: Improve local planning\n"
                    "problem: A teammate needs to shape work without touching code.\n"
                    "desired: Alfred saves a scoped draft for the operator."
                ),
                "ts": "1716480101.000001",
            },
        }
    )

    assert created.handled is True
    assert created.action == "draft_created"


def test_trusted_collaborator_cannot_trust_another_user(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UOPERATOR")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UTEAM1")
    poster = Poster()
    listener = SlackPlanningListener(state_root=tmp_path, poster=poster)

    result = listener.handle_payload(
        {
            "event_id": "EvTrustDenied",
            "event": {
                "type": "message",
                "channel": "D2",
                "channel_type": "im",
                "user": "UTEAM1",
                "text": "trust <@UTEAM2>",
                "ts": "1716480102.000001",
            },
        }
    )

    assert result.handled is True
    assert result.action == "control_trust_rejected"
    assert "Only the operator" in poster.messages[-1]["text"]


# ---------------------------------------------------------------------------
# Conversational intent router (additive fallback) + safety gate
# ---------------------------------------------------------------------------


class CardPoster:
    """Poster that returns an incrementing ``ts`` so the card can be tracked."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self._n = 0

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        self._n += 1
        return {"ok": True, "ts": f"170000000{self._n}.000001"}

    def card_ts(self) -> str:
        # The confirmation card is the first message the router posts.
        return f"170000000{1}.000001"


class _SlackResponseLike:
    """Mimics slack_sdk's SlackResponse: dict-like (``.get`` / ``[]``) but not a dict."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]


class SlackResponsePoster(CardPoster):
    """CardPoster whose post returns a dict-like SlackResponse, not a real dict."""

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        self._n += 1
        return _SlackResponseLike({"ok": True, "ts": f"170000000{self._n}.000001"})


def _intent_engine(payload: dict):
    """A mock LLM dispatch that always returns ``payload`` as JSON."""

    def _invoke(_prompt: str) -> str:
        return json.dumps(payload)

    return _invoke


def _intent_dm(text: str, *, event_id: str = "EvIntent", user: str = "U1") -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "message",
            "channel": "D9",
            "channel_type": "im",
            "user": user,
            "text": text,
            "ts": "1716480500.000001",
        },
    }


def _reaction(
    *,
    reaction: str,
    ts: str,
    user: str,
    channel: str = "D9",
    event_id: str = "EvReact",
) -> dict:
    return {
        "event_id": event_id,
        "event": {
            "type": "reaction_added",
            "user": user,
            "reaction": reaction,
            "item": {"type": "message", "channel": channel, "ts": ts},
        },
    }


def _intent_catalog():
    from slack_intent import RepoCatalog

    return RepoCatalog.build(
        {"acme-frontend": "frontend", "acme-backend": "backend"},
        gh_org="acme-io",
    )


def test_router_explicitly_disabled_falls_through_to_planning(tmp_path: Path, monkeypatch) -> None:
    # The router is ON by default in production, but ALFRED_INTENT_ROUTER_ENABLED=0
    # explicitly disables it. With no intent_engine injected and the flag off,
    # the resolver returns None: the router is inert and free text opens a
    # planning draft exactly as before. (The conftest autouse fixture already
    # pins the flag off for the default test environment; we set it here too so
    # the test is explicit about the contract it asserts.)
    monkeypatch.setenv("ALFRED_INTENT_ROUTER_ENABLED", "0")
    poster = Poster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
    )
    result = listener.handle_payload(_intent_dm("can you queue the web app issue 7"))
    assert result.handled is True
    assert result.action == "draft_created"


def test_status_query_answered_directly_without_confirmation(tmp_path: Path) -> None:
    poster = CardPoster()

    class StubControl:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def handle(self, text, *, trusted, actor_user_id=None):
            self.calls.append(text)
            return SimpleNamespace(
                handled=True, action="status", text="*Fleet status*\nall green", detail=""
            )

    control = StubControl()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
    )
    result = listener.handle_payload(_intent_dm("how's the fleet doing?"))
    assert result.handled is True
    assert result.action == "intent_status"
    # Read-only: it answered, it never registered a confirmation card.
    assert "Fleet status" in poster.messages[-1]["text"]
    assert "status" in control.calls
    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    assert registry.lookup("D9", poster.card_ts()) is None


def test_top_level_mention_keeps_thread_conversational_without_remention(
    tmp_path: Path,
) -> None:
    poster = CardPoster()
    control = SimpleNamespace(calls=[])

    def handle(text, *, trusted, actor_user_id=None):
        control.calls.append(text)
        return SimpleNamespace(
            handled=True,
            action=text.split()[0],
            text=f"*Answer for* `{text}`",
            detail="",
        )

    control.handle = handle
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    first = listener.handle_payload(
        {
            "event_id": "EvMentionStatus",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> how is the fleet doing?",
                "ts": "1716480700.000001",
            },
        }
    )

    assert first.action == "intent_status"
    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    record = registry.lookup("C1", "1716480700.000001")
    assert record is not None
    assert record.kind == "conversation"
    assert record.status == "open"

    second = listener.handle_payload(
        {
            "event_id": "EvMentionStatusReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "what shipped today?",
                "ts": "1716480701.000001",
                "thread_ts": "1716480700.000001",
            },
        }
    )

    assert second.handled is True
    assert second.action == "intent_status"
    assert control.calls == ["status", "runs"]
    assert poster.messages[-1]["thread_ts"] == "1716480700.000001"


def test_conversation_thread_fallback_allows_only_read_only_controls(tmp_path: Path) -> None:
    registry = SlackThreadRegistry(tmp_path / "threads")
    registry.register(
        SlackThreadRecord(
            kind="conversation",
            channel="C1",
            thread_ts="1716480000.000000",
            title="Fleet status",
            status="open",
        )
    )
    control = SimpleNamespace(calls=[])

    def handle(text, *, trusted, actor_user_id=None):
        control.calls.append(text)
        return SimpleNamespace(
            handled=True,
            action=text.split()[0],
            text=f"*Answer for* `{text}`",
            detail="",
        )

    control.handle = handle
    poster = CardPoster()
    listener = SlackPlanningListener(
        registry=registry,
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "unknown", "confidence": 0.2}),
        repo_catalog=_intent_catalog(),
    )

    read_only = listener.handle_payload(
        _thread_reply("runs", event_id="EvConversationRuns", user="U1")
    )
    mutating = listener.handle_payload(
        _thread_reply(
            "hold acme-io/acme-backend#8",
            event_id="EvConversationHold",
            user="U1",
        )
    )

    assert read_only.handled is True
    assert read_only.action == "conversation_control_runs"
    assert mutating.handled is False
    assert "not actionable" in mutating.detail
    assert control.calls == ["runs"]


def test_conversation_thread_reply_can_borrow_root_target(tmp_path: Path) -> None:
    poster = CardPoster()
    control = SimpleNamespace(
        handle=lambda text, **_: SimpleNamespace(
            handled=True, action=text.split()[0], text=f"*Answer for* `{text}`", detail=""
        )
    )
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 4,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    first = listener.handle_payload(
        {
            "event_id": "EvThreadRootTarget",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> can you arm acme-io/acme-backend#4",
                "ts": "1716480900.000001",
            },
        }
    )

    assert first.action == "intent_confirmation_posted"

    listener._intent_engine = _intent_engine({"action": "queue_issue", "confidence": 0.8})
    second = listener.handle_payload(
        {
            "event_id": "EvThreadBorrowTarget",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "yes, do it",
                "ts": "1716480901.000001",
                "thread_ts": "1716480900.000001",
            },
        }
    )

    assert second.action == "intent_confirmation_posted"
    assert "acme-io/acme-backend#4" in poster.messages[-1]["text"]


def test_conversation_thread_does_not_borrow_stale_channel_target(tmp_path: Path) -> None:
    poster = CardPoster()
    control = SimpleNamespace(
        handle=lambda text, **_: SimpleNamespace(
            handled=True, action="status", text="*Fleet status*", detail=""
        )
    )
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-backend",
                "issue": 9,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    older = listener.handle_payload(
        {
            "event_id": "EvOlderChannelTarget",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> queue acme-io/acme-backend#9",
                "ts": "1716480910.000001",
            },
        }
    )
    assert older.action == "intent_confirmation_posted"

    listener._intent_engine = _intent_engine({"action": "status_query", "confidence": 0.95})
    status_root = listener.handle_payload(
        {
            "event_id": "EvNewStatusRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> status?",
                "ts": "1716480920.000001",
            },
        }
    )
    assert status_root.action == "intent_status"

    listener._intent_engine = _intent_engine({"action": "queue_issue", "confidence": 0.8})
    followup = listener.handle_payload(
        {
            "event_id": "EvNewStatusThreadFollowup",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "yes, do it",
                "ts": "1716480921.000001",
                "thread_ts": "1716480920.000001",
            },
        }
    )

    assert followup.action == "intent_clarify"
    assert "acme-io/acme-backend#9" not in poster.messages[-1]["text"]


def test_conversation_thread_reply_can_complete_clarification(tmp_path: Path) -> None:
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=SimpleNamespace(
            handle=lambda text, **_: SimpleNamespace(
                handled=True, action="status", text="*Fleet status*", detail=""
            )
        ),
        intent_engine=_intent_engine({"action": "queue_issue", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    root = listener.handle_payload(
        {
            "event_id": "EvClarifyRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> queue issue #4",
                "ts": "1716480930.000001",
            },
        }
    )
    assert root.action == "intent_clarify"

    listener._intent_engine = _intent_engine({"action": "unknown", "confidence": 0.8})
    reply = listener.handle_payload(
        {
            "event_id": "EvClarifyReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "acme-io/acme-backend#4",
                "ts": "1716480931.000001",
                "thread_ts": "1716480930.000001",
            },
        }
    )

    assert reply.action == "intent_confirmation_posted"
    assert "Confirm queue" in poster.messages[-1]["text"]
    assert "acme-io/acme-backend#4" in poster.messages[-1]["text"]


def test_conversation_thread_reply_can_complete_agent_clarification(tmp_path: Path) -> None:
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=SimpleNamespace(
            handle=lambda text, **_: SimpleNamespace(
                handled=True, action="status", text="*Fleet status*", detail=""
            )
        ),
        intent_engine=_intent_engine(
            {"action": "schedule_agent", "agent": "batman", "confidence": 0.95}
        ),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    root = listener.handle_payload(
        {
            "event_id": "EvAgentClarifyRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> schedule Batman",
                "ts": "1716480940.000001",
            },
        }
    )
    assert root.action == "intent_clarify"

    listener._intent_engine = _intent_engine({"action": "unknown", "confidence": 0.8})
    reply = listener.handle_payload(
        {
            "event_id": "EvAgentClarifyReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "daily@09:00",
                "ts": "1716480941.000001",
                "thread_ts": "1716480940.000001",
            },
        }
    )

    assert reply.action == "intent_confirmation_posted"
    assert "Confirm reschedule" in poster.messages[-1]["text"]
    assert "batman" in poster.messages[-1]["text"]
    assert "daily@09:00" in poster.messages[-1]["text"]


def test_conversation_thread_schedule_asks_cadence_after_agent_reply(tmp_path: Path) -> None:
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=SimpleNamespace(
            handle=lambda text, **_: SimpleNamespace(
                handled=True, action="status", text="*Fleet status*", detail=""
            )
        ),
        intent_engine=_intent_engine({"action": "schedule_agent", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    root = listener.handle_payload(
        {
            "event_id": "EvScheduleClarifyRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> schedule",
                "ts": "1716480950.000001",
            },
        }
    )
    assert root.action == "intent_clarify"

    listener._intent_engine = _intent_engine({"action": "unknown", "confidence": 0.8})
    agent_reply = listener.handle_payload(
        {
            "event_id": "EvScheduleAgentReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "Batman",
                "ts": "1716480951.000001",
                "thread_ts": "1716480950.000001",
            },
        }
    )
    assert agent_reply.action == "intent_clarify"
    assert "What cadence should `batman` use?" in poster.messages[-1]["text"]

    cadence_reply = listener.handle_payload(
        {
            "event_id": "EvScheduleCadenceReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "daily@09:00",
                "ts": "1716480952.000001",
                "thread_ts": "1716480950.000001",
            },
        }
    )
    assert cadence_reply.action == "intent_confirmation_posted"
    assert "batman" in poster.messages[-1]["text"]
    assert "daily@09:00" in poster.messages[-1]["text"]


def test_conversation_thread_reply_can_complete_dry_run_clarification(tmp_path: Path) -> None:
    poster = CardPoster()
    control = SimpleNamespace(calls=[])

    def handle(text, *, trusted, actor_user_id=None):
        control.calls.append(text)
        return SimpleNamespace(
            handled=True,
            action="dry-run",
            text=f"*Dry run for* `{text}`",
            detail="",
        )

    control.handle = handle
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "dry_run_agent", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    root = listener.handle_payload(
        {
            "event_id": "EvDryRunClarifyRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> dry run",
                "ts": "1716480960.000001",
            },
        }
    )
    assert root.action == "intent_clarify"

    listener._intent_engine = _intent_engine({"action": "unknown", "confidence": 0.8})
    reply = listener.handle_payload(
        {
            "event_id": "EvDryRunTargetReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "Batman",
                "ts": "1716480961.000001",
                "thread_ts": "1716480960.000001",
            },
        }
    )

    assert reply.action == "intent_dry_run_agent"
    assert control.calls == ["dry-run batman"]
    assert "I ran the dry-run for `batman`." in poster.messages[-1]["text"]


def test_conversation_thread_completed_dry_run_allows_later_read_only_reply(
    tmp_path: Path,
) -> None:
    poster = CardPoster()
    control = SimpleNamespace(calls=[])

    def handle(text, *, trusted, actor_user_id=None):
        control.calls.append(text)
        action = text.split()[0]
        return SimpleNamespace(
            handled=True,
            action=action,
            text=f"*Answer for* `{text}`",
            detail="",
        )

    control.handle = handle
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine(
            {"action": "dry_run_agent", "agent": "batman", "confidence": 0.95}
        ),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    root = listener.handle_payload(
        {
            "event_id": "EvCompletedDryRunRoot",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> dry run Batman",
                "ts": "1716480962.000001",
            },
        }
    )
    assert root.action == "intent_dry_run_agent"

    listener._intent_engine = _intent_engine({"action": "unknown", "confidence": 0.2})
    reply = listener.handle_payload(
        {
            "event_id": "EvCompletedDryRunRunsReply",
            "event": {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "runs",
                "ts": "1716480963.000001",
                "thread_ts": "1716480962.000001",
            },
        }
    )

    assert reply.action == "conversation_control_runs"
    assert control.calls == ["dry-run batman", "runs"]
    assert "Which agent should I dry-run?" not in poster.messages[-1]["text"]


def test_threaded_mention_does_not_claim_existing_human_thread(tmp_path: Path) -> None:
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=SimpleNamespace(
            handle=lambda text, **_: SimpleNamespace(
                handled=True, action="status", text="*Fleet status*", detail=""
            )
        ),
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
        bot_user_id="UALFRED",
    )

    result = listener.handle_payload(
        {
            "event_id": "EvThreadMentionStatus",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "text": "<@UALFRED> status?",
                "ts": "1716480801.000001",
                "thread_ts": "1716480800.000001",
            },
        }
    )

    assert result.action == "intent_status"
    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    assert registry.lookup("C1", "1716480800.000001") is None


def test_mutating_intent_posts_card_and_does_not_execute(tmp_path: Path, monkeypatch) -> None:
    """The central safety gate: a queue intent NEVER runs from prose alone.

    The router posts a confirmation card and registers it, but no queue/hold
    side effect happens. We hard-fail if ``set_issue_pickup`` is ever called.
    """
    import issue_queue

    def _must_not_run(*args, **kwargs):
        raise AssertionError("set_issue_pickup must not run without confirmation")

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _must_not_run)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
    )

    result = listener.handle_payload(_intent_dm("can you queue acme-io/acme-frontend#12 for me"))

    assert result.handled is True
    assert result.action == "intent_confirmation_posted"
    # A card was posted summarizing the interpreted action.
    card = poster.messages[-1]
    assert "acme-io/acme-frontend#12" in card["text"]
    assert card.get("blocks")
    # And a pending record is registered keyed on the card ts.
    from slack_thread_registry import SlackThreadRegistry

    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    record = registry.lookup("D9", poster.card_ts())
    assert record is not None
    assert record.kind == "conversational_action"
    assert record.status == "awaiting_confirmation"
    assert record.parent_repo == "acme-io/acme-frontend"
    assert record.parent_issue == 12


def test_confirmation_card_registers_with_slack_response_object(
    tmp_path: Path,
) -> None:
    # In production chat_postMessage returns a SlackResponse (dict-like, not a
    # real dict). The card ts must still register so the operator's later reaction
    # resolves; otherwise prose-driven confirmation silently breaks.
    poster = SlackResponsePoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
    )

    result = listener.handle_payload(_intent_dm("can you queue acme-io/acme-frontend#12 for me"))
    assert result.action == "intent_confirmation_posted"

    from slack_thread_registry import SlackThreadRegistry

    registry = SlackThreadRegistry(tmp_path / "slack-threads")
    record = registry.lookup("D9", poster.card_ts())
    assert record is not None
    assert record.kind == "conversational_action"
    assert record.parent_issue == 12


def test_confirm_reaction_from_operator_executes_action(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UFOUNDER")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UFOUNDER UTEAM")

    import issue_queue

    calls: list[dict] = []

    def _capture(repo, number, *, hold):
        calls.append({"repo": repo, "number": number, "hold": hold})
        return True, f"{repo}#{number} queued for Alfred"

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _capture)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
    )

    posted = listener.handle_payload(
        _intent_dm("can you queue acme-io/acme-frontend#12", user="UFOUNDER")
    )
    assert posted.action == "intent_confirmation_posted"
    assert calls == []  # nothing ran yet

    # The operator reacts to confirm on the card message.
    confirmed = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UFOUNDER",
        )
    )
    assert confirmed.handled is True
    assert confirmed.action == "intent_queue_issue"
    assert calls == [{"repo": "acme-io/acme-frontend", "number": 12, "hold": False}]


def test_confirm_reaction_from_non_operator_does_not_execute(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UFOUNDER")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UFOUNDER UTEAM")

    import issue_queue

    def _must_not_run(*args, **kwargs):
        raise AssertionError("only the operator may confirm a conversational action")

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _must_not_run)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
    )
    listener.handle_payload(_intent_dm("can you queue acme-io/acme-frontend#12", user="UFOUNDER"))

    # A trusted collaborator (not the operator) reacts: it must NOT execute.
    result = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UTEAM",
        )
    )
    assert result.handled is False
    assert "workspace owner" in result.detail.lower()


def test_cancel_reaction_discards_without_executing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UFOUNDER")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UFOUNDER")

    import issue_queue

    def _must_not_run(*args, **kwargs):
        raise AssertionError("cancel must not execute the action")

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _must_not_run)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "hold_issue",
                "repo": "acme-io/acme-backend",
                "issue": 8,
                "confidence": 0.9,
            }
        ),
        repo_catalog=_intent_catalog(),
    )
    listener.handle_payload(
        _intent_dm("please put acme-io/acme-backend#8 on hold", user="UFOUNDER")
    )

    result = listener.handle_payload(_reaction(reaction="x", ts=poster.card_ts(), user="UFOUNDER"))
    assert result.handled is True
    assert result.action == "intent_cancelled"
    assert "Cancelled" in poster.messages[-1]["text"]


def test_confirm_reaction_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "UFOUNDER")
    monkeypatch.setenv("ALFRED_TRUSTED_SLACK_USER_IDS", "UFOUNDER")

    import issue_queue

    calls: list[dict] = []

    def _capture(repo, number, *, hold):
        calls.append({"repo": repo, "number": number, "hold": hold})
        return True, f"{repo}#{number} queued"

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _capture)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.95,
            }
        ),
        repo_catalog=_intent_catalog(),
    )
    listener.handle_payload(_intent_dm("can you queue acme-io/acme-frontend#12", user="UFOUNDER"))
    first = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UFOUNDER",
            event_id="EvReact1",
        )
    )
    second = listener.handle_payload(
        _reaction(
            reaction="white_check_mark",
            ts=poster.card_ts(),
            user="UFOUNDER",
            event_id="EvReact2",
        )
    )
    assert first.action == "intent_queue_issue"
    assert second.handled is False  # already resolved
    assert len(calls) == 1  # executed exactly once


def test_ambiguous_mutating_intent_asks_and_does_not_post_card(tmp_path: Path, monkeypatch) -> None:
    import issue_queue

    def _must_not_run(*args, **kwargs):
        raise AssertionError("ambiguous intent must not execute")

    monkeypatch.setattr(issue_queue, "set_issue_pickup", _must_not_run)

    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine({"action": "queue_issue", "confidence": 0.85}),
        repo_catalog=_intent_catalog(),
    )
    # No repo / no issue resolvable -> clarify, not a card.
    result = listener.handle_payload(_intent_dm("can you queue that thing for me"))
    assert result.handled is True
    assert result.action == "intent_clarify"
    # The reply is a question, not a confirmation card.
    assert not poster.messages[-1].get("blocks")


def test_command_shaped_message_uses_conversation_router_first(tmp_path: Path) -> None:
    class StubControl:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def handle(self, text, *, trusted, actor_user_id=None):
            self.calls.append(text)
            return SimpleNamespace(handled=True, action="status", text="*Fleet status*", detail="")

    control = StubControl()
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "status_query", "confidence": 0.95}),
        repo_catalog=_intent_catalog(),
    )
    result = listener.handle_payload(_intent_dm("status"))
    assert result.handled is True
    assert result.action == "intent_status"
    assert control.calls == ["status"]


def test_command_fallback_runs_when_router_does_not_classify(tmp_path: Path) -> None:
    class StubControl:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def handle(self, text, *, trusted, actor_user_id=None):
            self.calls.append(text)
            return SimpleNamespace(handled=True, action="status", text="*Fleet status*", detail="")

    control = StubControl()
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        control_handler=control,
        intent_engine=_intent_engine({"action": "unknown", "confidence": 0.2}),
        repo_catalog=_intent_catalog(),
    )

    result = listener.handle_payload(_intent_dm("status"))

    assert result.handled is True
    assert result.action == "control_status"
    assert control.calls == ["status"]


def test_low_confidence_mutating_intent_falls_through_to_planning(
    tmp_path: Path,
) -> None:
    poster = CardPoster()
    listener = SlackPlanningListener(
        state_root=tmp_path,
        poster=poster,
        trusted_user_ids=("U1",),
        intent_engine=_intent_engine(
            {
                "action": "queue_issue",
                "repo": "acme-io/acme-frontend",
                "issue": 12,
                "confidence": 0.1,
            }
        ),
        repo_catalog=_intent_catalog(),
    )
    result = listener.handle_payload(_intent_dm("maybe queue acme-io/acme-frontend#12?"))
    # Sub-threshold confidence is unknown -> safe planning default.
    assert result.handled is True
    assert result.action == "draft_created"
