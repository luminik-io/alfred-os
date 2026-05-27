"""Optional Redis Agent Memory Server provider.

This adapter keeps Redis AMS outside the default install. Operators who
already run https://github.com/redis/agent-memory-server can add it to
the provider chain with ``ALFRED_MEMORY_PROVIDERS=fleet,redis``.

The provider is deliberately tolerant: recall failures return ``[]``;
reflect failures raise :class:`NotImplementedError` so a chained writer
can fall through to ``fleet``.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fleet_brain import Lesson, Severity, new_id

__all__ = ["RedisAgentMemoryProvider"]

_LOG = logging.getLogger(__name__)

_JSON = "application/json"
_DEFAULT_URL = "http://127.0.0.1:8000"
_DEFAULT_TIMEOUT_S = 5.0

Transport = Callable[[str, dict[str, Any], dict[str, str], float], Any]


@dataclass
class RedisAgentMemoryProvider:
    """Bridge Alfred's memory Protocol to Redis Agent Memory Server."""

    base_url: str = _DEFAULT_URL
    token: str | None = None
    namespace: str = "alfred"
    user_id: str | None = None
    timeout_s: float = _DEFAULT_TIMEOUT_S
    search_mode: str = "hybrid"
    transport: Transport | None = None
    name: str = "redis"

    @classmethod
    def from_env(
        cls,
        *,
        env: Mapping[str, str] | None = None,
    ) -> RedisAgentMemoryProvider:
        envmap = env if env is not None else os.environ
        timeout_raw = envmap.get("ALFRED_REDIS_MEMORY_TIMEOUT_S", "")
        try:
            timeout = float(timeout_raw) if timeout_raw else _DEFAULT_TIMEOUT_S
        except ValueError:
            timeout = _DEFAULT_TIMEOUT_S
        return cls(
            base_url=(envmap.get("ALFRED_REDIS_MEMORY_URL") or _DEFAULT_URL).rstrip("/"),
            token=(envmap.get("ALFRED_REDIS_MEMORY_TOKEN") or "").strip() or None,
            namespace=(envmap.get("ALFRED_REDIS_MEMORY_NAMESPACE") or "alfred").strip() or "alfred",
            user_id=(envmap.get("ALFRED_REDIS_MEMORY_USER_ID") or "").strip() or None,
            timeout_s=timeout,
            search_mode=(envmap.get("ALFRED_REDIS_MEMORY_SEARCH_MODE") or "hybrid").strip()
            or "hybrid",
        )

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        text = (query or " ".join(x for x in (codename, repo) if x) or "alfred").strip()
        payload: dict[str, Any] = {
            "text": text,
            "limit": max(1, int(limit)),
            "search_mode": self.search_mode,
            "namespace": {"eq": self.namespace},
        }
        if self.user_id:
            payload["user_id"] = {"eq": self.user_id}
        try:
            response = self._post("/v1/long-term-memory/search", payload)
        except Exception as exc:
            _LOG.debug("memory.redis: recall failed: %s", exc)
            return []
        return _parse_search_response(response, codename=codename, repo=repo)

    def reflect(
        self,
        *,
        codename: str,
        repo: str,
        body: str,
        tags: Iterable[str] | None = None,
        severity: Severity = "info",
        firing_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Lesson:
        created = created_at or datetime.now(UTC)
        clean_tags = sorted({str(tag).strip() for tag in (tags or []) if str(tag).strip()})
        topics = sorted(
            {
                "alfred",
                f"codename:{codename}",
                f"repo:{repo}",
                f"severity:{severity}",
                *clean_tags,
            }
        )
        lesson = Lesson(
            id=new_id(),
            codename=codename,
            repo=repo,
            body=body.strip(),
            tags=clean_tags,
            created_at=created,
            firing_id=firing_id,
            severity=severity,
        )
        payload: dict[str, Any] = {
            "memories": [
                {
                    "id": lesson.id,
                    "text": lesson.body,
                    "topics": topics,
                    "memory_type": "semantic",
                    "namespace": self.namespace,
                    "session_id": firing_id,
                    "entities": [codename, repo],
                    "created_at": created.astimezone(UTC).isoformat(),
                    "updated_at": created.astimezone(UTC).isoformat(),
                }
            ],
            "deduplicate": True,
        }
        if self.user_id:
            payload["memories"][0]["user_id"] = self.user_id
        try:
            self._post("/v1/long-term-memory/", payload)
        except Exception as exc:
            raise NotImplementedError(
                "RedisAgentMemoryProvider could not write; falling through "
                "to the next memory provider."
            ) from exc
        return lesson

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": _JSON, "Content-Type": _JSON}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.transport is not None:
            return self.transport(url, payload, headers, self.timeout_s)
        return _default_transport(url, payload, headers, self.timeout_s)


def _default_transport(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> Any:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not raw.strip():
        return {}
    return json.loads(raw)


def _parse_search_response(
    response: Any,
    *,
    codename: str | None,
    repo: str | None,
) -> list[Lesson]:
    entries = _response_entries(response)
    out: list[Lesson] = []
    for entry in entries:
        lesson = _entry_to_lesson(entry, codename=codename, repo=repo)
        if lesson is not None:
            out.append(lesson)
    return out


def _response_entries(response: Any) -> list[Any]:
    if isinstance(response, list):
        return response
    if not isinstance(response, dict):
        return []
    value = response.get("memories") or response.get("results") or response.get("items")
    return value if isinstance(value, list) else []


def _entry_to_lesson(
    entry: Any,
    *,
    codename: str | None,
    repo: str | None,
) -> Lesson | None:
    if not isinstance(entry, dict):
        return None
    record = entry.get("memory") or entry.get("record") or entry
    if not isinstance(record, dict):
        return None
    text = record.get("text") or record.get("body") or record.get("content")
    if not isinstance(text, str) or not text.strip():
        return None
    raw_metadata = record.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_topics = record.get("topics")
    topics: list[Any] = raw_topics if isinstance(raw_topics, list) else []
    control = _control_topics(topics)
    tags = sorted(
        {
            str(topic).strip()
            for topic in topics
            if str(topic).strip()
            and str(topic).strip() != "alfred"
            and ":" not in str(topic).strip()
        }
    )
    severity_raw = metadata.get("severity")
    severity_candidate = severity_raw or control.get("severity")
    severity: Severity = "info"
    if severity_candidate in ("info", "warning", "blocker"):
        severity = cast(Severity, severity_candidate)
    created_at = _parse_created_at(record.get("created_at") or metadata.get("created_at"))
    return Lesson(
        id=str(record.get("id") or new_id()),
        codename=str(metadata.get("codename") or control.get("codename") or codename or ""),
        repo=str(metadata.get("repo") or control.get("repo") or repo or ""),
        body=text.strip(),
        tags=tags,
        created_at=created_at,
        firing_id=metadata.get("firing_id") or record.get("session_id"),
        severity=severity,
    )


def _control_topics(topics: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in topics:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"codename", "repo", "severity"} and value:
            out[key] = value
    return out


def _parse_created_at(value: Any) -> datetime:
    if not isinstance(value, str):
        return datetime.now(UTC)
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
