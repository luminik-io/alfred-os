"""Slack-native planning listener for Alfred.

The listener turns Slack into an intake and refinement surface without
making chat itself an approval mechanism. It can:

* capture trusted replies in known Alfred plan/report threads;
* create a planning draft from a DM or app mention;
* acknowledge what changed and what still needs scope.

Execution remains gated by the existing reaction approval flow.
"""

from __future__ import annotations

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

from planning_assistant import (
    Refiner,
    engine_refiner_from_env,
    refine_issue_draft,
    render_operator_feedback_ack,
    render_post_pr_feedback_ack,
)
from slack_approval import (
    ThreadFeedback,
    default_slack_client,
    operator_user_id_from_env,
    resolve_bot_token,
    trusted_feedback_user_ids_from_env,
)
from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry
from spec_helper import IssueDraft

ENV_APP_TOKEN = "SLACK_APP_TOKEN"
ENV_ALT_APP_TOKEN = "ALFRED_SLACK_APP_TOKEN"
ENV_BOT_USER_ID = "ALFRED_SLACK_BOT_USER_ID"
_MAX_STORED_REVISIONS = 50
_DRAFT_REVISION_LOCKS: dict[str, threading.Lock] = {}
_DRAFT_REVISION_LOCKS_GUARD = threading.Lock()


class SlackPoster(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any: ...


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

    @property
    def root_ts(self) -> str:
        return self.thread_ts or self.ts

    @property
    def is_thread_reply(self) -> bool:
        return bool(self.thread_ts and self.thread_ts != self.ts)

    @property
    def is_direct_intake(self) -> bool:
        return self.event_type == "app_mention" or self.channel_type == "im"


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
    ) -> None:
        self.state_root = state_root or _default_state_root()
        self.registry = registry or SlackThreadRegistry(self.state_root / "slack-threads")
        self.poster = poster
        operator = operator_user_id_from_env()
        self.trusted_user_ids = set(
            trusted_user_ids
            if trusted_user_ids is not None
            else trusted_feedback_user_ids_from_env(operator)
        )
        self.bot_user_id = bot_user_id or (os.environ.get(ENV_BOT_USER_ID) or "").strip()
        self.seen = seen_store or SeenEventStore(self.state_root / "slack-listener" / "seen")
        self.refiner = refiner
        self.memory_provider = memory_provider
        self._now = now or (lambda: datetime.now(UTC))

    def handle_payload(self, payload: dict[str, Any]) -> ListenerResult:
        event = parse_slack_payload(payload)
        if event is None:
            return ListenerResult(False, "ignored", "unsupported Slack event")
        if self.seen.mark_seen(event.event_id):
            return ListenerResult(False, "duplicate", "event already processed")
        if self.bot_user_id and event.user == self.bot_user_id:
            return ListenerResult(False, "ignored", "bot self-message")
        if not self.trusted_user_ids:
            return ListenerResult(False, "ignored", "no trusted Slack users configured")
        if event.user not in self.trusted_user_ids:
            return ListenerResult(False, "ignored", "untrusted Slack user")

        record = self.registry.lookup(event.channel, event.root_ts)
        if record is not None and event.is_thread_reply:
            return self._handle_registered_thread(event, record)
        if event.is_direct_intake:
            return self._handle_direct_intake(event)
        return ListenerResult(False, "ignored", "message is not a registered thread or intake")

    def _handle_registered_thread(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
    ) -> ListenerResult:
        feedback = ThreadFeedback(author=event.user, text=event.text, ts=event.ts)
        self.registry.append_feedback(
            record, author=feedback.author, text=feedback.text, ts=feedback.ts
        )
        if record.kind == "draft":
            return self._handle_draft_revision(event, record, feedback)
        if record.kind in {"report", "pr", "followup"}:
            ack = render_post_pr_feedback_ack([feedback.text])
            action = "captured_followup"
        else:
            ack = render_operator_feedback_ack([feedback.text])
            action = "captured_plan_feedback"
        self._post_thread_ack(event.channel, event.root_ts, ack or "*Feedback captured*")
        return ListenerResult(True, action, thread_kind=record.kind)

    def _handle_direct_intake(self, event: SlackInputEvent) -> ListenerResult:
        draft = draft_from_slack_text(event.text)
        refined = refine_issue_draft(
            draft,
            [],
            refiner=self.refiner,
            memory_provider=self.memory_provider,
        )
        draft_path = self._save_draft(
            event,
            refined.draft,
            refined.issue_body,
            refined.spec_body,
            readiness=refined.readiness,
            memory=refined.memory,
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

    def _handle_draft_revision(
        self,
        event: SlackInputEvent,
        record: SlackThreadRecord,
        feedback: ThreadFeedback,
    ) -> ListenerResult:
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
        self._post_thread_ack(event.channel, event.root_ts, render_draft_revision_ack(refined))
        return ListenerResult(
            True,
            "draft_revised",
            draft_path=str(payload_path),
            thread_kind=updated_record.kind,
            readiness_ok=refined.readiness.ok,
            readiness_score=refined.readiness.score,
        )

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


def parse_slack_payload(payload: dict[str, Any]) -> SlackInputEvent | None:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
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
        open_questions=fields.get("question") or fields.get("questions") or "",
    )


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
            "`desired: ...`, `acceptance: ...`, `test: ...`, or `question: ...`.",
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
    )
    client = SocketModeClient(app_token=app_token, web_client=poster)

    def _handler(socket_client: Any, req: Any) -> None:
        socket_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if getattr(req, "type", "") == "events_api":
            try:
                active.handle_payload(getattr(req, "payload", {}) or {})
            except Exception as exc:
                print(f"[SLACK-LISTENER-WARN] handle_payload failed: {exc}", file=sys.stderr)

    client.socket_mode_request_listeners.append(_handler)
    client.connect()
    while True:
        time.sleep(60)


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


def _read_draft_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


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
        print(f"[SLACK-LISTENER-WARN] memory provider unavailable: {exc}", file=sys.stderr)
        return None
    try:
        return load_provider()
    except Exception as exc:
        print(f"[SLACK-LISTENER-WARN] memory provider failed to load: {exc}", file=sys.stderr)
        return None


def _safe_event_id(event_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", event_id).strip("_") or "event"


def _default_state_root() -> Path:
    home = (os.environ.get("ALFRED_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state"
    return Path.home() / ".alfred" / "state"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
