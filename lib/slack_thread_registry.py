"""Persistent registry for Slack threads Alfred can safely react to.

The listener only treats a thread as actionable when Alfred previously
registered the root message. That keeps channel chatter from becoming
implicit instructions while still letting users refine plans in Slack.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SlackThreadRecord:
    kind: str
    channel: str
    thread_ts: str
    codename: str = ""
    firing_id: str = ""
    title: str = ""
    status: str = "open"
    parent_repo: str = ""
    parent_issue: int | None = None
    plan_path: str = ""
    draft_path: str = ""
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def default_registry_root() -> Path:
    home = (os.environ.get("ALFRED_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state" / "slack-threads"
    return Path.home() / ".alfred" / "state" / "slack-threads"


class SlackThreadRegistry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_registry_root()

    def register(self, record: SlackThreadRecord) -> SlackThreadRecord:
        now = _utc_now()
        existing = self.lookup(record.channel, record.thread_ts)
        created = record.created_at or (existing.created_at if existing else now)
        out = SlackThreadRecord(
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
            created_at=created,
            updated_at=now,
            metadata=dict(record.metadata or {}),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(out.channel, out.thread_ts)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(out), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return out

    def lookup(self, channel: str, thread_ts: str) -> SlackThreadRecord | None:
        path = self._path(channel, thread_ts)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        return _record_from_dict(raw)

    def append_feedback(
        self,
        record: SlackThreadRecord,
        *,
        author: str,
        text: str,
        ts: str,
    ) -> Path:
        path = self.root / "feedback" / f"{_safe_key(record.channel, record.thread_ts)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "author": author,
            "text": text,
            "ts": ts,
            "captured_at": _utc_now(),
            "kind": record.kind,
            "channel": record.channel,
            "thread_ts": record.thread_ts,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        return path

    def mark_status(self, record: SlackThreadRecord, status: str) -> SlackThreadRecord:
        return self.register(
            SlackThreadRecord(
                kind=record.kind,
                channel=record.channel,
                thread_ts=record.thread_ts,
                codename=record.codename,
                firing_id=record.firing_id,
                title=record.title,
                status=status,
                parent_repo=record.parent_repo,
                parent_issue=record.parent_issue,
                plan_path=record.plan_path,
                draft_path=record.draft_path,
                created_at=record.created_at,
                metadata=record.metadata,
            )
        )

    def _path(self, channel: str, thread_ts: str) -> Path:
        return self.root / f"{_safe_key(channel, thread_ts)}.json"


def _record_from_dict(raw: dict[str, Any]) -> SlackThreadRecord:
    parent_issue = raw.get("parent_issue")
    if parent_issue is not None:
        try:
            parent_issue = int(parent_issue)
        except (TypeError, ValueError):
            parent_issue = None
    metadata = raw.get("metadata")
    return SlackThreadRecord(
        kind=str(raw.get("kind") or ""),
        channel=str(raw.get("channel") or ""),
        thread_ts=str(raw.get("thread_ts") or ""),
        codename=str(raw.get("codename") or ""),
        firing_id=str(raw.get("firing_id") or ""),
        title=str(raw.get("title") or ""),
        status=str(raw.get("status") or "open"),
        parent_repo=str(raw.get("parent_repo") or ""),
        parent_issue=parent_issue,
        plan_path=str(raw.get("plan_path") or ""),
        draft_path=str(raw.get("draft_path") or ""),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _safe_key(channel: str, thread_ts: str) -> str:
    raw = f"{channel}-{thread_ts}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("_") or "thread"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
