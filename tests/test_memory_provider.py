"""Tests for the memory-provider Protocol, providers, and config parser.

Covers:

* :class:`MemoryProvider` Protocol -- each concrete class is
  ``isinstance``-checkable.
* :class:`FleetBrainProvider` wraps :class:`FleetBrain` and routes
  ``recall`` / ``reflect`` through it.
* :class:`ChainedMemoryProvider` consults providers in order, returns
  the first non-empty ``recall``, skips read-only providers for
  ``reflect``, and tolerates a flaky provider.
* :class:`NullMemoryProvider` is a no-op.
* :mod:`alfred.memory.config` parses ``ALFRED_MEMORY_PROVIDERS`` and
  builds the right chain shape.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "lib"))

from fleet_brain import FleetBrain, Lesson, Severity, SQLiteStore  # noqa: E402
from memory import MemoryProvider  # noqa: E402
from memory.config import (  # noqa: E402
    PROVIDER_REGISTRY,
    build_chain,
    load_provider,
    parse_provider_names,
)
from memory.gbrain_stub import GBrainProvider  # noqa: E402
from memory.providers import (  # noqa: E402
    ChainedMemoryProvider,
    FleetBrainProvider,
    NullMemoryProvider,
)
from memory.redis_agent_memory import RedisAgentMemoryProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet_brain_provider() -> FleetBrainProvider:
    """An in-memory FleetBrain wrapped as a provider. No on-disk side effects."""
    brain = FleetBrain(store=SQLiteStore(db_path=Path(":memory:")))
    return FleetBrainProvider(brain=brain)


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------


def test_protocol_isinstance_for_all_concrete_providers(
    fleet_brain_provider: FleetBrainProvider,
) -> None:
    null = NullMemoryProvider()
    gbrain = GBrainProvider()
    chain = ChainedMemoryProvider(providers=[null])
    redis = RedisAgentMemoryProvider()
    assert isinstance(fleet_brain_provider, MemoryProvider)
    assert isinstance(null, MemoryProvider)
    assert isinstance(gbrain, MemoryProvider)
    assert isinstance(redis, MemoryProvider)
    assert isinstance(chain, MemoryProvider)


def test_protocol_required_attributes(fleet_brain_provider: FleetBrainProvider) -> None:
    """Every provider exposes ``name`` and the two methods."""
    for p in (
        fleet_brain_provider,
        NullMemoryProvider(),
        GBrainProvider(),
        RedisAgentMemoryProvider(),
        ChainedMemoryProvider(providers=[NullMemoryProvider()]),
    ):
        assert isinstance(p.name, str) and p.name
        assert callable(p.recall)
        assert callable(p.reflect)


# ---------------------------------------------------------------------------
# FleetBrainProvider
# ---------------------------------------------------------------------------


def test_fleet_brain_provider_round_trips(fleet_brain_provider: FleetBrainProvider) -> None:
    fleet_brain_provider.reflect(
        codename="lucius",
        repo="acme-org/api",
        body="GraphQL schema lives in src/schema.graphql",
        tags=["graphql"],
    )
    out = fleet_brain_provider.recall(codename="lucius", repo="acme-org/api")
    assert len(out) == 1
    assert out[0].body == "GraphQL schema lives in src/schema.graphql"
    assert out[0].tags == ["graphql"]


def test_fleet_brain_provider_recall_filters(
    fleet_brain_provider: FleetBrainProvider,
) -> None:
    fleet_brain_provider.reflect(codename="lucius", repo="acme-org/api", body="A")
    fleet_brain_provider.reflect(codename="bane", repo="acme-org/api", body="B")
    only_lucius = fleet_brain_provider.recall(codename="lucius")
    assert [L.body for L in only_lucius] == ["A"]


# ---------------------------------------------------------------------------
# NullMemoryProvider
# ---------------------------------------------------------------------------


def test_null_provider_recall_empty() -> None:
    assert NullMemoryProvider().recall(query="anything") == []


def test_null_provider_reflect_raises() -> None:
    with pytest.raises(NotImplementedError):
        NullMemoryProvider().reflect(codename="lucius", repo="acme-org/api", body="x")


# ---------------------------------------------------------------------------
# ChainedMemoryProvider
# ---------------------------------------------------------------------------


class _StaticProvider:
    """Test double: returns a fixed list and records reflect calls."""

    def __init__(
        self,
        *,
        name: str,
        lessons: list[Lesson] | None = None,
        writable: bool = True,
    ) -> None:
        self.name = name
        self._lessons = lessons or []
        self._writable = writable
        self.reflect_calls: list[dict[str, object]] = []
        self.recall_calls = 0

    def recall(
        self,
        *,
        query: str | None = None,
        codename: str | None = None,
        repo: str | None = None,
        limit: int = 5,
    ) -> list[Lesson]:
        self.recall_calls += 1
        return list(self._lessons[:limit])

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
        if not self._writable:
            raise NotImplementedError(f"{self.name} is read-only")
        record = {
            "codename": codename,
            "repo": repo,
            "body": body,
            "tags": list(tags or []),
            "severity": severity,
        }
        self.reflect_calls.append(record)
        return Lesson(
            id="LSN1",
            codename=codename,
            repo=repo,
            body=body,
            tags=sorted(set(record["tags"])),  # type: ignore[arg-type]
            created_at=datetime.now(),
            firing_id=firing_id,
            severity=severity,
        )


class _BoomProvider(_StaticProvider):
    """Test double: raises on recall to exercise chain tolerance."""

    def recall(self, **kwargs: object) -> list[Lesson]:  # type: ignore[override]
        raise RuntimeError("boom")


def _make_lesson(body: str, *, codename: str = "x", repo: str = "y") -> Lesson:
    return Lesson(
        id="L_" + body,
        codename=codename,
        repo=repo,
        body=body,
        tags=[],
        created_at=datetime.now(),
        firing_id=None,
        severity="info",
    )


def test_chain_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError):
        ChainedMemoryProvider(providers=[])


def test_chain_returns_first_non_empty_recall() -> None:
    first = _StaticProvider(name="first", lessons=[])
    second = _StaticProvider(name="second", lessons=[_make_lesson("hit")])
    third = _StaticProvider(name="third", lessons=[_make_lesson("never")])
    chain = ChainedMemoryProvider(providers=[first, second, third])
    out = chain.recall(query="q")
    assert [L.body for L in out] == ["hit"]
    # third must not be consulted -- ordering matters
    assert third.recall_calls == 0


def test_chain_falls_through_when_all_empty() -> None:
    a = _StaticProvider(name="a", lessons=[])
    b = _StaticProvider(name="b", lessons=[])
    chain = ChainedMemoryProvider(providers=[a, b])
    assert chain.recall(query="anything") == []


def test_chain_tolerates_provider_exception() -> None:
    flaky = _BoomProvider(name="flaky")
    ok = _StaticProvider(name="ok", lessons=[_make_lesson("hello")])
    chain = ChainedMemoryProvider(providers=[flaky, ok])
    out = chain.recall(query="q")
    assert [L.body for L in out] == ["hello"]


def test_chain_reflect_skips_readonly_then_writes() -> None:
    read_only = _StaticProvider(name="ro", writable=False)
    writable = _StaticProvider(name="rw", writable=True)
    chain = ChainedMemoryProvider(providers=[read_only, writable])
    chain.reflect(codename="lucius", repo="acme-org/api", body="learned a thing")
    assert read_only.reflect_calls == []
    assert len(writable.reflect_calls) == 1
    assert writable.reflect_calls[0]["body"] == "learned a thing"


def test_chain_reflect_raises_when_all_readonly() -> None:
    ro1 = _StaticProvider(name="ro1", writable=False)
    ro2 = _StaticProvider(name="ro2", writable=False)
    chain = ChainedMemoryProvider(providers=[ro1, ro2])
    with pytest.raises(NotImplementedError):
        chain.reflect(codename="lucius", repo="acme-org/api", body="x")


# ---------------------------------------------------------------------------
# GBrainProvider stub
# ---------------------------------------------------------------------------


def test_gbrain_provider_missing_binary_returns_empty() -> None:
    provider = GBrainProvider(binary_path=Path("/no/such/binary"))
    assert provider.recall(query="x") == []


def test_gbrain_provider_reflect_always_read_only() -> None:
    with pytest.raises(NotImplementedError):
        GBrainProvider().reflect(codename="lucius", repo="acme-org/api", body="x")


def test_gbrain_provider_invokes_binary_and_parses(tmp_path: Path) -> None:
    """End-to-end: write a tiny shell script that emits a JSON lesson list."""
    payload = [
        {
            "body": "kb hit",
            "tags": ["graphql", "layout"],
            "codename": "lucius",
            "repo": "acme-org/api",
        }
    ]
    binary = tmp_path / "fake-kb"
    binary.write_text(
        "#!/bin/sh\ncat >/dev/null\nprintf '%s' '" + json.dumps(payload) + "'\n",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    provider = GBrainProvider(binary_path=binary)
    out = provider.recall(query="schema", codename="lucius", repo="acme-org/api")
    assert len(out) == 1
    assert out[0].body == "kb hit"
    assert out[0].tags == ["graphql", "layout"]
    assert out[0].codename == "lucius"


def test_gbrain_provider_from_env_handles_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_GBRAIN_BIN", raising=False)
    provider = GBrainProvider.from_env(env={})
    assert provider.recall(query="x") == []


def test_gbrain_provider_tolerates_garbage_output(tmp_path: Path) -> None:
    binary = tmp_path / "garbage-kb"
    binary.write_text("#!/bin/sh\ncat >/dev/null\nprintf 'not json'\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    provider = GBrainProvider(binary_path=binary)
    assert provider.recall(query="x") == []


# ---------------------------------------------------------------------------
# RedisAgentMemoryProvider
# ---------------------------------------------------------------------------


def test_redis_provider_from_env() -> None:
    provider = RedisAgentMemoryProvider.from_env(
        env={
            "ALFRED_REDIS_MEMORY_URL": "http://memory.local/",
            "ALFRED_REDIS_MEMORY_TOKEN": "token",
            "ALFRED_REDIS_MEMORY_NAMESPACE": "team",
            "ALFRED_REDIS_MEMORY_USER_ID": "operator",
            "ALFRED_REDIS_MEMORY_TIMEOUT_S": "1.5",
        }
    )

    assert provider.base_url == "http://memory.local"
    assert provider.token == "token"
    assert provider.namespace == "team"
    assert provider.user_id == "operator"
    assert provider.timeout_s == 1.5


def test_redis_provider_recall_posts_search_payload() -> None:
    calls: list[dict[str, object]] = []

    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        calls.append(
            {
                "method": method,
                "url": url,
                "payload": payload,
                "headers": headers,
                "timeout_s": timeout_s,
            }
        )
        return {
            "memories": [
                {
                    "memory": {
                        "id": "redis-1",
                        "text": "Use owner/repo in Batman plans.",
                        "topics": ["batman", "plans"],
                        "metadata": {
                            "codename": "batman",
                            "repo": "acme/app",
                            "severity": "warning",
                        },
                    }
                }
            ]
        }

    provider = RedisAgentMemoryProvider(
        base_url="http://memory.local",
        token="secret",
        namespace="alfred",
        transport=transport,
    )

    lessons = provider.recall(query="plans", codename="batman", repo="acme/app", limit=2)

    assert lessons[0].body == "Use owner/repo in Batman plans."
    assert lessons[0].tags == ["batman", "plans"]
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://memory.local/v1/long-term-memory/search"
    payload = calls[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["text"] == "plans"
    assert payload["limit"] == 2
    assert payload["namespace"] == {"eq": "alfred"}
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer secret"


def test_redis_provider_health_uses_health_endpoint() -> None:
    calls: list[dict[str, object]] = []

    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        return {"status": "healthy"}

    provider = RedisAgentMemoryProvider(base_url="http://memory.local", transport=transport)

    health = provider.health()

    assert health["ok"] is True
    assert health["response"] == {"status": "healthy"}
    assert calls == [
        {
            "method": "GET",
            "url": "http://memory.local/v1/health",
            "payload": None,
            "headers": {"Accept": "application/json"},
        }
    ]


def test_redis_provider_health_reports_error() -> None:
    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        raise RuntimeError("down")

    provider = RedisAgentMemoryProvider(base_url="http://memory.local", transport=transport)

    assert provider.health() == {
        "ok": False,
        "base_url": "http://memory.local",
        "namespace": "alfred",
        "error": "down",
    }


def test_redis_provider_recall_parses_supported_record_fields() -> None:
    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        return {
            "memories": [
                {
                    "id": "redis-2",
                    "text": "Prompt seeding should happen before dry-run.",
                    "topics": [
                        "alfred",
                        "codename:batman",
                        "repo:acme/app",
                        "severity:blocker",
                        "plans",
                    ],
                    "session_id": "fire-1",
                    "created_at": "2026-05-27T12:00:00Z",
                }
            ],
            "total": 1,
        }

    provider = RedisAgentMemoryProvider(transport=transport)
    lessons = provider.recall(query="dry-run")

    assert lessons[0].codename == "batman"
    assert lessons[0].repo == "acme/app"
    assert lessons[0].severity == "blocker"
    assert lessons[0].tags == ["plans"]
    assert lessons[0].firing_id == "fire-1"


def test_redis_provider_reflect_uses_supported_record_fields() -> None:
    calls: list[dict[str, object]] = []

    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "payload": payload})
        return {"status": "ok"}

    provider = RedisAgentMemoryProvider(
        base_url="http://memory.local",
        namespace="alfred",
        user_id="operator",
        transport=transport,
    )

    provider.reflect(
        codename="lucius",
        repo="acme/app",
        body="Prefer a small scoped PR after plan approval.",
        tags=["planning"],
        severity="warning",
        firing_id="fire-2",
    )

    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://memory.local/v1/long-term-memory/"
    payload = calls[0]["payload"]
    assert isinstance(payload, dict)
    assert set(payload) == {"memories", "deduplicate"}
    memory = payload["memories"][0]
    assert memory["namespace"] == "alfred"
    assert memory["user_id"] == "operator"
    assert memory["session_id"] == "fire-2"
    assert memory["entities"] == ["lucius", "acme/app"]
    assert "metadata" not in memory
    assert "codename:lucius" in memory["topics"]
    assert "repo:acme/app" in memory["topics"]
    assert "severity:warning" in memory["topics"]


def test_redis_provider_sync_lesson_mirrors_trusted_lesson() -> None:
    calls: list[dict[str, object]] = []

    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        calls.append({"method": method, "url": url, "payload": payload})
        return {"status": "ok"}

    provider = RedisAgentMemoryProvider(transport=transport)
    lesson = _make_lesson("Redis should receive reviewed lessons", codename="bane", repo="acme/api")

    assert provider.sync_lesson(lesson) is True
    payload = calls[0]["payload"]
    assert isinstance(payload, dict)
    memory = payload["memories"][0]
    assert memory["id"] == lesson.id
    assert memory["text"] == "Redis should receive reviewed lessons"
    assert "codename:bane" in memory["topics"]
    assert "repo:acme/api" in memory["topics"]


def test_redis_provider_reflect_falls_through_on_write_error() -> None:
    def transport(method, url, payload, headers, timeout_s):  # type: ignore[no-untyped-def]
        raise RuntimeError("down")

    provider = RedisAgentMemoryProvider(transport=transport)

    with pytest.raises(NotImplementedError):
        provider.reflect(codename="lucius", repo="acme/app", body="remember this")


def test_build_chain_supports_redis_provider() -> None:
    out = build_chain(
        ["redis", "null"],
        env={"ALFRED_REDIS_MEMORY_URL": "http://memory.local"},
    )

    assert isinstance(out, ChainedMemoryProvider)
    assert [p.name for p in out.providers] == ["redis", "null"]


# ---------------------------------------------------------------------------
# Config parser
# ---------------------------------------------------------------------------


def test_parse_provider_names_basics() -> None:
    assert parse_provider_names("") == []
    assert parse_provider_names(None) == []
    assert parse_provider_names("fleet") == ["fleet"]
    assert parse_provider_names("fleet,gbrain") == ["fleet", "gbrain"]
    # whitespace, case, duplicates
    assert parse_provider_names(" Fleet ,  GBRAIN, fleet") == ["fleet", "gbrain"]


def test_build_chain_single_returns_provider_directly() -> None:
    out = build_chain(["null"], env={})
    assert isinstance(out, NullMemoryProvider)


def test_build_chain_multiple_wraps_in_chained() -> None:
    out = build_chain(["null", "gbrain"], env={})
    assert isinstance(out, ChainedMemoryProvider)
    assert [p.name for p in out.providers] == ["null", "gbrain"]


def test_build_chain_unknown_name_skipped() -> None:
    out = build_chain(["bogus", "null"], env={})
    assert isinstance(out, NullMemoryProvider)


def test_build_chain_empty_returns_null() -> None:
    assert isinstance(build_chain([], env={}), NullMemoryProvider)


def test_load_provider_unset_defaults_to_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_MEMORY_PROVIDERS", raising=False)
    out = load_provider(env={})
    assert isinstance(out, FleetBrainProvider)


def test_load_provider_explicit_empty_is_null() -> None:
    out = load_provider(env={"ALFRED_MEMORY_PROVIDERS": ""})
    assert isinstance(out, NullMemoryProvider)


def test_load_provider_chained_fleet_then_gbrain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {
        "ALFRED_MEMORY_PROVIDERS": "fleet,gbrain",
        "ALFRED_GBRAIN_BIN": "/no/such/binary",
    }
    # Point the fleet at an in-memory store via env-aware factory swap.
    registry = dict(PROVIDER_REGISTRY)
    in_mem = FleetBrain(store=SQLiteStore(db_path=Path(":memory:")))
    registry["fleet"] = lambda _e: FleetBrainProvider(brain=in_mem)
    out = build_chain(["fleet", "gbrain"], env=env, registry=registry)
    assert isinstance(out, ChainedMemoryProvider)
    assert [p.name for p in out.providers] == ["fleet", "gbrain"]


def test_registry_is_open_for_extension() -> None:
    """Adding a new provider is just a new registry entry."""

    class _Custom:
        name = "custom"

        def recall(self, **_: object) -> list[Lesson]:
            return [_make_lesson("custom hit")]

        def reflect(self, **_: object) -> Lesson:
            raise NotImplementedError

    registry = dict(PROVIDER_REGISTRY)
    registry["custom"] = lambda _env: _Custom()
    out = build_chain(["custom"], env={}, registry=registry)
    assert isinstance(out, _Custom)
    assert isinstance(out, MemoryProvider)


# ---------------------------------------------------------------------------
# End-to-end chained-recall worked trace (sanity)
# ---------------------------------------------------------------------------


def test_worked_trace_fleet_then_gbrain(
    tmp_path: Path, fleet_brain_provider: FleetBrainProvider
) -> None:
    """fleet returns lessons -> chain stops there (gbrain not consulted)."""
    fleet_brain_provider.reflect(
        codename="lucius",
        repo="acme-org/api",
        body="fleet-side lesson",
    )
    gbrain = _StaticProvider(name="gbrain", lessons=[_make_lesson("kb fallback")])
    chain = ChainedMemoryProvider(providers=[fleet_brain_provider, gbrain])
    out = chain.recall(codename="lucius", repo="acme-org/api")
    assert [L.body for L in out] == ["fleet-side lesson"]
    assert gbrain.recall_calls == 0


def test_worked_trace_fleet_empty_falls_through_to_gbrain(
    fleet_brain_provider: FleetBrainProvider,
) -> None:
    """fleet returns nothing -> chain consults gbrain."""
    gbrain = _StaticProvider(name="gbrain", lessons=[_make_lesson("kb fallback")])
    chain = ChainedMemoryProvider(providers=[fleet_brain_provider, gbrain])
    out = chain.recall(codename="never", repo="never")
    assert [L.body for L in out] == ["kb fallback"]
    assert gbrain.recall_calls == 1


def test_worked_trace_reflect_routes_to_fleet_when_gbrain_readonly(
    fleet_brain_provider: FleetBrainProvider,
) -> None:
    """reflect skips the read-only gbrain and writes to the fleet."""
    gbrain = GBrainProvider(binary_path=Path("/no/such/binary"))  # read-only
    chain = ChainedMemoryProvider(providers=[gbrain, fleet_brain_provider])
    chain.reflect(
        codename="lucius",
        repo="acme-org/api",
        body="reflected via chain",
    )
    out = fleet_brain_provider.recall(codename="lucius", repo="acme-org/api")
    assert any(L.body == "reflected via chain" for L in out)


def test_os_env_unmodified_by_load_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_provider must not mutate os.environ."""
    monkeypatch.setenv("ALFRED_MEMORY_PROVIDERS", "null")
    snapshot = dict(os.environ)
    load_provider()
    assert dict(os.environ) == snapshot
