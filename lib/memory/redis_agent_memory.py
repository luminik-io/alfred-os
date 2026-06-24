"""Redis Agent Memory Server provider.

This adapter is Alfred's primary semantic memory client. A fresh install
talks to the bundled loopback AMS by default; ``ALFRED_REDIS_MEMORY_URL``
can point it at a different endpoint.

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

from .ams_server import DEFAULT_HOST, DEFAULT_PORT

__all__ = ["RedisAgentMemoryProvider"]

_LOG = logging.getLogger(__name__)

_JSON = "application/json"
_DEFAULT_TIMEOUT_S = 2.0
_AMS_DEFAULT_HOST = DEFAULT_HOST
_AMS_DEFAULT_PORT = DEFAULT_PORT

Transport = Callable[[str, str, dict[str, Any] | None, dict[str, str], float], Any]


def _ams_default_url(envmap: Mapping[str, str]) -> str:
    host = (envmap.get("ALFRED_AMS_HOST") or "").strip() or _AMS_DEFAULT_HOST
    port_raw = (envmap.get("ALFRED_AMS_PORT") or "").strip()
    try:
        port = int(port_raw) if port_raw else _AMS_DEFAULT_PORT
    except ValueError:
        port = _AMS_DEFAULT_PORT
    return f"http://{host}:{port}"


@dataclass
class RedisAgentMemoryProvider:
    """Bridge Alfred's memory Protocol to Redis Agent Memory Server."""

    base_url: str = f"http://{_AMS_DEFAULT_HOST}:{_AMS_DEFAULT_PORT}"
    token: str | None = None
    namespace: str = "alfred"
    user_id: str | None = None
    timeout_s: float = _DEFAULT_TIMEOUT_S
    search_mode: str = "semantic"
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
            base_url=(envmap.get("ALFRED_REDIS_MEMORY_URL") or _ams_default_url(envmap)).rstrip(
                "/"
            ),
            token=(
                envmap.get("ALFRED_REDIS_MEMORY_TOKEN") or envmap.get("ALFRED_AMS_TOKEN") or ""
            ).strip()
            or None,
            namespace=(envmap.get("ALFRED_REDIS_MEMORY_NAMESPACE") or "alfred").strip() or "alfred",
            user_id=(envmap.get("ALFRED_REDIS_MEMORY_USER_ID") or "").strip() or None,
            timeout_s=timeout,
            search_mode=(envmap.get("ALFRED_REDIS_MEMORY_SEARCH_MODE") or "semantic").strip()
            or "semantic",
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
        required_topics = _scope_topics(codename=codename, repo=repo)
        if required_topics:
            payload["topics"] = {"all": required_topics}
        if self.user_id:
            payload["user_id"] = {"eq": self.user_id}
        try:
            response = self._request("POST", "/v1/long-term-memory/search", payload)
        except Exception as exc:
            _LOG.debug("memory.redis: recall failed: %s", exc)
            return []
        return _parse_search_response(
            response,
            codename=codename,
            repo=repo,
            namespace=self.namespace,
            user_id=self.user_id,
            required_topics=required_topics,
        )

    def recall_scored(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[tuple[Lesson, float | None]]:
        """Like :meth:`recall`, but pair each lesson with its relevance score.

        The score is a similarity in ``[0, 1]`` (higher is closer) derived from
        the AMS search response. ``None`` means the server did not report a
        score for that entry; gating treats ``None`` as "cannot judge" and lets
        the lesson through rather than silently dropping it.
        """
        text = (query or " ".join(x for x in (codename, repo) if x) or "alfred").strip()
        payload: dict[str, Any] = {
            "text": text,
            "limit": max(1, int(limit)),
            "search_mode": self.search_mode,
            "namespace": {"eq": self.namespace},
        }
        required_topics = _scope_topics(codename=codename, repo=repo)
        if required_topics:
            payload["topics"] = {"all": required_topics}
        if self.user_id:
            payload["user_id"] = {"eq": self.user_id}
        try:
            response = self._request("POST", "/v1/long-term-memory/search", payload)
        except Exception as exc:
            _LOG.debug("memory.redis: recall_scored failed: %s", exc)
            return []
        out: list[tuple[Lesson, float | None]] = []
        for entry in _response_entries(response):
            lesson = _entry_to_lesson(
                entry,
                codename=codename,
                repo=repo,
                namespace=self.namespace,
                user_id=self.user_id,
                required_topics=required_topics,
            )
            if lesson is not None:
                out.append((lesson, _entry_relevance(entry)))
        return out

    def health(self) -> dict[str, Any]:
        """Return Redis AMS health data, normalized for ``alfred brain``.

        The AMS REST API exposes ``GET /v1/health``. Alfred keeps this
        helper on the provider rather than the Protocol because health
        checks are operator tooling, not runner context.
        """
        try:
            response = self._request("GET", "/v1/health", None)
        except Exception as exc:
            return {
                "ok": False,
                "base_url": self.base_url,
                "namespace": self.namespace,
                "error": str(exc),
            }
        return {
            "ok": True,
            "base_url": self.base_url,
            "namespace": self.namespace,
            "response": response,
        }

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
        memory_id: str | None = None,
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
            id=memory_id or new_id(),
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
            # Dedup is handled upstream in Python before the write. The AMS
            # server-side merge runs the weak local llama3.2:1b generation
            # model, which rewrites and corrupts the stored lesson text, so we
            # never ask the server to deduplicate.
            "deduplicate": False,
        }
        if self.user_id:
            payload["memories"][0]["user_id"] = self.user_id
        try:
            self._request("POST", "/v1/long-term-memory/", payload)
        except Exception as exc:
            raise NotImplementedError(
                "RedisAgentMemoryProvider could not write; falling through "
                "to the next memory provider."
            ) from exc
        return lesson

    def sync_lesson(self, lesson: Lesson) -> bool:
        """Mirror one trusted fleet-brain lesson into Redis AMS.

        This is deliberately explicit. Alfred does not stream raw event
        logs or unreviewed candidates into Redis; operators sync trusted
        lessons after review.
        """
        try:
            self.reflect(
                codename=lesson.codename,
                repo=lesson.repo,
                body=lesson.body,
                tags=lesson.tags,
                severity=lesson.severity,
                firing_id=lesson.firing_id,
                created_at=lesson.created_at,
                memory_id=lesson.id,
            )
        except NotImplementedError:
            return False
        return True

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Accept": _JSON}
        if payload is not None:
            headers["Content-Type"] = _JSON
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.transport is not None:
            return self.transport(method, url, payload, headers, self.timeout_s)
        return _default_transport(method, url, payload, headers, self.timeout_s)


