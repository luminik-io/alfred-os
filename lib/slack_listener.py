"""Slack-native planning listener for Alfred.

The listener turns Slack into an intake and refinement surface without
making chat itself an approval mechanism. It can:

* capture trusted replies in known Alfred plan/report threads;
* create a planning draft from a DM or app mention;
* acknowledge what changed and what still needs scope.

Execution remains gated by the existing reaction approval flow.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from issue_summary import default_engine_invoke, summarize_issue
from planning_assistant import (
    Refiner,
    apply_repository_scope_feedback,
    engine_refiner_from_env,
    plan_feedback_requires_resolution,
    refine_issue_draft,
    render_operator_feedback_ack,
    render_plan_revision_ack,
    render_post_pr_feedback_ack,
    render_post_pr_followup_block,
)
from slack_approval import (
    ThreadFeedback,
    default_slack_client,
    operator_user_id_from_env,
    resolve_bot_token,
    trusted_feedback_user_ids_from_env,
)
from slack_control import SlackControlHandler, is_control_message, parse_control_command
from slack_format import github_issue_link, github_url_link
from slack_intent import (
    ACTION_ASSIGN,
    ACTION_DRY_RUN_AGENT,
    ACTION_HOLD,
    ACTION_PAUSE_AGENT,
    ACTION_QUEUE,
    ACTION_RESUME_AGENT,
    ACTION_RUN_AGENT,
    ACTION_SCHEDULE_AGENT,
    ACTION_STATUS,
    ConversationContext,
    Intent,
    RepoCatalog,
    ambient_enabled,
    ambient_engages,
    classify_intent,
    default_intent_engine_invoke,
    looks_like_followup_reference,
)
from slack_issue_bridge import SlackIssueBridge, build_issue_body
from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry
from slack_thread_status import SlackThreadStatusTracker
from slack_trust import SlackTrustStore
from spec_helper import IssueDraft

ENV_APP_TOKEN = "SLACK_APP_TOKEN"
ENV_ALT_APP_TOKEN = "ALFRED_SLACK_APP_TOKEN"
ENV_BOT_USER_ID = "ALFRED_SLACK_BOT_USER_ID"
ENV_PLAN_ANSWER_ENGINE = "ALFRED_PLAN_THREAD_ANSWER_ENGINE"
ENV_PLAN_ANSWER_TIMEOUT = "ALFRED_PLAN_THREAD_ANSWER_TIMEOUT"
# Optional allowlist of channel ids where ambient listening may engage. Ambient
# never engages in a channel that is not on this list, so even with both
# ambient flags armed the blast radius is exactly the channels named here.
ENV_AMBIENT_CHANNELS = "ALFRED_SLACK_AMBIENT_CHANNELS"
_MAX_STORED_REVISIONS = 50
_DRAFT_REVISION_LOCKS: dict[str, threading.Lock] = {}
_DRAFT_REVISION_LOCKS_GUARD = threading.Lock()


class SlackPoster(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


PlanAnswerer = Callable[[SlackThreadRecord, str, str], str | None]


@dataclass(frozen=True)
class SlackInputEvent:
    event_id: str
    event_type: str
    channel: str
    user: str
    text: str
    ts: str
    thread_ts: str
    channel_type: str = ""
    reaction: str = ""

    @property
    def root_ts(self) -> str:
        return self.thread_ts or self.ts

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_ts and self.thread_ts != self.ts)

    @property
    def conversation_id(self) -> str:
        """Stable id for multi-turn conversation context.

        Multi-turn context (the "do it" / "that one" follow-up resolution) must
        survive consecutive messages in the same conversation. A threaded reply
        already shares a stable ``thread_ts`` with its root, so threads key on
        ``thread:{channel}:{root_ts}`` and stay isolated per thread. But a DM or
        a non-threaded @mention gives every message a fresh ``ts`` and no
        ``thread_ts``, so keying those on ``root_ts`` would put each turn in its
        own bucket and a bare follow-up could never find the prior target. For
        those we key on the conversation participant instead
        (``dm:{channel}:{user}``), which is stable across the operator's
        consecutive non-threaded messages in the same channel / DM. The distinct
        ``thread:`` / ``dm:`` prefixes keep the two id spaces from ever
        colliding (a thread-root ts can never equal a user id).
        """
        if self.is_thread_reply:
            return f"thread:{self.channel}:{self.root_ts}"
        return f"dm:{self.channel}:{self.user}"

    @property
    def is_reaction(self) -> bool:
        return self.event_type == "reaction_added"

    @property
    def is_direct_intake(self) -> bool:
        return self.event_type == "app_mention" or self.channel_type == "im"

    def mentions_bot(self, bot_user_id: str) -> bool:
        """True iff this message carries a literal ``<@BOT>`` mention token.

        Slack does NOT strip the mention token from a message's text (the
        ``<@U...>`` form has no ``|`` and so survives :func:`_clean_slack_text`),
        which lets the ambient path recognise a channel message that the bot
        will ALSO receive as a separate ``app_mention`` event. Both the raw and
        the bare ``@BOT`` forms are honored, mirroring :func:`ambient_engages`.
        """
        bot = (bot_user_id or "").strip()
        if not bot:
            return False
        text = self.text or ""
        return f"<@{bot}>" in text or f"@{bot}" in text

    @property
    def is_plain_channel_message(self) -> bool:
        """A plain ``message`` event in a channel (not a DM, not an @mention).

        This is the ambient-listening candidate: ordinary channel chatter the
        listener ignores today. ``channel_type`` is ``channel`` / ``group`` for
        public / private channels and ``im`` for DMs; DMs and @mentions are
        already handled as direct intake, so we exclude them here.
        """
        return (
            self.event_type == "message"
            and self.channel_type not in {"", "im"}
            and not self.is_direct_intake
        )


@dataclass(frozen=True)
class ListenerResult:
    handled: bool
    action: str
    detail: str = ""
    draft_path: str = ""
    thread_kind: str = ""
    readiness_ok: bool | None = None
    readiness_score: int | None = None


class SeenEventStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def mark_seen(self, event_id: str) -> bool:
        if not event_id:
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{_safe_event_id(event_id)}.seen"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return True
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_utc_now() + "\n")
        return False


class SlackPlanningListener:
    def __init__(
        self,
        *,
        registry: SlackThreadRegistry | None = None,
        state_root: Path | None = None,
        poster: SlackPoster | None = None,
        trusted_user_ids: Iterable[str] | None = None,
        bot_user_id: str | None = None,
        seen_store: SeenEventStore | None = None,
        refiner: Refiner | None = None,
        memory_provider: Any | None = None,
        now: Callable[[], datetime] | None = None,
        bridge: SlackIssueBridge | None = None,
        status_tracker: SlackThreadStatusTracker | None = None,
        control_handler: SlackControlHandler | None = None,
        intent_engine: Callable[[str], str] | None = None,
        repo_catalog: RepoCatalog | None = None,
        conversation_context: ConversationContext | None = None,
        ambient_channels: Iterable[str] | None = None,
        plan_answerer: PlanAnswerer | None = None,
    ) -> None:
        self.state_root = state_root or _default_state_root()
        self.registry = registry or SlackThreadRegistry(self.state_root / "slack-threads")
        self.poster = poster
        self.bridge = bridge if bridge is not None else SlackIssueBridge()
        self.status_tracker = (
            status_tracker
            if status_tracker is not None
            else SlackThreadStatusTracker(
                root=self.state_root / "slack-thread-status",
                poster=poster,
            )
        )
        operator = operator_user_id_from_env()
        self._operator_user_id = operator
        self._static_trusted_user_ids = (
            set(trusted_user_ids) if trusted_user_ids is not None else None
        )
        self.control_handler = (
            control_handler
            if control_handler is not None
            else SlackControlHandler(
                operator_user_id=operator,
                trust_store=SlackTrustStore.from_state_root(self.state_root),
                state_root=self.state_root,
                memory_provider=memory_provider,
            )
        )
        self.bot_user_id = bot_user_id or (os.environ.get(ENV_BOT_USER_ID) or "").strip()
        self.seen = seen_store or SeenEventStore(self.state_root / "slack-listener" / "seen")
        self.refiner = refiner
        self.memory_provider = memory_provider
        self._plan_answerer = (
            plan_answerer if plan_answerer is not None else _default_plan_thread_answerer()
        )
        # The conversational intent router is an additive fallback for the
        # previously-unrouted free-text case. When no engine is injected we
        # resolve one from env; it returns ``None`` unless the router is
        # explicitly enabled, so the listener keeps its exact prior behavior
        # by default. The repo alias catalog is built once from the canonical
        # repo map plus the env queue allowlist.
        if intent_engine is not None:
            self._intent_engine: Callable[[str], str] | None = intent_engine
        else:
            self._intent_engine = default_intent_engine_invoke()
        self._repo_catalog = (
            repo_catalog if repo_catalog is not None else RepoCatalog.from_environment()
        )
        # Bounded, in-process multi-turn context so follow-ups ("yes that one",
        # "do it") resolve against the previous turn's interpreted target. It is
        # never persisted and never authority for a mutation: every mutating
        # intent still surfaces the workspace-owner confirmation card.
        self._conversation = (
            conversation_context if conversation_context is not None else ConversationContext()
        )
        # Channel allowlist for ambient listening (empty == ambient never
        # engages, even when both ambient flags are armed).
        self._ambient_channels = (
            set(ambient_channels) if ambient_channels is not None else _ambient_channels_from_env()
        )
        self._now = now or (lambda: datetime.now(UTC))

    def handle_payload(self, payload: dict[str, Any]) -> ListenerResult:
        event = parse_slack_payload(payload)
        if event is None:
            return ListenerResult(False, "ignored", "unsupported Slack event")
        if self.seen.mark_seen(event.event_id):
            return ListenerResult(False, "duplicate", "event already processed")
        if self.bot_user_id and event.user == self.bot_user_id:
            return ListenerResult(False, "ignored", "bot self-message")
        trusted_user_ids = self._trusted_user_ids()
        if not trusted_user_ids:
            return ListenerResult(False, "ignored", "no trusted Slack users configured")
        if event.user not in trusted_user_ids:
            return ListenerResult(False, "ignored", "untrusted Slack user")

        record = self.registry.lookup(event.channel, event.root_ts)
        if record is not None and event.is_reaction:
            return self._handle_thread_reaction(event, record)
        if record is not None and event.is_thread_reply:
            return self._handle_registered_thread(event, record)
        if event.is_reaction:
            return ListenerResult(False, "ignored", "reaction is not on a registered thread")
        if event.is_direct_intake:
            result = self._handle_direct_intake(event)
            return self._remember_conversation_root(event, result)
        if event.is_plain_channel_message:
            result = self._maybe_handle_ambient(event)
            return self._remember_conversation_root(event, result)
        return ListenerResult(False, "ignored", "message is not a registered thread or intake")

    def _handle_thread_reaction(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        """An approval reaction on a registered draft thread can create an issue.

        Reactions on non-draft threads (plan/report/pr) carry no approval
        authority here: the reaction approval gate in ``slack_approval`` owns
        plan execution. This path bridges a *draft* into a queued issue, and
        resolves the workspace owner's confirm/cancel on a *conversational_action*
        card surfaced by the intent router.
        """
        if record.kind == "conversational_action":
            return self._handle_conversational_action_reaction(event, record)
        if record.kind != "draft":
            return ListenerResult(False, "ignored", "reaction is not on a draft thread")
        if not self.bridge.is_approval(reaction=event.reaction):
            return ListenerResult(False, "ignored", "reaction is not an approval token")
        return self._attempt_issue_conversion(event, record)

    def _handle_registered_thread(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        feedback = ThreadFeedback(author=event.user, text=event.text, ts=event.ts)
        feedback_path = self.registry.append_feedback(
            record, author=feedback.author, text=feedback.text, ts=feedback.ts
        )
        if record.kind == "draft":
            return self._handle_draft_revision(event, record, feedback)
        if record.kind == "conversation":
            return self._handle_conversation_thread_reply(event)
        if record.kind == "plan":
            return self._handle_plan_revision(event, record, feedback, feedback_path)
        if record.kind in {"report", "pr", "followup"}:
            followup_path = self._write_followup_context(record, feedback)
            pr_urls = _string_list(record.metadata.get("created") or record.metadata.get("pr_urls"))
            ack = render_post_pr_feedback_ack(
                [feedback.text],
                pr_urls=pr_urls,
                issue_url=_issue_url_from_record(record),
            )
            action = "captured_followup"
            if followup_path is not None:
                metadata = dict(record.metadata)
                metadata["followup_path"] = str(followup_path)
                metadata["last_followup_at"] = _utc_now()
                self.registry.register(
                    SlackThreadRecord(
                        kind=record.kind,
                        channel=record.channel,
                        thread_ts=record.thread_ts,
                        codename=record.codename,
                        firing_id=record.firing_id,
                        title=record.title,
                        status="followup_waiting",
                        parent_repo=record.parent_repo,
                        parent_issue=record.parent_issue,
                        plan_path=record.plan_path,
                        draft_path=record.draft_path,
                        created_at=record.created_at,
                        metadata=metadata,
                    )
                )
        else:
            ack = render_operator_feedback_ack([feedback.text])
            action = "captured_plan_feedback"
        self._post_thread_ack(event.channel, event.root_ts, ack or "*Feedback captured*")
        return ListenerResult(True, action, thread_kind=record.kind)

    def _remember_conversation_root(
        self, event: SlackInputEvent, result: ListenerResult
    ) -> ListenerResult:
        """Keep top-level Alfred-started Slack threads conversational.

        A trusted top-level mention or Alfred-addressed ambient message can start
        with a status question, clarification, or confirmation card rather than
        a planning draft. Register that root so later replies in the same thread
        do not need another @mention. We intentionally do not claim a pre-existing
        human-owned thread when the first Alfred mention happened as a reply.
        """
        if not result.handled or event.is_reaction or event.is_thread_reply:
            return result
        if result.action.startswith("draft_"):
            return result
        if not (event.is_direct_intake or event.is_plain_channel_message):
            return result
        self.registry.register(
            SlackThreadRecord(
                kind="conversation",
                channel=event.channel,
                thread_ts=event.root_ts,
                codename="slack",
                title=_thread_title_from_text(event.text),
                status="open",
                metadata={
                    "source": "slack-conversation",
                    "last_action": result.action,
                    "origin_event_type": event.event_type,
                    "requested_by": event.user,
                },
            )
        )
        self._mirror_context_to_thread(event)
        return result

    def _mirror_context_to_thread(self, event: SlackInputEvent) -> None:
        thread_conversation_id = f"thread:{event.channel}:{event.root_ts}"
        if event.conversation_id == thread_conversation_id:
            return
        for turn in self._conversation.recent(event.conversation_id):
            self._conversation.record(
                thread_conversation_id,
                text=turn.text,
                action=turn.action,
                repo=turn.repo,
                issue=turn.issue,
            )

    def _handle_conversation_thread_reply(self, event: SlackInputEvent) -> ListenerResult:
        """Route trusted replies in an Alfred-started thread without re-mentioning."""
        routed = self._maybe_route_intent(event)
        if routed is not None:
            return routed

        if _is_read_only_control_text(event.text):
            control = self.control_handler.handle(
                event.text,
                trusted=True,
                actor_user_id=event.user,
            )
            if control.handled:
                self._post_thread_ack(event.channel, event.root_ts, control.text)
                return ListenerResult(
                    True,
                    f"conversation_control_{control.action}",
                    detail=control.detail,
                )

        return ListenerResult(False, "ignored", "conversation reply is not actionable")

    def _handle_plan_revision(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
        feedback: ThreadFeedback,
        feedback_path: Path,
    ) -> ListenerResult:
        all_feedback = _read_feedback_texts(feedback_path) or (feedback.text,)
        base_repos = _string_list(record.metadata.get("affected_repos"))
        default_org = (
            record.parent_repo.split("/", 1)[0]
            if record.parent_repo and "/" in record.parent_repo
            else None
        )
        revised_repos = apply_repository_scope_feedback(
            base_repos,
            all_feedback,
            default_org=default_org,
        )
        child_count = _revised_child_count(record.metadata, base_repos, revised_repos)
        if _is_plan_thread_question(feedback.text):
            ack = self._answer_plan_thread_question(
                record,
                feedback.text,
                revised_repos=revised_repos,
                child_count=child_count,
            )
            metadata = dict(record.metadata or {})
            metadata.update(
                {
                    "last_plan_question_at": _utc_now(),
                    "last_plan_question": _strip_plan_question_prefix(feedback.text),
                    "revised_repos": list(revised_repos),
                }
            )
            updated_record = self.registry.register(
                SlackThreadRecord(
                    kind=record.kind,
                    channel=record.channel,
                    thread_ts=record.thread_ts,
                    codename=record.codename,
                    firing_id=record.firing_id,
                    title=record.title,
                    status=record.status or "open",
                    parent_repo=record.parent_repo,
                    parent_issue=record.parent_issue,
                    plan_path=record.plan_path,
                    draft_path=record.draft_path,
                    created_at=record.created_at,
                    metadata=metadata,
                )
            )
            self._post_thread_ack(event.channel, event.root_ts, ack or "*Answer unavailable*")
            return ListenerResult(
                True,
                "plan_question_answered",
                thread_kind=updated_record.kind,
                readiness_ok=not bool(metadata.get("plan_requires_resolution")),
            )
        requires_resolution = plan_feedback_requires_resolution(all_feedback)
        revision_path, revision_count = self._write_plan_revision_context(
            record,
            feedback,
            all_feedback,
            revised_repos,
            requires_resolution=requires_resolution,
        )
        metadata = dict(record.metadata or {})
        metadata.update(
            {
                "last_plan_feedback_at": _utc_now(),
                "plan_revision_count": revision_count,
                "plan_requires_resolution": requires_resolution,
                "revised_repos": list(revised_repos),
            }
        )
        if revision_path is not None:
            metadata["plan_revision_path"] = str(revision_path)
        updated_record = self.registry.register(
            SlackThreadRecord(
                kind=record.kind,
                channel=record.channel,
                thread_ts=record.thread_ts,
                codename=record.codename,
                firing_id=record.firing_id,
                title=record.title,
                status="needs_resolution" if requires_resolution else "revised",
                parent_repo=record.parent_repo,
                parent_issue=record.parent_issue,
                plan_path=record.plan_path,
                draft_path=record.draft_path,
                created_at=record.created_at,
                metadata=metadata,
            )
        )
        ack = render_plan_revision_ack(
            all_feedback,
            revised_repos=revised_repos,
            child_count=child_count,
        )
        self._post_thread_ack(event.channel, event.root_ts, ack or "*Plan feedback captured*")
        return ListenerResult(
            True,
            "plan_revised",
            detail=str(revision_path or ""),
            thread_kind=updated_record.kind,
            readiness_ok=not requires_resolution,
        )

    def _answer_plan_thread_question(
        self,
        record: SlackThreadRecord,
        question: str,
        *,
        revised_repos: Iterable[str],
        child_count: int | None,
    ) -> str:
        plan_markdown = _read_text_file(record.plan_path)
        clean_question = _strip_plan_question_prefix(question)
        engine_answer = None
        if self._plan_answerer is not None:
            try:
                engine_answer = self._plan_answerer(record, clean_question, plan_markdown)
            except Exception as exc:
                print(
                    f"[SLACK-LISTENER-WARN] plan answerer failed for "
                    f"{record.channel}/{record.thread_ts}: {exc}",
                    file=sys.stderr,
                )
        if engine_answer and engine_answer.strip():
            lines = ["*Answer*", "", engine_answer.strip()]
        else:
            lines = _fallback_plan_question_answer(
                record,
                clean_question,
                plan_markdown,
                revised_repos=revised_repos,
                child_count=child_count,
            )
        lines.extend(
            [
                "",
                "*Safety:* this did not change the plan or approve execution. "
                "Reply naturally with changes, or use `open question:` for "
                "something that must block approval.",
            ]
        )
        return "\n".join(lines)

    def _handle_direct_intake(self, event: SlackInputEvent) -> ListenerResult:
        # Conversation is the primary Slack surface. Try the natural-language
        # router first, even for old command-shaped messages like "run batman".
        # Literal commands remain as a backcompat fallback when the router is
        # disabled or decides the text is not a known intent.
        routed = self._maybe_route_intent(event)
        if routed is not None:
            return routed

        # Backcompat fallback: a trusted direct message/mention that LEADS with
        # a control verb acts on the fleet instead of opening a planning draft.
        # The user is already trust-gated in ``handle_payload``; the handler
        # re-checks. Free-form prose falls through to planning intake.
        if is_control_message(event.text):
            control = self.control_handler.handle(
                event.text,
                trusted=True,
                actor_user_id=event.user,
            )
            if control.handled:
                self._post_thread_ack(event.channel, event.root_ts, control.text)
                return ListenerResult(
                    True,
                    f"control_{control.action}",
                    detail=control.detail,
                )

        draft = draft_from_slack_text(event.text)
        refined = refine_issue_draft(
            draft,
            [],
            refiner=self.refiner,
            memory_provider=self.memory_provider,
        )
        try:
            draft_path = self._save_draft(
                event,
                refined.draft,
                refined.issue_body,
                refined.spec_body,
                readiness=refined.readiness,
                memory=refined.memory,
            )
            memory_candidate_ids = self._propose_planning_memory_candidates(
                event,
                refined,
                draft_path,
                source="slack-draft",
            )
            _append_memory_candidate_ids(draft_path, memory_candidate_ids)
        except OSError as exc:
            print(
                f"[SLACK-LISTENER-WARN] could not save planning draft for "
                f"{event.channel}/{event.root_ts}: {exc}",
                file=sys.stderr,
            )
            self._post_thread_ack(
                event.channel,
                event.root_ts,
                "*Planning draft could not be saved*\n\n"
                "Please check local disk space and permissions, then send the request again.",
            )
            return ListenerResult(
                True,
                "draft_save_failed",
                detail=str(exc),
                readiness_ok=refined.readiness.ok,
                readiness_score=refined.readiness.score,
            )
        registered_thread_ts = event.ts if event.is_thread_reply else event.root_ts
        record = self.registry.register(
            SlackThreadRecord(
                kind="draft",
                channel=event.channel,
                thread_ts=registered_thread_ts,
                codename="planning",
                title=refined.draft.title,
                status="ready" if refined.readiness.ok else "needs_scope",
                draft_path=str(draft_path),
                metadata={
                    "source": "slack",
                    "readiness_score": refined.readiness.score,
                },
            )
        )
        self._post_thread_ack(event.channel, event.root_ts, render_draft_ack(refined))
        return ListenerResult(
            True,
            "draft_created",
            draft_path=str(draft_path),
            thread_kind=record.kind,
            readiness_ok=refined.readiness.ok,
            readiness_score=refined.readiness.score,
        )

    # ------------------------------------------------------------------
    # Conversational intent router (additive fallback)
    # ------------------------------------------------------------------

    def _maybe_route_intent(self, event: SlackInputEvent) -> ListenerResult | None:
        """Try to interpret free-text prose as a known intent.

        Returns a :class:`ListenerResult` when the router handled the message
        (answered a status query, asked a clarifying question, or surfaced a
        confirmation card for a mutating action), or ``None`` to let the caller
        fall through to the unchanged planning intake.

        The router never executes a mutating action. A ``queue`` / ``hold``
        intent only ever produces a workspace-owner confirmation card; the action runs
        in :meth:`_execute_confirmed_intent` after that person reacts.

        Multi-turn: a mutating intent that resolved no repo is augmented from
        the previous turn's target when the message is a short back-reference
        ("do it", "that one"). The borrowed target is still just a SUGGESTION;
        it only ever feeds the confirmation card, never an auto-execution.
        """
        if self._intent_engine is None:
            return None

        intent = classify_intent(
            event.text,
            engine_invoke=self._intent_engine,
            catalog=self._repo_catalog,
        )
        intent = self._augment_intent_from_context(event, intent)

        if intent.action == ACTION_STATUS:
            self._conversation.record(
                event.conversation_id,
                text=event.text,
                action=intent.action,
            )
            return self._answer_status_query(event)
        if intent.action == ACTION_DRY_RUN_AGENT:
            self._conversation.record(
                event.conversation_id,
                text=event.text,
                action=intent.action,
            )
            return self._answer_dry_run_agent(event, intent)
        if intent.action in {
            ACTION_ASSIGN,
            ACTION_QUEUE,
            ACTION_HOLD,
            ACTION_RUN_AGENT,
            ACTION_PAUSE_AGENT,
            ACTION_RESUME_AGENT,
            ACTION_SCHEDULE_AGENT,
        }:
            self._conversation.record(
                event.conversation_id,
                text=event.text,
                action=intent.action,
                repo=intent.repo,
                issue=intent.issue,
            )
            return self._propose_intent_action(event, intent)
        # plan_request / unknown / anything low-confidence: fall through to the
        # safe planning default (no result), preserving prior behavior.
        return None

    def _augment_intent_from_context(self, event: SlackInputEvent, intent: Intent) -> Intent:
        """Fill a mutating intent's missing target from recent conversation.

        Only triggers when (a) the intent is mutating, (b) it resolved no repo
        of its own, and (c) the message is a short back-reference. In that case
        we borrow the most recent ``(repo, issue)`` target recorded for this
        conversation. If the current message carried its own issue number we
        keep it (the operator may say "do that one but issue 5"). Anything that
        already resolved a repo is left untouched.
        """
        if intent.action not in {ACTION_ASSIGN, ACTION_QUEUE, ACTION_HOLD}:
            return intent
        if intent.repo:
            return intent
        if not looks_like_followup_reference(event.text):
            return intent
        prev_repo, prev_issue = self._conversation.last_target(event.conversation_id)
        if not prev_repo:
            return intent
        params = dict(intent.params or {})
        params["context_repo"] = prev_repo
        if prev_issue is not None and intent.issue is None:
            params["context_issue"] = prev_issue
        resolved_issue = intent.issue if intent.issue is not None else prev_issue
        # Re-derive the clarification against the now-augmented entities so a
        # borrowed-but-still-incomplete target still asks rather than guesses.
        from slack_intent import _clarify_for_mutating

        clarification = _clarify_for_mutating(intent.action, prev_repo, resolved_issue, [])
        return Intent(
            action=intent.action,
            repo=prev_repo,
            issue=resolved_issue,
            params=params,
            confidence=intent.confidence,
            clarification=clarification,
        )

    def _maybe_handle_ambient(self, event: SlackInputEvent) -> ListenerResult:
        """Engage a plain channel message ONLY when armed and clearly relevant.

        Four gates, cheapest first, before any engine call:

        1. ``ambient_enabled()`` (both ``ALFRED_INTENT_ROUTER_ENABLED`` and
           ``ALFRED_SLACK_AMBIENT``). Off by default => never engages.
        2. The channel is on the ambient allowlist (empty => never engages).
        3. The message does NOT mention the bot. A channel post that @mentions
           the bot is also delivered as a separate ``app_mention`` event (and
           handled there as direct intake), so engaging it here too would
           process the same message twice. We let the ``app_mention`` copy own
           it and skip the ambient copy.
        4. ``ambient_engages`` (deterministic: addressed to Alfred or a tight
           fleet-specific action cue). Ordinary chatter never reaches the engine.

        When all pass, the message is routed through the SAME intent path as a
        DM / @mention, except ambient NEVER falls through to opening a planning
        draft: unclear channel prose is left alone, not turned into a draft.
        Mutating intents still only ever surface the confirmation card.
        """
        if not ambient_enabled():
            return ListenerResult(False, "ignored", "ambient listening disabled")
        if event.channel not in self._ambient_channels:
            return ListenerResult(False, "ignored", "channel is not on the ambient allowlist")
        # A bot-mention channel message also arrives as an ``app_mention``
        # event; let that copy handle it and skip this one so the same message
        # is never processed twice.
        if event.mentions_bot(self.bot_user_id):
            return ListenerResult(
                False,
                "ignored",
                "bot-mention message is handled as app_mention, not ambient",
            )
        if not ambient_engages(event.text, bot_user_id=self.bot_user_id):
            return ListenerResult(False, "ignored", "ambient message is ordinary chatter")

        routed = self._maybe_route_intent(event)
        if routed is not None:
            return routed

        # A channel message may still LEAD with a literal control verb (e.g.
        # "status"); honor that as a backcompat fallback when the router is
        # disabled or cannot classify the text.
        if is_control_message(event.text):
            control = self.control_handler.handle(
                event.text,
                trusted=True,
                actor_user_id=event.user,
            )
            if control.handled:
                self._post_thread_ack(event.channel, event.root_ts, control.text)
                return ListenerResult(
                    True,
                    f"ambient_control_{control.action}",
                    detail=control.detail,
                )

        # Engaged but the router recognised nothing actionable: stay quiet. We
        # deliberately do NOT open a planning draft from ambient channel prose.
        return ListenerResult(False, "ignored", "ambient message engaged but not actionable")

    def _answer_status_query(self, event: SlackInputEvent) -> ListenerResult:
        """Answer a read-only status question directly (no confirmation gate).

        Status is non-mutating, so it is safe to answer immediately by reusing
        the existing read-only control handlers (``status`` / ``runs`` /
        ``plans``). The user was already trust-gated in :meth:`handle_payload`.

        The question's natural-language flavor selects the most relevant
        handler ("what did you ship?" -> recent runs, "what's blocked / what
        needs scope?" -> the planning inbox, otherwise fleet status) and the
        handler's structured output is framed with a short conversational
        lead-in instead of being dumped raw.
        """
        verb, lead_in = _status_query_plan(event.text)
        control = self.control_handler.handle(
            verb,
            trusted=True,
            actor_user_id=event.user,
        )
        body = (control.text or "").strip()
        if not body:
            text = "I could not read the fleet state just now. Try again in a moment."
        else:
            text = f"{lead_in}\n\n{body}" if lead_in else body
        self._post_thread_ack(event.channel, event.root_ts, text)
        return ListenerResult(
            True,
            "intent_status",
            detail=f"natural-language status query -> {verb}",
        )

    def _answer_dry_run_agent(self, event: SlackInputEvent, intent: Intent) -> ListenerResult:
        """Run a conversational dry-run immediately.

        ``dry_run_agent`` is non-mutating, so it can reuse the read-only
        ``dry-run`` control path without a confirmation card. Missing agent
        names still clarify rather than guessing.
        """
        if intent.needs_clarification:
            self._post_thread_ack(event.channel, event.root_ts, intent.clarification)
            return ListenerResult(
                True,
                "intent_clarify",
                detail="dry_run_agent: missing agent",
            )
        control = self.control_handler.handle(
            f"dry-run {intent.agent}",
            trusted=True,
            actor_user_id=event.user,
        )
        text = (control.text or "").strip()
        control_failed = _control_result_failed(control)
        if control_failed or not text:
            text = (
                text
                if text
                else (
                    f"I could not dry-run `{intent.agent}` just now. "
                    "Check that the agent codename exists and try again."
                )
            )
        else:
            text = f"I ran the dry-run for `{intent.agent}`.\n\n{text}"
        self._post_thread_ack(event.channel, event.root_ts, text)
        return ListenerResult(
            True,
            "intent_dry_run_agent_failed" if control_failed else "intent_dry_run_agent",
            detail=control.detail or f"natural-language dry-run -> {intent.agent}",
        )

    def _propose_intent_action(self, event: SlackInputEvent, intent: Intent) -> ListenerResult:
        """Surface a workspace-owner-confirmable card for a mutating intent.

        SAFETY: this never mutates anything. When entities are missing or
        ambiguous it asks a clarifying question. Otherwise it posts a Block Kit
        confirmation card summarizing the interpreted action and registers a
        ``conversational_action`` thread keyed on that card's message ts. The
        action only runs after the workspace owner reacts to confirm
        (:meth:`_execute_confirmed_intent`); a cancel reaction discards it.
        """
        if intent.needs_clarification:
            self._post_thread_ack(event.channel, event.root_ts, intent.clarification)
            return ListenerResult(
                True,
                "intent_clarify",
                detail=f"{intent.action}: ambiguous or missing entity",
            )

        verb = _intent_action_verb(intent.action)
        target = _intent_action_target(intent)
        text, blocks = render_intent_confirmation(intent)
        posted = self._post_message(event.channel, text, blocks=blocks, thread_ts=event.root_ts)
        card_ts = str((posted or {}).get("ts") or "")
        if not card_ts:
            # We could not post the card (no poster, or API error). Do nothing
            # mutating; fall back to planning intake by reporting unhandled via
            # a benign result so the caller's contract still holds.
            return ListenerResult(
                True,
                "intent_card_unposted",
                detail="confirmation card could not be posted; nothing changed",
            )

        self.registry.register(
            SlackThreadRecord(
                kind="conversational_action",
                channel=event.channel,
                thread_ts=card_ts,
                codename="intent",
                title=f"{verb} {target}",
                status="awaiting_confirmation",
                parent_repo=intent.repo,
                parent_issue=intent.issue,
                metadata={
                    "source": "slack-intent",
                    "intent_action": intent.action,
                    "repo": intent.repo,
                    "issue": intent.issue,
                    "agent": intent.agent,
                    "schedule": intent.schedule,
                    "confidence": intent.confidence,
                    "origin_ts": event.root_ts,
                    "requested_by": event.user,
                    "raw_text": intent.params.get("raw_text", ""),
                },
            )
        )
        return ListenerResult(
            True,
            "intent_confirmation_posted",
            detail=f"{verb} {target} awaiting confirmation",
        )

    def _handle_conversational_action_reaction(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        """Resolve a reaction on a pending conversational-action card.

        Only the workspace owner's confirm reaction executes the action; a cancel
        reaction discards it. Any other reactor or any non-approval/non-reject
        reaction is ignored, so the card stays pending. This reuses the
        same single-owner authority that the reaction-approval gate relies on:
        the listener's ``_operator_user_id``.
        """
        if record.status not in {"awaiting_confirmation", ""}:
            # Already resolved (confirmed or cancelled). Reactions are
            # idempotent: never double-execute.
            return ListenerResult(False, "ignored", "conversational action already resolved")
        if not self._operator_user_id or event.user != self._operator_user_id:
            # Only the configured workspace owner can confirm a mutating action. A trusted
            # collaborator's reaction never executes.
            return ListenerResult(
                False, "ignored", "only the workspace owner can confirm this action"
            )

        if _is_cancel_reaction(event.reaction):
            self.registry.mark_status(record, "cancelled")
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                "*Cancelled.* Nothing changed.",
            )
            return ListenerResult(
                True,
                "intent_cancelled",
                detail="workspace owner cancelled the proposed action",
            )

        if not _is_confirm_reaction(event.reaction):
            return ListenerResult(False, "ignored", "reaction is not a confirm or cancel token")

        return self._execute_confirmed_intent(event, record)

    def _execute_confirmed_intent(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        """Run the assign / queue / hold action the workspace owner just confirmed.

        This is the ONLY place a conversational mutating action executes, and
        only after the workspace owner's confirm reaction on the card. The actions use
        the same queue / assignment primitives as the leading-verb fallback, so
        they inherit the same allowlist and validation guards.
        """
        from issue_assignment import assign_issue
        from issue_queue import set_issue_pickup

        metadata = dict(record.metadata or {})
        action = str(metadata.get("intent_action") or "")
        if action in {
            ACTION_RUN_AGENT,
            ACTION_PAUSE_AGENT,
            ACTION_RESUME_AGENT,
            ACTION_SCHEDULE_AGENT,
        }:
            return self._execute_confirmed_agent_intent(event, record, action)

        repo = record.parent_repo or str(metadata.get("repo") or "")
        issue = record.parent_issue
        if issue is None:
            raw_issue = metadata.get("issue")
            issue = int(raw_issue) if isinstance(raw_issue, int) else None

        if action not in {ACTION_ASSIGN, ACTION_QUEUE, ACTION_HOLD} or not repo or issue is None:
            self.registry.mark_status(record, "error")
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                "*Could not run that.* The confirmed action was incomplete; nothing changed.",
            )
            return ListenerResult(False, "intent_invalid", "confirmed action missing repo/issue")

        if action == ACTION_ASSIGN:
            assignment = assign_issue(repo, issue)
            self.registry.mark_status(record, "confirmed" if assignment.ok else "failed")
            if not assignment.ok:
                reason = assignment.error or assignment.detail
                self._post_thread_ack(
                    event.channel,
                    record.thread_ts,
                    f"*Assignment did not run.* {reason}",
                )
                return ListenerResult(
                    True,
                    "intent_assign_issue_failed",
                    detail=reason,
                )
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                _assignment_ack_text(repo, issue, assignment.detail),
            )
            return ListenerResult(
                True,
                "intent_assign_issue",
                detail=assignment.detail,
            )

        hold = action == ACTION_HOLD
        ok, detail = set_issue_pickup(repo, issue, hold=hold)
        # Mark resolved regardless of the gh outcome so a repeated reaction
        # never re-runs the command.
        self.registry.mark_status(record, "confirmed" if ok else "failed")
        if not ok:
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                "*Queue update failed (gh error).*",
            )
            return ListenerResult(
                True,
                f"intent_{action}_failed",
                detail=detail,
            )
        emoji = ":raised_hand:" if hold else ":inbox_tray:"
        self._post_thread_ack(event.channel, record.thread_ts, f"{emoji} {detail}.")
        return ListenerResult(
            True,
            f"intent_{action}",
            detail=detail,
        )

    def _execute_confirmed_agent_intent(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
        action: str,
    ) -> ListenerResult:
        """Run a confirmed conversational scheduler action."""
        metadata = dict(record.metadata or {})
        agent = str(metadata.get("agent") or "").strip()
        command = _control_command_for_agent_intent(action)
        schedule = str(metadata.get("schedule") or "").strip()
        if not command or not agent:
            self.registry.mark_status(record, "error")
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                "*Could not run that.* The confirmed agent action was incomplete; nothing changed.",
            )
            return ListenerResult(
                False,
                "intent_invalid",
                "confirmed agent action missing command or agent",
            )

        control_text = f"{command} {agent}"
        if action == ACTION_SCHEDULE_AGENT:
            if not schedule:
                self.registry.mark_status(record, "error")
                self._post_thread_ack(
                    event.channel,
                    record.thread_ts,
                    "*Could not run that.* The confirmed schedule change was incomplete; nothing changed.",
                )
                return ListenerResult(
                    False,
                    "intent_invalid",
                    "confirmed schedule action missing cadence",
                )
            control_text = f"{command} {agent} {schedule}"

        control = self.control_handler.handle(
            control_text,
            trusted=True,
            actor_user_id=event.user,
        )
        control_failed = _control_result_failed(control)
        self.registry.mark_status(record, "failed" if control_failed else "confirmed")
        if control_failed:
            self._post_thread_ack(
                event.channel,
                record.thread_ts,
                (control.text or f"*Could not {command}* `{agent}`. Nothing changed.").strip(),
            )
            return ListenerResult(
                True,
                f"intent_{action}_failed",
                detail=control.detail,
            )
        self._post_thread_ack(
            event.channel,
            record.thread_ts,
            (control.text or f"*{_intent_action_verb(action).capitalize()}* `{agent}`.").strip(),
        )
        return ListenerResult(
            True,
            f"intent_{action}",
            detail=control.detail,
        )

    def _post_message(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict] | None = None,
        thread_ts: str | None = None,
    ) -> Any | None:
        """Post a Slack message and return the API response (with ``ts``).

        Unlike :meth:`_post_thread_ack` this surfaces the response so the
        caller can register the posted message's ts (needed to resolve a later
        confirm reaction). Best-effort: returns ``None`` when there is no
        poster or the API call fails.
        """
        if self.poster is None or not text.strip():
            return None
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            resp = self.poster.chat_postMessage(**kwargs)
        except Exception:
            return None
        # The Slack SDK returns a ``SlackResponse`` (dict-like, supports
        # ``.get``/``["ts"]``) in production; tests may inject a plain dict.
        # Accept either via the mapping interface the caller uses; reject only a
        # missing or non-mapping response so the card ts still registers.
        return resp if hasattr(resp, "get") else None

    def _handle_draft_revision(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
        feedback: ThreadFeedback,
    ) -> ListenerResult:
        # Explicit, unambiguous approval text turns the saved draft into a
        # queued GitHub issue. Anything else is normal refinement. The user
        # is already trust-gated in handle_payload; the bridge re-checks.
        if self.bridge.is_approval(text=feedback.text):
            return self._attempt_issue_conversion(event, record)

        payload_path = Path(record.draft_path).expanduser() if record.draft_path else None
        if payload_path is None:
            return self._ack_unavailable_draft(event, record, "saved draft path is unavailable")

        with _draft_revision_lock(payload_path):
            payload = _read_draft_payload(payload_path)
            draft = _draft_from_payload(payload) if payload else None
            if payload is None or draft is None:
                print(
                    f"[SLACK-LISTENER-WARN] saved draft unavailable for "
                    f"{event.channel}/{event.root_ts}: {payload_path}",
                    file=sys.stderr,
                )
                return self._ack_unavailable_draft(event, record, "saved draft is unavailable")

            refined = refine_issue_draft(
                draft,
                [feedback.text],
                refiner=self.refiner,
                memory_provider=self.memory_provider,
            )
            try:
                revision_count = _write_revised_draft_payload(
                    payload_path, payload, event, feedback, refined
                )
            except OSError as exc:
                self._post_thread_ack(
                    event.channel,
                    event.root_ts,
                    "*Planning feedback captured*\n\nI could not save the revised draft. "
                    "Please check local disk space and permissions, then reply again.",
                )
                return ListenerResult(
                    True,
                    "captured_draft_feedback",
                    f"saved draft write failed: {exc}",
                    thread_kind=record.kind,
                )
            updated_record = self._register_draft_revision(
                record,
                refined,
                payload_path,
                revision_count=revision_count,
            )
        memory_candidate_ids = self._propose_planning_memory_candidates(
            event,
            refined,
            payload_path,
            source="slack-revision",
        )
        if memory_candidate_ids:
            with _draft_revision_lock(payload_path):
                _append_memory_candidate_ids(payload_path, memory_candidate_ids)
        self._post_thread_ack(event.channel, event.root_ts, render_draft_revision_ack(refined))
        return ListenerResult(
            True,
            "draft_revised",
            draft_path=str(payload_path),
            thread_kind=updated_record.kind,
            readiness_ok=refined.readiness.ok,
            readiness_score=refined.readiness.score,
        )

    def _attempt_issue_conversion(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        """Bridge an approved draft to labeled GitHub issue work.

        SAFETY: this never runs code. It only asks the bridge to create
        labeled GitHub issues, which the autonomous fleet later claims through
        all existing gates. The approving user is trust-gated in
        ``handle_payload``; ``trusted=True`` reflects that, and the bridge
        re-verifies it plus enablement, approval, and the repo allowlist.
        """
        payload_path = Path(record.draft_path).expanduser() if record.draft_path else None
        if payload_path is None:
            return self._ack_unavailable_draft(event, record, "saved draft path is unavailable")

        with _draft_revision_lock(payload_path):
            payload = _read_draft_payload(payload_path)
            if payload is None:
                print(
                    f"[SLACK-LISTENER-WARN] saved draft unavailable for "
                    f"{event.channel}/{event.root_ts}: {payload_path}",
                    file=sys.stderr,
                )
                return self._ack_unavailable_draft(event, record, "saved draft is unavailable")

            already_converted = _draft_already_converted(payload) or record.status == "converted"
            outcome = self.bridge.convert(
                payload,
                trusted=event.user in self._trusted_user_ids(),
                thread_link=_thread_link(record),
                already_converted=already_converted,
            )
            if outcome.created:
                self._record_conversion(payload_path, payload, record, outcome)

        summary = _issue_summary_for_payload(payload) if outcome.created else ""
        self._post_thread_ack(
            event.channel,
            event.root_ts,
            render_bridge_outcome_ack(outcome, summary=summary),
        )
        if outcome.created:
            return ListenerResult(
                True,
                "issue_created",
                detail=outcome.detail,
                draft_path=str(payload_path),
                thread_kind=record.kind,
            )
        if outcome.status == "already_converted":
            return ListenerResult(
                True,
                "issue_already_created",
                detail=outcome.detail,
                draft_path=str(payload_path),
                thread_kind=record.kind,
            )
        action = "approval_no_issue" if outcome.refused else "approval_ignored"
        return ListenerResult(
            True,
            action,
            detail=outcome.detail,
            draft_path=str(payload_path),
            thread_kind=record.kind,
        )

    def _record_conversion(
        self,
        payload_path: Path,
        payload: dict[str, Any],
        record: SlackThreadRecord,
        outcome: Any,
    ) -> None:
        """Persist the converted state so the draft can never double-create."""
        try:
            _write_converted_draft_payload(payload_path, payload, outcome)
        except OSError as exc:
            print(
                f"[SLACK-LISTENER-WARN] could not mark draft converted {payload_path}: {exc}",
                file=sys.stderr,
            )
        metadata = dict(record.metadata or {})
        metadata.update(
            {
                "bridge_issue_url": outcome.issue_url,
                "bridge_issue_urls": list(getattr(outcome, "issue_urls", ())),
                "bridge_issues_by_repo": dict(getattr(outcome, "issues_by_repo", {})),
                "bridge_repo": outcome.repo,
                "bridge_repos": list(getattr(outcome, "repos", ())),
                "bridge_bundle_slug": getattr(outcome, "bundle_slug", ""),
                "bridge_bundle_label": getattr(outcome, "bundle_label", ""),
                "converted_at": _utc_now(),
            }
        )
        self.registry.register(
            SlackThreadRecord(
                kind=record.kind,
                channel=record.channel,
                thread_ts=record.thread_ts,
                codename=record.codename,
                firing_id=record.firing_id,
                title=record.title,
                status="converted",
                parent_repo=record.parent_repo,
                parent_issue=record.parent_issue,
                plan_path=record.plan_path,
                draft_path=record.draft_path,
                created_at=record.created_at,
                metadata=metadata,
            )
        )
        self._register_status_thread(record, outcome)

    def _register_status_thread(self, record: SlackThreadRecord, outcome: Any) -> None:
        """Link the originating thread to the filed issue for progress posts.

        Best-effort: the status tracker is read-only on GitHub and only posts
        back into the thread the bridge already owns, so a failure here never
        blocks issue creation. The thread root is ``record.thread_ts`` (the
        registered draft thread), which is where the bridge posted its
        acknowledgement.
        """
        issue_number = _issue_number_from_url(getattr(outcome, "issue_url", ""))
        if issue_number is None:
            return
        try:
            self.status_tracker.register_issue_thread(
                channel=record.channel,
                thread_ts=record.thread_ts,
                repo=str(getattr(outcome, "repo", "") or ""),
                issue_number=issue_number,
                issue_url=str(getattr(outcome, "issue_url", "") or ""),
                title=record.title,
            )
        except Exception as exc:
            print(
                f"[SLACK-LISTENER-WARN] could not register status thread for "
                f"{record.channel}/{record.thread_ts}: {exc}",
                file=sys.stderr,
            )

    def _write_plan_revision_context(
        self,
        record: SlackThreadRecord,
        feedback: ThreadFeedback,
        all_feedback: Iterable[str],
        revised_repos: Iterable[str],
        *,
        requires_resolution: bool,
    ) -> tuple[Path | None, int]:
        revision_dir = self.state_root / "plan-revisions"
        path = (
            revision_dir / f"slack-{_safe_event_id(record.channel + '-' + record.thread_ts)}.json"
        )
        now = _utc_now()
        try:
            revision_dir.mkdir(parents=True, exist_ok=True)
            with _draft_revision_lock(path):
                existing = _read_json_object(path) or {}
                existing_revisions = existing.get("revisions")
                revisions = existing_revisions if isinstance(existing_revisions, list) else []
                stored_count = existing.get("revision_count")
                revision_count = (
                    stored_count + 1
                    if isinstance(stored_count, int) and not isinstance(stored_count, bool)
                    else len(revisions) + 1
                )
                revision = {
                    "author": feedback.author,
                    "text": feedback.text,
                    "ts": feedback.ts,
                    "captured_at": now,
                    "requires_resolution": requires_resolution,
                    "revised_repos": list(revised_repos),
                }
                payload = {
                    "source": "slack-plan-thread",
                    "kind": "plan_revision",
                    "created_at": existing.get("created_at") or now,
                    "updated_at": now,
                    "revision_count": revision_count,
                    "record": {
                        "channel": record.channel,
                        "thread_ts": record.thread_ts,
                        "codename": record.codename,
                        "firing_id": record.firing_id,
                        "title": record.title,
                        "parent_repo": record.parent_repo,
                        "parent_issue": record.parent_issue,
                        "plan_path": record.plan_path,
                    },
                    "feedback": list(all_feedback),
                    "latest": revision,
                    "revisions": [*revisions, revision][-_MAX_STORED_REVISIONS:],
                }
                tmp = path.with_name(f"{path.name}.tmp")
                tmp.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                tmp.replace(path)
            return path, revision_count
        except OSError as exc:
            print(
                f"[SLACK-LISTENER-WARN] could not write plan revision for "
                f"{record.channel}/{record.thread_ts}: {exc}",
                file=sys.stderr,
            )
            existing_count = _metadata_int(record.metadata.get("plan_revision_count")) or 0
            return None, existing_count + 1

    def sync_thread_status(self, *, fetcher: Any | None = None) -> list[dict[str, Any]]:
        """Sweep tracked issue threads and post fleet progress deltas.

        Read-only on GitHub. Used by the ``alfred slack-thread-sync`` CLI and
        the listener's optional idle-loop hook. ``fetcher`` defaults to the
        read-only ``gh``-backed fetcher.
        """
        from slack_thread_status import default_issue_state_fetcher

        return self.status_tracker.sweep(fetcher=fetcher or default_issue_state_fetcher)

    def _write_followup_context(
        self,
        record: SlackThreadRecord,
        feedback: ThreadFeedback,
    ) -> Path | None:
        pr_urls = _string_list(record.metadata.get("created") or record.metadata.get("pr_urls"))
        block = render_post_pr_followup_block(
            [feedback.text],
            pr_urls=pr_urls,
            issue_url=_issue_url_from_record(record),
        )
        if not block:
            return None
        followup_dir = self.state_root / "followups"
        try:
            followup_dir.mkdir(parents=True, exist_ok=True)
            path = (
                followup_dir / f"slack-{_safe_event_id(record.channel + '-' + record.thread_ts)}.md"
            )
            header = [
                f"# Follow-up for {record.title or record.codename or record.kind}",
                "",
                f"- Captured: {_utc_now()}",
                f"- Thread: `{record.channel}` / `{record.thread_ts}`",
            ]
            if record.firing_id:
                header.append(f"- Firing: `{record.firing_id}`")
            if record.parent_repo and record.parent_issue:
                header.append(
                    f"- Parent: [{record.parent_repo}#{record.parent_issue}]"
                    f"(https://github.com/{record.parent_repo}/issues/{record.parent_issue})"
                )
            body = "\n".join(header).rstrip() + "\n\n" + block
            if path.exists():
                try:
                    existing = path.read_text(encoding="utf-8").rstrip()
                except OSError:
                    existing = ""
                body = f"{existing}\n\n---\n\n{body}" if existing else body
            path.write_text(body, encoding="utf-8")
            return path
        except OSError as exc:
            print(
                f"[SLACK-LISTENER-WARN] could not write follow-up context for "
                f"{record.channel}/{record.thread_ts}: {exc}",
                file=sys.stderr,
            )
            return None

    def _ack_unavailable_draft(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
        detail: str,
    ) -> ListenerResult:
        self._post_thread_ack(
            event.channel,
            event.root_ts,
            "*Planning feedback captured*\n\nI could not reopen the saved draft. "
            "Start a fresh planning message if you want Alfred to revise this scope.",
        )
        return ListenerResult(
            True,
            "captured_draft_feedback",
            detail,
            thread_kind=record.kind,
        )

    def _register_draft_revision(
        self,
        record: SlackThreadRecord,
        result: Any,
        payload_path: Path,
        *,
        revision_count: int,
    ) -> SlackThreadRecord:
        metadata = dict(record.metadata or {})
        metadata.update(
            {
                "source": metadata.get("source") or "slack",
                "readiness_score": result.readiness.score,
                "readiness_ok": result.readiness.ok,
                "updated_at": _utc_now(),
                "revision_count": revision_count,
            }
        )
        return self.registry.register(
            SlackThreadRecord(
                kind=record.kind,
                channel=record.channel,
                thread_ts=record.thread_ts,
                codename=record.codename,
                firing_id=record.firing_id,
                title=result.draft.title,
                status="ready" if result.readiness.ok else "needs_scope",
                parent_repo=record.parent_repo,
                parent_issue=record.parent_issue,
                plan_path=record.plan_path,
                draft_path=str(payload_path),
                created_at=record.created_at,
                metadata=metadata,
            )
        )

    def _propose_planning_memory_candidates(
        self,
        event: SlackInputEvent,
        result: Any,
        draft_path: Path,
        *,
        source: str,
    ) -> tuple[str, ...]:
        """Queue reviewable memory candidates from scoped Slack planning work."""
        if _env_disabled("ALFRED_SLACK_MEMORY_CANDIDATES"):
            return ()
        readiness = getattr(result, "readiness", None)
        if readiness is None or not getattr(readiness, "ok", False):
            return ()
        writer = _memory_candidate_writer(self.memory_provider)
        if writer is None or not hasattr(writer, "propose_memory"):
            return ()
        draft = getattr(result, "draft", None)
        if not isinstance(draft, IssueDraft):
            return ()
        body = _slack_memory_candidate_body(draft)
        if not body:
            return ()
        evidence = {
            "kind": "slack_planning",
            "source": source,
            "draft_path": str(draft_path),
            "event_id": event.event_id,
            "channel": event.channel,
            "thread_ts": event.root_ts,
            "readiness_score": getattr(readiness, "score", None),
            "amendments": list(getattr(result, "amendments", ()) or ()),
        }
        ids: list[str] = []
        proposed_keys: list[str] = []
        propose_memory = writer.propose_memory
        use_modern_signature = _propose_memory_supports_modern_signature(propose_memory)
        with _draft_revision_lock(draft_path):
            existing_keys = _draft_memory_candidate_keys(draft_path)
            for repo in draft.repos or ["planning"]:
                candidate_key = _slack_memory_candidate_key(repo)
                if candidate_key in existing_keys:
                    continue
                repo_evidence = {
                    **evidence,
                    "repo": repo,
                    "candidate_key": candidate_key,
                }
                if use_modern_signature:
                    kwargs = {
                        "codename": "planning",
                        "repo": repo,
                        "body": body,
                        "tags": ["slack", "planning"],
                        "severity": "info",
                        "source": source,
                        "evidence": json.dumps(repo_evidence, sort_keys=True),
                        "confidence": 0.68,
                    }
                else:
                    kwargs = {
                        "agent": "planning",
                        "repo": repo,
                        "topic": "slack-planning",
                        "body": body,
                        "source": source,
                        "evidence": [repo_evidence],
                    }
                try:
                    candidate = propose_memory(**kwargs)
                except Exception as exc:
                    print(
                        f"[SLACK-LISTENER-WARN] could not queue {source} memory "
                        f"candidate for {repo}: {exc}",
                        file=sys.stderr,
                    )
                    continue
                candidate_id = getattr(candidate, "id", candidate)
                ids.append(str(candidate_id))
                proposed_keys.append(candidate_key)
            if proposed_keys:
                _append_memory_candidate_keys(draft_path, proposed_keys)
        return tuple(ids)

    def _save_draft(
        self,
        event: SlackInputEvent,
        draft: IssueDraft,
        issue_body: str,
        spec_body: str,
        *,
        readiness: Any | None = None,
        memory: Iterable[Any] = (),
    ) -> Path:
        root = self.state_root / "planning-drafts"
        root.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        path = root / f"slack-{stamp}-{_safe_event_id(event.event_id)}.json"
        payload = {
            "source": "slack",
            "created_at": _utc_now(),
            "event": asdict(event),
            "draft": asdict(draft),
            "issue_body": issue_body,
            "spec_body": spec_body,
            "readiness": _readiness_payload(readiness),
            "memory": [asdict(item) for item in memory],
            "revision_count": 0,
            "revisions": [],
        }
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def _post_thread_ack(self, channel: str, thread_ts: str, text: str) -> None:
        if self.poster is None or not text.strip():
            return
        try:
            self.poster.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        except Exception:
            return

    def _trusted_user_ids(self) -> set[str]:
        if self._static_trusted_user_ids is not None:
            return set(self._static_trusted_user_ids)
        return set(
            trusted_feedback_user_ids_from_env(
                self._operator_user_id,
                state_root=self.state_root,
            )
        )


def _memory_candidate_writer(provider: Any | None) -> Any | None:
    if provider is None:
        return None
    if hasattr(provider, "propose_memory"):
        return provider
    brain = getattr(provider, "brain", None)
    if brain is not None and hasattr(brain, "propose_memory"):
        return brain
    providers = getattr(provider, "providers", None)
    if isinstance(providers, (list, tuple)):
        for child in providers:
            writer = _memory_candidate_writer(child)
            if writer is not None:
                return writer
    return None


def _env_disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def _propose_memory_supports_modern_signature(method: Any) -> bool:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return True
    parameters = signature.parameters.values()
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters):
        return True
    return {"codename", "tags", "severity", "confidence"}.issubset(signature.parameters)


def _append_memory_candidate_ids(path: Path, candidate_ids: Iterable[str]) -> None:
    _append_draft_list_values(path, "memory_candidate_ids", candidate_ids)


def _append_memory_candidate_keys(path: Path, candidate_keys: Iterable[str]) -> None:
    _append_draft_list_values(path, "memory_candidate_keys", candidate_keys)


def _draft_memory_candidate_keys(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    existing = payload.get("memory_candidate_keys")
    if isinstance(existing, list):
        return {str(item) for item in existing if str(item)}
    return set()


def _append_draft_list_values(path: Path, field: str, values: Iterable[str]) -> None:
    clean_values = [str(value) for value in values if str(value)]
    if not clean_values:
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    existing = payload.get(field)
    merged = [str(item) for item in existing] if isinstance(existing, list) else []
    for value in clean_values:
        if value not in merged:
            merged.append(value)
    payload[field] = merged
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _slack_memory_candidate_key(repo: str) -> str:
    return f"slack-planning:{repo.strip() or 'planning'}"


def _slack_memory_candidate_body(draft: IssueDraft) -> str:
    parts = [
        f"Slack planning lesson for {draft.title.strip() or 'untitled work'}.",
        f"Problem: {_short_plain(draft.problem, 220)}" if draft.problem else "",
        (
            f"Desired behavior: {_short_plain(draft.desired_behavior, 220)}"
            if draft.desired_behavior
            else ""
        ),
    ]
    if draft.acceptance_criteria:
        parts.append(
            "Acceptance: "
            + "; ".join(_short_plain(item, 140) for item in draft.acceptance_criteria[:3])
        )
    if draft.test_plan:
        parts.append(f"Verification: {_short_plain(draft.test_plan, 180)}")
    body = " ".join(part for part in parts if part).strip()
    return body[:900]


def _short_plain(value: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "..."


def parse_slack_payload(payload: dict[str, Any]) -> SlackInputEvent | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    if event_type == "reaction_added":
        return _parse_reaction_event(payload, event)
    if event_type not in {"app_mention", "message"}:
        return None
    subtype = str(event.get("subtype") or "")
    if subtype and subtype not in {"file_share"}:
        return None
    if event.get("bot_id"):
        return None
    channel = str(event.get("channel") or "")
    ts = str(event.get("ts") or "")
    user = str(event.get("user") or "")
    text = _clean_slack_text(str(event.get("text") or ""))
    if not channel or not ts or not user or not text:
        return None
    return SlackInputEvent(
        event_id=str(payload.get("event_id") or f"{channel}:{ts}:{user}"),
        event_type=event_type,
        channel=channel,
        user=user,
        text=text,
        ts=ts,
        thread_ts=str(event.get("thread_ts") or ""),
        channel_type=str(event.get("channel_type") or ""),
    )


def _parse_reaction_event(payload: dict[str, Any], event: dict[str, Any]) -> SlackInputEvent | None:
    """Parse a ``reaction_added`` event into a thread-targeted input event.

    The reacted message ts (``item.ts``) becomes both ``ts`` and ``thread_ts``
    so the event resolves to the registered draft thread root. Reaction events
    carry no message text; approval is decided purely on the reaction name.
    """
    raw_item = event.get("item")
    item = raw_item if isinstance(raw_item, dict) else {}
    channel = str(item.get("channel") or "")
    ts = str(item.get("ts") or "")
    user = str(event.get("user") or "")
    reaction = str(event.get("reaction") or "")
    if not channel or not ts or not user or not reaction:
        return None
    return SlackInputEvent(
        event_id=str(payload.get("event_id") or f"reaction:{channel}:{ts}:{user}:{reaction}"),
        event_type="reaction_added",
        channel=channel,
        user=user,
        text="",
        ts=ts,
        thread_ts=ts,
        reaction=reaction,
    )


def draft_from_slack_text(text: str) -> IssueDraft:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    fields = _structured_fields(lines)
    title = fields.get("title") or _title_from_text(text)
    problem = fields.get("problem") or _strip_mentions(text)
    desired = fields.get("desired") or fields.get("desired behavior") or ""
    current = fields.get("current") or fields.get("current behavior") or ""
    repos = _repos_from_text(text)
    acceptance = _list_field(fields, "acceptance")
    test_plan = fields.get("test") or fields.get("test plan") or ""
    out_of_scope = (
        fields.get("out of scope") or fields.get("non goals") or fields.get("non-goals") or ""
    )
    return IssueDraft(
        title=title,
        problem=problem,
        current_behavior=current,
        desired_behavior=desired,
        repos=repos,
        acceptance_criteria=acceptance,
        test_plan=test_plan,
        out_of_scope=out_of_scope,
        open_questions=(fields.get("open question") or fields.get("open questions") or ""),
    )


# Reaction vocabulary for the conversational-action confirmation gate. These
# mirror ``slack_approval.SlackApproval`` defaults so the workspace owner uses
# the same gestures everywhere: a check / thumbs-up confirms, an x / thumbs-down
# cancels.
_CONFIRM_REACTIONS: frozenset[str] = frozenset({"white_check_mark", "thumbsup", "+1"})
_CANCEL_REACTIONS: frozenset[str] = frozenset({"x", "thumbsdown", "-1"})


def _reaction_name(reaction: str) -> str:
    """Normalize a Slack reaction name (drop skin-tone variants)."""
    return (reaction or "").split("::", 1)[0].strip().lower()


def _is_confirm_reaction(reaction: str) -> bool:
    return _reaction_name(reaction) in _CONFIRM_REACTIONS


def _is_cancel_reaction(reaction: str) -> bool:
    return _reaction_name(reaction) in _CANCEL_REACTIONS


def render_intent_confirmation(intent: Intent) -> tuple[str, list[dict]]:
    """Render the workspace-owner-confirmable card for a mutating intent.

    Returns ``(fallback_text, blocks)``. The fallback text is what notifications
    and non-Block-Kit clients show; the Block Kit blocks render the structured
    summary. The card states the interpreted action, target, and exact
    reactions to confirm or cancel. It never claims the action happened;
    nothing runs until the workspace owner reacts.
    """
    verb = _intent_action_verb(intent.action)
    effect = _intent_action_effect(intent.action)
    target = _intent_action_target(intent)
    target_display = _intent_action_target_display(intent)
    raw_text = (intent.params or {}).get("raw_text", "")
    fallback = f"Confirm {verb} {target}? React to confirm; nothing changed yet."
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Confirm action*"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Action*\n{verb}"},
                {"type": "mrkdwn", "text": f"*Target*\n{target_display}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Effect*\n{effect}."},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "Status: waiting for your reaction. "
                        "Use :white_check_mark: to confirm or :x: to cancel."
                    ),
                }
            ],
        },
    ]
    if raw_text:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"From your message: {raw_text[:280]}"}],
            }
        )
    return fallback, blocks


def _intent_action_verb(action: str) -> str:
    return {
        ACTION_ASSIGN: "assign",
        ACTION_QUEUE: "queue",
        ACTION_HOLD: "hold",
        ACTION_RUN_AGENT: "trigger",
        ACTION_PAUSE_AGENT: "pause",
        ACTION_RESUME_AGENT: "resume",
        ACTION_SCHEDULE_AGENT: "reschedule",
    }.get(action, "run")


def _intent_action_effect(action: str) -> str:
    return {
        ACTION_ASSIGN: "choose Batman or Lucius and label the issue for that lane",
        ACTION_QUEUE: "make it eligible for autonomous pickup",
        ACTION_HOLD: "take it out of Alfred's reach",
        ACTION_RUN_AGENT: "trigger one manual agent run now",
        ACTION_PAUSE_AGENT: "stop scheduled firings until it is resumed",
        ACTION_RESUME_AGENT: "resume scheduled firings",
        ACTION_SCHEDULE_AGENT: "edit the configured cadence in agents.conf",
    }.get(action, "run the requested action")


def _intent_action_target(intent: Intent) -> str:
    if intent.action in {ACTION_ASSIGN, ACTION_QUEUE, ACTION_HOLD}:
        return f"{intent.repo}#{intent.issue}"
    if intent.agent:
        if intent.action == ACTION_SCHEDULE_AGENT and intent.schedule:
            return f"{intent.agent} -> {intent.schedule}"
        return intent.agent
    return "unknown target"


def _intent_action_target_display(intent: Intent) -> str:
    if intent.action in {ACTION_ASSIGN, ACTION_QUEUE, ACTION_HOLD}:
        if intent.repo and intent.issue:
            return github_issue_link(intent.repo, intent.issue)
        return "`unknown issue`"
    target = _intent_action_target(intent)
    return f"`{target}`" if target != "unknown target" else target


def _control_command_for_agent_intent(action: str) -> str:
    return {
        ACTION_RUN_AGENT: "run",
        ACTION_PAUSE_AGENT: "pause",
        ACTION_RESUME_AGENT: "resume",
        ACTION_SCHEDULE_AGENT: "schedule set",
    }.get(action, "")


def _assignment_ack_text(repo: str, issue: int, detail: str) -> str:
    return "\n".join(
        [
            ":label: *Issue routed*",
            f"*Issue:* {github_issue_link(repo, issue)}",
            f"*Result:* {detail}",
        ]
    )


def _status_query_plan(text: str) -> tuple[str, str]:
    """Map a natural-language read-only question to a control verb + lead-in.

    Returns ``(verb, lead_in)`` where ``verb`` is one of the read-only control
    commands (``status`` / ``runs`` / ``plans``) and ``lead_in`` is a short
    conversational sentence rendered above the handler's structured output.
    Deterministic and side-effect free; the default is ``status``.
    """
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()

    ship_cues = (
        "ship",
        "shipped",
        "merge",
        "merged",
        "what did you do",
        "what have you done",
        "recent runs",
        "what ran",
        "last run",
    )
    blocked_cues = (
        "blocked",
        "stuck",
        "waiting",
        "needs scope",
        "need scope",
        "needs review",
        "in the inbox",
        "planning inbox",
        "what's queued",
        "whats queued",
        "what is queued",
    )
    if any(cue in normalized for cue in ship_cues):
        return "runs", "Here's what the fleet has been working on recently:"
    if any(cue in normalized for cue in blocked_cues):
        return "plans", "Here's what's in the planning inbox right now:"
    return "status", "Here's where the fleet stands:"


def _is_read_only_control_text(text: str) -> bool:
    """True when a leading-verb Slack command is safe to run without a card."""
    command = parse_control_command(text)
    if command is None:
        return False
    if command.verb in {"status", "runs", "plans", "plan", "trusted", "help", "dry-run"}:
        return True
    if command.verb == "schedule":
        return command.arg == "list" or command.arg.startswith("show ")
    if command.verb == "memory":
        args = command.arg.split()
        subcommand = args[0].lower() if args else "review"
        if subcommand in {"review", "queue", "candidates", "candidate", "promotions", "promotable"}:
            return True
        return subcommand == "redis" and not (len(args) > 1 and args[1].lower() == "sync")
    return False


def render_draft_ack(result: Any) -> str:
    readiness = result.readiness
    status = "ready for review" if readiness.ok else "needs a little more scope"
    lines = [
        "*Planning draft saved*",
        "",
        f"*Status:* {status} ({readiness.score}/100)",
        f"*Title:* {result.draft.title or 'Untitled Alfred work'}",
    ]
    if result.draft.repos:
        lines.append("*Repos:* " + ", ".join(f"`{repo}`" for repo in result.draft.repos))
    if result.questions:
        lines.extend(["", "*Questions to answer before Alfred builds:*"])
        lines.extend(f"- {question}" for question in result.questions[:5])
    lines.extend(
        [
            "",
            "*How to steer this:* reply with lines like `repo: owner/repo`, "
            "`desired: ...`, `acceptance: ...`, `test: ...`, `open question: ...`, "
            "or `open questions: none`. Ask normal questions in plain language.",
            "*Safety:* chat edits the draft only. Implementation still needs the normal approval gate.",
        ]
    )
    return "\n".join(lines)


def render_draft_revision_ack(result: Any) -> str:
    readiness = result.readiness
    status = "ready for review" if readiness.ok else "needs more scope"
    lines = [
        "*Planning draft revised*",
        "",
        f"*Status:* {status} ({readiness.score}/100)",
        f"*Title:* {result.draft.title or 'Untitled Alfred work'}",
    ]
    if result.amendments:
        lines.extend(["", "*Applied now:*"])
        lines.extend(f"- {item}" for item in result.amendments[:6])
        if len(result.amendments) > 6:
            lines.append(f"- ...and {len(result.amendments) - 6} more update(s).")
    if result.draft.repos:
        lines.extend(["", "*Current repo scope:*"])
        lines.extend(f"- `{repo}`" for repo in result.draft.repos[:8])
    if result.questions:
        lines.extend(["", "*Questions to answer before Alfred builds:*"])
        lines.extend(f"- {question}" for question in result.questions[:5])
    lines.extend(
        [
            "",
            "*Next:* keep replying in this thread to shape the draft. "
            "Creating issues or running agents still needs an explicit operator action.",
        ]
    )
    return "\n".join(lines)


def _issue_summary_for_payload(payload: dict[str, Any]) -> str:
    """Best-effort plain-English summary of a converted draft payload.

    Reads the draft title + rendered issue body and asks the engine (when
    ``ALFRED_ISSUE_SUMMARY_ENABLED`` is set) for a short "what is this" line,
    falling back to a trimmed body/title. Never raises: a summary is a
    convenience on the ack, not load-bearing.
    """
    try:
        draft = payload.get("draft")
        title = ""
        if isinstance(draft, dict):
            title = str(draft.get("title") or "").strip()
        body = str(payload.get("issue_body") or "").strip() or build_issue_body(payload)
        return summarize_issue(title, body, engine_invoke=default_engine_invoke())
    except Exception as exc:  # summary is never load-bearing
        print(
            f"[SLACK-LISTENER-WARN] issue summary failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return ""


def render_bridge_outcome_ack(outcome: Any, *, summary: str = "") -> str:
    """Render a Slack acknowledgement for an issue-bridge conversion attempt.

    ``summary`` is an optional plain-English "what this issue is" line. When
    supplied (issue created), it renders above the link so the approver sees
    what they just filed, not just a bare URL.
    """
    if outcome.created:
        issue_urls = list(getattr(outcome, "issue_urls", ()) or ())
        issues_by_repo = dict(getattr(outcome, "issues_by_repo", {}) or {})
        if getattr(outcome, "is_bundle", False):
            lines = ["*Bundle created*", ""]
        else:
            lines = ["*Issue created*", ""]
        clean_summary = (summary or "").strip()
        if clean_summary:
            lines.extend([clean_summary, ""])
        if getattr(outcome, "is_bundle", False):
            if getattr(outcome, "bundle_label", ""):
                lines.append(f"*Bundle label:* `{outcome.bundle_label}`")
            lines.append("*Issues:*")
            for repo, url in issues_by_repo.items():
                lines.append(f"- `{repo}`: {_slack_github_url(url)}")
            if not issues_by_repo:
                for url in issue_urls:
                    lines.append(f"- {_slack_github_url(url)}")
        else:
            lines.extend(
                [
                    f"*Repo:* `{outcome.repo}`",
                    f"*Issue:* {_slack_github_url(outcome.issue_url)}",
                ]
            )
        lines.extend(
            [
                "",
                "It is now in the autonomous queue. The fleet still claims it "
                "through every existing gate (claim-lock, spend caps, review, "
                "Batman approval) before any change ships.",
            ]
        )
        return "\n".join(lines)
    if outcome.status == "already_converted":
        issue_urls = list(getattr(outcome, "issue_urls", ()) or ())
        if len(issue_urls) > 1:
            suffix = "\n\n*Existing issues:*\n" + "\n".join(
                f"- {_slack_github_url(url)}" for url in issue_urls
            )
        else:
            suffix = (
                f"\n\n*Existing issue:* {_slack_github_url(outcome.issue_url)}"
                if outcome.issue_url
                else ""
            )
        return (
            "*Already filed*\n\nThis draft was already converted to an issue, "
            "so I did not create a duplicate." + suffix
        )
    if outcome.status == "disabled":
        return (
            "*Approval noted, but the issue bridge is off*\n\n"
            "Set `ALFRED_BRIDGE_ENABLED=1` and `ALFRED_BRIDGE_REPOS` to let "
            "explicit approvals file issues. Nothing was created."
        )
    if outcome.status in {"refused_not_ready", "refused_readiness_missing"}:
        return (
            "*Draft still needs scope*\n\n"
            f"{outcome.detail or 'The draft needs a complete readiness check before filing.'}\n\n"
            "Reply in this thread with the missing acceptance criteria, repo scope, "
            "test plan, or `open questions: none` once the risk is accepted. Nothing "
            "was created and no code was run."
        )
    return (
        "*Could not create the issue*\n\n"
        f"{outcome.detail or 'The draft was not eligible to file.'}\n\n"
        "Nothing was created and no code was run. Adjust the draft and approve again."
    )


def _slack_github_url(url: str) -> str:
    return github_url_link(url) or (url or "")


def run_socket_mode(listener: SlackPlanningListener | None = None) -> None:
    app_token = (os.environ.get(ENV_APP_TOKEN) or os.environ.get(ENV_ALT_APP_TOKEN) or "").strip()
    if not app_token:
        raise RuntimeError("Set SLACK_APP_TOKEN or ALFRED_SLACK_APP_TOKEN to run the listener.")
    try:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as exc:
        raise RuntimeError("Install slack-sdk to run the Slack planning listener.") from exc

    bot_token = resolve_bot_token()
    poster: Any = default_slack_client(bot_token)
    active = listener or SlackPlanningListener(
        poster=poster,
        refiner=engine_refiner_from_env(),
        memory_provider=_default_memory_provider(),
        bridge=SlackIssueBridge(),
    )
    client = SocketModeClient(app_token=app_token, web_client=poster)

    def _handler(socket_client: Any, req: Any) -> None:
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if getattr(req, "type", "") == "events_api":
            try:
                active.handle_payload(getattr(req, "payload", {}) or {})
            except Exception as exc:
                print(
                    f"[SLACK-LISTENER-WARN] handle_payload failed: {exc}",
                    file=sys.stderr,
                )

    client.socket_mode_request_listeners.append(_handler)
    client.connect()
    print(
        "[SLACK-LISTENER] connected; listening for events "
        f"(trusted_users={len(active._trusted_user_ids())}, "
        f"bridge={'on' if active.bridge.config.enabled else 'off'})",
        flush=True,
    )
    interval = _thread_sync_interval_s()
    while True:
        time.sleep(interval if interval > 0 else 60)
        if interval > 0:
            try:
                active.sync_thread_status()
            except Exception as exc:
                print(
                    f"[SLACK-LISTENER-WARN] thread-status sync failed: {exc}",
                    file=sys.stderr,
                )


def _structured_fields(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        line = _strip_mentions(line)
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized = " ".join(key.lower().replace("_", " ").replace("-", " ").split())
        cleaned = value.strip()
        if normalized and cleaned:
            if normalized in {"acceptance", "acceptance criteria"} and normalized in out:
                out[normalized] = f"{out[normalized]}\n{cleaned}"
            else:
                out[normalized] = cleaned
    return out


def _list_field(fields: dict[str, str], key: str) -> list[str]:
    raw = fields.get(key) or fields.get(f"{key} criteria") or ""
    return [item.strip().lstrip("-*").strip() for item in re.split(r",|;|\n", raw) if item.strip()]


def _repos_from_text(text: str) -> list[str]:
    repos = re.findall(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b", text)
    seen: set[str] = set()
    out: list[str] = []
    for repo in repos:
        if repo not in seen:
            seen.add(repo)
            out.append(repo)
    return out


def _title_from_text(text: str) -> str:
    cleaned = _strip_mentions(text)
    first = re.split(r"[\n.!?]", cleaned, maxsplit=1)[0].strip()
    return first[:90] or "Untitled Alfred work"


def _strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text).strip()


def _clean_slack_text(text: str) -> str:
    text = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _is_plan_thread_question(text: str) -> bool:
    lines = _clean_slack_text(text).splitlines()
    if not lines:
        return False
    for raw in lines:
        line = raw.strip()
        lowered = line.lower()
        if lowered.startswith(("open question:", "open questions:")):
            return False
        if _is_plan_thread_field_command(line):
            return False
        if lowered.startswith(("question:", "questions:")):
            continue
        if line.endswith("?"):
            continue
        if re.match(
            r"^(can you|could you|would you|what|why|how|who|which|when|where)\b",
            lowered,
        ):
            continue
        if lowered.startswith(("explain ", "tell me ", "help me understand ")):
            continue
        return False
    return True


def _is_plan_thread_field_command(line: str) -> bool:
    cleaned = _clean_slack_text(line)
    lowered = cleaned.lower()
    if lowered.startswith(("add repo ", "remove repo ")):
        return True
    if ":" not in cleaned:
        return False
    raw_field = cleaned.split(":", 1)[0]
    field = " ".join(raw_field.replace("_", " ").replace("-", " ").lower().split())
    return field in {
        "acceptance",
        "acceptance criteria",
        "change",
        "context",
        "current",
        "current behavior",
        "desired",
        "desired behavior",
        "fix",
        "non goal",
        "non goals",
        "out of scope",
        "problem",
        "repo",
        "repos",
        "repositories",
        "clear open questions",
        "clear question",
        "clear questions",
        "remove repo",
        "resolve question",
        "resolve questions",
        "resolved question",
        "resolved questions",
        "rollout",
        "test",
        "test plan",
        "tests",
        "title",
        "user",
    }


def _strip_plan_question_prefix(text: str) -> str:
    clean = _clean_slack_text(text)
    lines: list[str] = []
    for line in clean.splitlines():
        if ":" not in line:
            lines.append(line.strip())
            continue
        raw, value = line.split(":", 1)
        field = " ".join(raw.replace("_", " ").replace("-", " ").lower().split())
        if field in {"question", "questions"}:
            lines.append(value.strip())
        else:
            lines.append(line.strip())
    return "\n".join(item for item in lines if item).strip()


def _read_text_file(raw_path: str | Path | None) -> str:
    if not raw_path:
        return ""
    try:
        return Path(raw_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _default_plan_thread_answerer() -> PlanAnswerer | None:
    engine = (os.environ.get(ENV_PLAN_ANSWER_ENGINE) or "").strip()
    if not engine:
        return None
    timeout = _env_int(ENV_PLAN_ANSWER_TIMEOUT, default=180)

    def _answer(
        record: SlackThreadRecord,
        question: str,
        plan_markdown: str,
    ) -> str | None:
        try:
            from agent_runner import invoke_agent_engine
        except Exception:
            return None
        prompt = _plan_answer_prompt(record, question, plan_markdown)
        firing_id = datetime.now(UTC).strftime("slack-plan-answer-%Y%m%d-%H%M%S")
        result, _engine_used = invoke_agent_engine(
            prompt,
            engine=engine,
            agent="slack-plan-chat",
            firing_id=firing_id,
            workdir=Path.cwd(),
            claude_allowed_tools="Read",
            timeout=timeout,
            claude_max_turns=6,
            codex_timeout=timeout,
        )
        if not result.success or not result.result_text:
            return None
        return result.result_text.strip()

    return _answer


def _plan_answer_prompt(
    record: SlackThreadRecord,
    question: str,
    plan_markdown: str,
) -> str:
    return "\n".join(
        [
            "You are Alfred answering a trusted operator inside a Slack plan thread.",
            "Answer the operator's question conversationally and concretely.",
            "Do not revise the plan, approve execution, file issues, or invent missing facts.",
            "If the plan does not contain enough context, say what is missing.",
            "Keep the reply concise enough for Slack, but useful.",
            "",
            "Thread context:",
            f"- Title: {record.title or '(unknown)'}",
            f"- Parent: {record.parent_repo}#{record.parent_issue or ''}".rstrip("#"),
            f"- Codename: {record.codename or '(unknown)'}",
            "",
            "Operator question:",
            question or "(empty)",
            "",
            "Current plan markdown:",
            plan_markdown or "(plan markdown unavailable)",
        ]
    )


def _fallback_plan_question_answer(
    record: SlackThreadRecord,
    question: str,
    plan_markdown: str,
    *,
    revised_repos: Iterable[str],
    child_count: int | None,
) -> list[str]:
    work = _extract_slack_field(plan_markdown, "Work") or record.title or "this plan"
    readiness = _extract_slack_field(plan_markdown, "Readiness")
    parent = _extract_slack_field(plan_markdown, "Parent")
    scope_lines = _extract_plan_scope_lines(plan_markdown)
    done_when = _extract_plan_section(plan_markdown, "Done when", "Scope checks")
    repos = tuple(str(repo).strip() for repo in revised_repos if str(repo).strip())
    repo_label = "repo" if len(repos) == 1 else "repos"
    child_label = ""
    if child_count is not None:
        child_label = f", {child_count} child issue(s)"

    lines = ["*Answer*", ""]
    lines.append(f"This plan is about: {work}")
    if parent:
        lines.append(f"Parent: {parent}")
    if readiness:
        lines.append(f"Readiness: {readiness}")
    if repos:
        lines.append("")
        lines.append(f"Current scope: {len(repos)} {repo_label}{child_label}.")
        lines.extend(f"- `{repo}`" for repo in repos[:8])
        if len(repos) > 8:
            lines.append(f"- ...and {len(repos) - 8} more repo(s).")
    elif scope_lines:
        lines.append("")
        lines.append("Current scope:")
        lines.extend(scope_lines[:8])
    if done_when:
        lines.append("")
        lines.append("Done when:")
        lines.extend(f"- {line}" for line in done_when[:5])
    lines.extend(
        [
            "",
            "After approval Alfred will file the scoped child issues, run the relevant agents, and report PR links or failures back in this thread.",
        ]
    )
    if question:
        lines.extend(["", f"Asked: {question}"])
    return lines


def _extract_slack_field(markdown: str, label: str) -> str:
    pattern = re.compile(rf"^\*{re.escape(label)}:\*\s*(.+)$", re.MULTILINE)
    match = pattern.search(markdown or "")
    return match.group(1).strip() if match else ""


def _extract_plan_scope_lines(markdown: str) -> list[str]:
    lines = []
    in_scope = False
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if line.startswith("*Scope if approved now:*"):
            in_scope = True
            continue
        if in_scope and line.startswith("*"):
            break
        if in_scope and line.strip().startswith("-"):
            lines.append(line.strip())
    return lines


def _extract_plan_section(
    markdown: str,
    start_label: str,
    end_label: str,
) -> list[str]:
    out: list[str] = []
    in_section = False
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if line == f"*{start_label}:*":
            in_section = True
            continue
        if in_section and line == f"*{end_label}:*":
            break
        if in_section and line:
            out.append(line.lstrip("- ").strip())
    return out


def _env_int(name: str, *, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _read_draft_payload(path: Path | None) -> dict[str, Any] | None:
    return _read_json_object(path)


def _read_json_object(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_feedback_texts(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    out: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                out.append(text)
    except OSError:
        return ()
    return tuple(out)


def _draft_already_converted(payload: dict[str, Any]) -> bool:
    bridge = payload.get("bridge")
    return isinstance(bridge, dict) and bool(bridge.get("converted"))


def _write_converted_draft_payload(
    path: Path,
    payload: dict[str, Any],
    outcome: Any,
) -> None:
    """Stamp the saved draft as converted so it can never double-create.

    Written atomically via a temp file, matching the revision writer.
    """
    updated = dict(payload)
    now = _utc_now()
    updated["bridge"] = {
        "converted": True,
        "issue_url": outcome.issue_url,
        "issue_urls": list(getattr(outcome, "issue_urls", ()) or ()),
        "issues_by_repo": dict(getattr(outcome, "issues_by_repo", {}) or {}),
        "repo": outcome.repo,
        "repos": list(getattr(outcome, "repos", ()) or ()),
        "labels": list(getattr(outcome, "labels", ()) or ()),
        "bundle_slug": getattr(outcome, "bundle_slug", ""),
        "bundle_label": getattr(outcome, "bundle_label", ""),
        "converted_at": now,
        "source": "slack",
    }
    updated["updated_at"] = now
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _thread_link(record: SlackThreadRecord) -> str:
    """Best-effort Slack thread reference for the issue footer.

    Uses an explicit permalink from metadata when present, else a stable
    ``channel/thread_ts`` reference. We never call the Slack API to build it.
    """
    permalink = str(record.metadata.get("permalink") or "").strip()
    if permalink:
        return permalink
    if record.channel and record.thread_ts:
        return f"slack://thread?channel={record.channel}&ts={record.thread_ts}"
    return ""


def _issue_number_from_url(url: str) -> int | None:
    """Extract the trailing issue number from a GitHub issue URL."""
    match = re.search(r"/issues/(\d+)\b", str(url or ""))
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _ambient_channels_from_env() -> set[str]:
    """Parse the ambient channel allowlist from ``ALFRED_SLACK_AMBIENT_CHANNELS``.

    Accepts a space-, comma-, or newline-separated list of Slack channel ids.
    Empty / unset yields an empty set, which means ambient listening engages in
    no channel even when both ambient flags are armed (fail-safe scoping).
    """
    raw = (os.environ.get(ENV_AMBIENT_CHANNELS) or "").strip()
    if not raw:
        return set()
    return {token for token in re.split(r"[\s,]+", raw) if token}


def _thread_sync_interval_s() -> int:
    """Idle-loop sweep cadence in seconds (0 disables the in-listener hook).

    Defaults to 5 minutes. The standalone ``alfred slack-thread-sync`` entry
    point can run on the operator's own schedule regardless of this setting.
    """
    raw = (os.environ.get("ALFRED_SLACK_THREAD_SYNC_INTERVAL_S") or "").strip()
    if not raw:
        return 300
    try:
        return max(int(raw), 0)
    except ValueError:
        return 300


def _draft_from_payload(payload: dict[str, Any] | None) -> IssueDraft | None:
    if not payload:
        return None
    raw = payload.get("draft")
    if not isinstance(raw, dict):
        return None
    fields: dict[str, Any] = {}
    for key in IssueDraft.__dataclass_fields__:
        value = raw.get(key)
        if key in {"repos", "acceptance_criteria"}:
            fields[key] = [str(item).strip() for item in value or [] if str(item).strip()]
        elif isinstance(value, str):
            fields[key] = value
        elif value is not None:
            fields[key] = str(value)
    try:
        return IssueDraft(**fields)
    except TypeError:
        return None


def _write_revised_draft_payload(
    path: Path,
    payload: dict[str, Any],
    event: SlackInputEvent,
    feedback: ThreadFeedback,
    result: Any,
) -> int:
    now = _utc_now()
    revisions = payload.get("revisions")
    revision_list = revisions if isinstance(revisions, list) else []
    revision = {
        "author": feedback.author,
        "text": feedback.text,
        "ts": feedback.ts,
        "event_id": event.event_id,
        "captured_at": now,
        "readiness_ok": result.readiness.ok,
        "readiness_score": result.readiness.score,
        "amendments": list(result.amendments),
    }
    updated = dict(payload)
    stored_count = payload.get("revision_count")
    revision_count = (
        stored_count + 1
        if isinstance(stored_count, int) and not isinstance(stored_count, bool)
        else len(revision_list) + 1
    )
    updated.update(
        {
            "updated_at": now,
            "draft": asdict(result.draft),
            "issue_body": result.issue_body,
            "spec_body": result.spec_body,
            "readiness": _readiness_payload(result.readiness),
            "memory": [asdict(item) for item in result.memory],
            "revision_count": revision_count,
            "revisions": [*revision_list, revision][-_MAX_STORED_REVISIONS:],
        }
    )
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return revision_count


def _draft_revision_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _DRAFT_REVISION_LOCKS_GUARD:
        lock = _DRAFT_REVISION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DRAFT_REVISION_LOCKS[key] = lock
        return lock


def _readiness_payload(readiness: Any | None) -> dict[str, Any]:
    if readiness is None:
        return {}
    return {
        "ok": bool(readiness.ok),
        "score": int(readiness.score),
        "findings": [asdict(finding) for finding in readiness.findings],
        "questions": list(readiness.questions),
    }


def _default_memory_provider() -> Any | None:
    try:
        from memory.config import load_provider
    except Exception as exc:
        print(
            f"[SLACK-LISTENER-WARN] memory provider unavailable: {exc}",
            file=sys.stderr,
        )
        return None
    try:
        return load_provider()
    except Exception as exc:
        print(
            f"[SLACK-LISTENER-WARN] memory provider failed to load: {exc}",
            file=sys.stderr,
        )
        return None


def _string_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable):
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(text)
        return tuple(out)
    text = str(value).strip()
    return (text,) if text else ()


def _metadata_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _revised_child_count(
    metadata: dict[str, Any],
    base_repos: Iterable[str],
    revised_repos: Iterable[str],
) -> int | None:
    children_by_repo = metadata.get("children_by_repo")
    revised = tuple(str(repo).strip() for repo in revised_repos if str(repo).strip())
    if isinstance(children_by_repo, dict) and revised:
        total = 0
        matched = False
        for repo in revised:
            count = _metadata_int(children_by_repo.get(repo))
            if count is not None:
                total += count
                matched = True
        if matched:
            return total
    child_count = _metadata_int(metadata.get("child_count"))
    base = tuple(str(repo).strip() for repo in base_repos if str(repo).strip())
    if child_count is not None and revised == base:
        return child_count
    if revised:
        return len(revised)
    return child_count


def _issue_url_from_record(record: SlackThreadRecord) -> str | None:
    explicit = str(record.metadata.get("issue_url") or "").strip()
    if explicit:
        return explicit
    if record.parent_repo and record.parent_issue:
        return f"https://github.com/{record.parent_repo}/issues/{record.parent_issue}"
    return None


def _control_result_failed(control: Any) -> bool:
    """A consumed Slack control is not necessarily a successful mutation."""
    if not bool(getattr(control, "handled", False)):
        return True
    action = str(getattr(control, "action", "") or "")
    return action.endswith("_failed") or action.endswith("_rejected")


def _safe_event_id(event_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", event_id).strip("_") or "event"


def _thread_title_from_text(text: str, *, limit: int = 90) -> str:
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "Slack conversation"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def _default_state_root() -> Path:
    home = (os.environ.get("ALFRED_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state"
    return Path.home() / ".alfred" / "state"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
