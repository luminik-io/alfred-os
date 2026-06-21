"""Bootstrap config for the local Redis Agent Memory Server.

``memory.redis_agent_memory`` is the client used by Alfred firings. This
module owns the server-side defaults shared by the launcher, doctor checks,
and docs: one loopback host, one port, one Redis URL, one embedding model.

The defaults are local-only and free to run: Redis Stack for storage/search,
Ollama for embeddings, and no cloud API key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "AMS_DEFAULTS",
    "AmsServerConfig",
    "ams_server_env",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
DEFAULT_AUTH_MODE = "disabled"
DEFAULT_EMBEDDING_MODEL = "ollama/mxbai-embed-large"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
DEFAULT_GENERATION_MODEL = "ollama/llama3.2:1b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_LONG_TERM_MEMORY = True
DEFAULT_COMPACTION_INTERVAL_SECONDS = 600
DEFAULT_FORGETTING_ENABLED = False

AMS_DEFAULTS: dict[str, str | int | bool] = {
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "redis_url": DEFAULT_REDIS_URL,
    "auth_mode": DEFAULT_AUTH_MODE,
    "embedding_model": DEFAULT_EMBEDDING_MODEL,
    "embedding_dimensions": DEFAULT_EMBEDDING_DIMENSIONS,
    "generation_model": DEFAULT_GENERATION_MODEL,
    "ollama_base_url": DEFAULT_OLLAMA_BASE_URL,
    "long_term_memory": DEFAULT_LONG_TERM_MEMORY,
    "compaction_interval_seconds": DEFAULT_COMPACTION_INTERVAL_SECONDS,
    "forgetting_enabled": DEFAULT_FORGETTING_ENABLED,
}


@dataclass(frozen=True)
class AmsServerConfig:
    """Resolved config for running the Redis Agent Memory Server locally."""

    host: str = field(default=DEFAULT_HOST)
    port: int = field(default=DEFAULT_PORT)
    redis_url: str = field(default=DEFAULT_REDIS_URL)
    auth_mode: str = field(default=DEFAULT_AUTH_MODE)
    token: str | None = None
    embedding_model: str = field(default=DEFAULT_EMBEDDING_MODEL)
    embedding_dimensions: int = field(default=DEFAULT_EMBEDDING_DIMENSIONS)
    generation_model: str = field(default=DEFAULT_GENERATION_MODEL)
    ollama_base_url: str = field(default=DEFAULT_OLLAMA_BASE_URL)
    long_term_memory: bool = field(default=DEFAULT_LONG_TERM_MEMORY)
    compaction_interval_seconds: int = field(default=DEFAULT_COMPACTION_INTERVAL_SECONDS)
    forgetting_enabled: bool = field(default=DEFAULT_FORGETTING_ENABLED)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/v1/health"

    @classmethod
    def from_env(cls, *, env: Mapping[str, str] | None = None) -> AmsServerConfig:
        envmap = env if env is not None else os.environ
        token = (envmap.get("ALFRED_AMS_TOKEN") or "").strip() or None
        return cls(
            host=(envmap.get("ALFRED_AMS_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST,
            port=_int_env(envmap, "ALFRED_AMS_PORT", DEFAULT_PORT),
            redis_url=(envmap.get("ALFRED_AMS_REDIS_URL") or DEFAULT_REDIS_URL).strip()
            or DEFAULT_REDIS_URL,
            auth_mode=(envmap.get("ALFRED_AMS_AUTH_MODE") or DEFAULT_AUTH_MODE).strip().lower()
            or DEFAULT_AUTH_MODE,
            token=token,
            embedding_model=(
                envmap.get("ALFRED_AMS_EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
            ).strip()
            or DEFAULT_EMBEDDING_MODEL,
            embedding_dimensions=_int_env(
                envmap,
                "ALFRED_AMS_EMBEDDING_DIM",
                DEFAULT_EMBEDDING_DIMENSIONS,
            ),
            generation_model=(
                envmap.get("ALFRED_AMS_GENERATION_MODEL") or DEFAULT_GENERATION_MODEL
            ).strip()
            or DEFAULT_GENERATION_MODEL,
            ollama_base_url=(
                envmap.get("ALFRED_AMS_OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL
            ).strip()
            or DEFAULT_OLLAMA_BASE_URL,
            long_term_memory=_bool_env(
                envmap,
                "ALFRED_AMS_LONG_TERM_MEMORY",
                DEFAULT_LONG_TERM_MEMORY,
            ),
            compaction_interval_seconds=_int_env(
                envmap,
                "ALFRED_AMS_COMPACTION_INTERVAL_S",
                DEFAULT_COMPACTION_INTERVAL_SECONDS,
            ),
            forgetting_enabled=_bool_env(
                envmap,
                "ALFRED_AMS_FORGETTING",
                DEFAULT_FORGETTING_ENABLED,
            ),
        )

    def to_server_env(self) -> dict[str, str]:
        """Return env vars consumed by the upstream server process."""
        out = {
            "REDIS_URL": self.redis_url,
            "AUTH_MODE": self.auth_mode,
            "DISABLE_AUTH": _as_str_bool(self.auth_mode == "disabled"),
            "LONG_TERM_MEMORY": _as_str_bool(self.long_term_memory),
            "EMBEDDING_MODEL": self.embedding_model,
            "REDISVL_VECTOR_DIMENSIONS": str(self.embedding_dimensions),
            "EMBEDDING_DIMENSIONS": str(self.embedding_dimensions),
            "GENERATION_MODEL": self.generation_model,
            "FAST_MODEL": self.generation_model,
            "SLOW_MODEL": self.generation_model,
            "INDEX_ALL_MESSAGES_IN_LONG_TERM_MEMORY": "false",
            "FORGETTING_ENABLED": _as_str_bool(self.forgetting_enabled),
            "COMPACTION_EVERY_MINUTES": str(max(1, self.compaction_interval_seconds // 60)),
            "OLLAMA_API_BASE": self.ollama_base_url,
            "OLLAMA_BASE_URL": self.ollama_base_url,
        }
        return out


def ams_server_env(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    return AmsServerConfig.from_env(env=env).to_server_env()


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool_env(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _as_str_bool(value: bool) -> str:
    return "true" if value else "false"