def _default_transport(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout_s: float,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers=headers,
        method=method,
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
    namespace: str | None = None,
    user_id: str | None = None,
    required_topics: list[str] | None = None,
) -> list[Lesson]:
    entries = _response_entries(response)
    out: list[Lesson] = []
    for entry in entries:
        lesson = _entry_to_lesson(
            entry,
            codename=codename,
            repo=repo,
            namespace=namespace,
            user_id=user_id,
            required_topics=required_topics or [],
        )
        if lesson is not None:
            out.append(lesson)
    return out


def _entry_relevance(entry: Any) -> float | None:
    """Extract a similarity score in ``[0, 1]`` from one search entry.

    AMS variants report either a similarity (``score`` / ``relevance``, higher
    is closer) or a vector distance (``dist`` / ``distance``, lower is closer).
    We normalize both to a similarity so a single threshold works regardless of
    which field the server emits. Returns ``None`` when no usable number is
    present so gating can let the lesson through instead of guessing.
    """
    if not isinstance(entry, dict):
        return None
    record = entry.get("memory") or entry.get("record") or entry
    sources = (entry, record if isinstance(record, dict) else {})
    for src in sources:
        for key in ("score", "relevance", "similarity"):
            val = src.get(key)
            if isinstance(val, (int, float)):
                return max(0.0, min(1.0, float(val)))
    for src in sources:
        for key in ("dist", "distance"):
            val = src.get(key)
            if isinstance(val, (int, float)):
                # Cosine distance in [0, 2]; map to similarity in [0, 1].
                return max(0.0, min(1.0, 1.0 - float(val) / 2.0))
    return None


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
    namespace: str | None,
    user_id: str | None,
    required_topics: list[str],
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
    if not _record_scope_matches(record, metadata, "namespace", namespace):
        return None
    if not _record_scope_matches(record, metadata, "user_id", user_id):
        return None
    raw_topics = record.get("topics")
    topics: list[Any] = raw_topics if isinstance(raw_topics, list) else []
    if required_topics and not _has_required_topics(topics, required_topics):
        return None
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


def _scope_topics(*, codename: str | None, repo: str | None) -> list[str]:
    out = []
    if codename:
        out.append(f"codename:{codename}")
    if repo:
        out.append(f"repo:{repo}")
    return out


def _has_required_topics(topics: list[Any], required_topics: list[str]) -> bool:
    topic_set = {topic.strip() for topic in topics if isinstance(topic, str) and topic.strip()}
    return all(topic in topic_set for topic in required_topics)


def _record_scope_matches(
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    key: str,
    expected: str | None,
) -> bool:
    if not expected:
        return True
    raw = record.get(key)
    if raw is None:
        raw = metadata.get(key)
    if raw is None:
        return True
    return str(raw).strip() == expected


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
