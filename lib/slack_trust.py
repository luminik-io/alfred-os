"""Local trust store for Slack-native Alfred collaboration.

Environment variables remain the bootstrap source of truth:
``ALFRED_OPERATOR_SLACK_USER_ID`` names the operator and
``ALFRED_TRUSTED_SLACK_USER_IDS`` names static collaborators. This module adds
one inspectable local layer for collaborators the operator trusts from Slack or
the local client.

Only Slack user ids are stored, never names or message text. The file is
written atomically under ``$ALFRED_HOME/state/slack-trust/trusted-users.json``
so a running listener can pick up collaborator changes without restart.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENV_OPERATOR_USER_ID = "ALFRED_OPERATOR_SLACK_USER_ID"
ENV_TRUSTED_USER_IDS = "ALFRED_TRUSTED_SLACK_USER_IDS"

_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{1,32}$")
_MENTION_RE = re.compile(r"^<@(?P<id>[UW][A-Z0-9]{1,32})(?:\|[^>]+)?>$")


@dataclass(frozen=True)
class LocalTrustedUser:
    user_id: str
    added_at: str
    added_by: str


@dataclass(frozen=True)
class TrustedUserView:
    user_id: str
    sources: tuple[str, ...]
    added_at: str | None = None
    added_by: str | None = None
    can_remove: bool = False


@dataclass(frozen=True)
class TrustedUsersSnapshot:
    operator_user_id: str | None
    users: tuple[TrustedUserView, ...]
    state_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_user_id": self.operator_user_id,
            "users": [
                {
                    **asdict(user),
                    "sources": list(user.sources),
                }
                for user in self.users
            ],
            "state_path": self.state_path,
        }


class SlackTrustStore:
    """Atomically stores operator-added trusted Slack collaborators."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "trusted-users.json"
        self.lock_path = root / "trusted-users.lock"

    @classmethod
    def from_state_root(cls, state_root: Path) -> SlackTrustStore:
        return cls(state_root / "slack-trust")

    def list_local(self) -> tuple[LocalTrustedUser, ...]:
        return self._list_local(strict=False)

    def _list_local(self, *, strict: bool) -> tuple[LocalTrustedUser, ...]:
        payload = self._read_payload(strict=strict)
        raw_users = payload.get("users")
        if not isinstance(raw_users, list):
            return ()
        out: list[LocalTrustedUser] = []
        seen: set[str] = set()
        for item in raw_users:
            if not isinstance(item, dict):
                continue
            user_id = normalize_slack_user_id(item.get("user_id"))
            if user_id is None or user_id in seen:
                continue
            seen.add(user_id)
            out.append(
                LocalTrustedUser(
                    user_id=user_id,
                    added_at=str(item.get("added_at") or ""),
                    added_by=str(item.get("added_by") or ""),
                )
            )
        return tuple(out)

    def add(self, user_id: str, *, added_by: str) -> tuple[bool, LocalTrustedUser]:
        normalized = normalize_slack_user_id(user_id)
        if normalized is None:
            raise ValueError("not a Slack user id")
        actor = normalize_slack_user_id(added_by) or str(added_by or "").strip()
        with self._locked():
            existing = {user.user_id: user for user in self._list_local(strict=True)}
            if normalized in existing:
                return False, existing[normalized]
            user = LocalTrustedUser(
                user_id=normalized,
                added_at=_utc_now(),
                added_by=actor,
            )
            existing[normalized] = user
            self._write_users(existing.values())
            return True, user

    def remove(self, user_id: str) -> bool:
        normalized = normalize_slack_user_id(user_id)
        if normalized is None:
            raise ValueError("not a Slack user id")
        with self._locked():
            before = self._list_local(strict=True)
            users = [user for user in before if user.user_id != normalized]
            changed = len(users) != len(before)
            if changed:
                self._write_users(users)
            return changed

    def snapshot(
        self,
        *,
        operator_user_id: str | None = None,
        env_trusted_user_ids: tuple[str, ...] = (),
    ) -> TrustedUsersSnapshot:
        operator = normalize_slack_user_id(operator_user_id)
        sources: dict[str, set[str]] = {}
        local: dict[str, LocalTrustedUser] = {user.user_id: user for user in self.list_local()}

        if operator:
            sources.setdefault(operator, set()).add("operator")
        for user_id in env_trusted_user_ids:
            normalized = normalize_slack_user_id(user_id)
            if normalized:
                sources.setdefault(normalized, set()).add("env")
        for user_id in local:
            sources.setdefault(user_id, set()).add("local")

        rows: list[TrustedUserView] = []
        for user_id in sorted(sources):
            user_sources = tuple(sorted(sources[user_id], key=_source_sort_key))
            local_user = local.get(user_id)
            rows.append(
                TrustedUserView(
                    user_id=user_id,
                    sources=user_sources,
                    added_at=local_user.added_at if local_user else None,
                    added_by=local_user.added_by if local_user else None,
                    can_remove="local" in user_sources,
                )
            )
        return TrustedUsersSnapshot(
            operator_user_id=operator,
            users=tuple(rows),
            state_path=str(self.path),
        )

    @contextmanager
    def _locked(
        self,
    ) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_payload(self, *, strict: bool = False) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError as exc:
            if strict:
                raise ValueError(f"could not read Slack trust store: {exc}") from exc
            return {}
        except json.JSONDecodeError as exc:
            if strict:
                raise ValueError("Slack trust store is not valid JSON") from exc
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_users(self, users: Any) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": _utc_now(),
            "users": [asdict(user) for user in sorted(users, key=lambda row: row.user_id)],
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)


def normalize_slack_user_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    mention = _MENTION_RE.match(text)
    if mention:
        text = mention.group("id")
    text = text.upper()
    return text if _USER_ID_RE.match(text) else None


def parse_trusted_user_ids(raw: str | None) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\s]+", raw or ""):
        normalized = normalize_slack_user_id(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return tuple(out)


def operator_user_id_from_env() -> str | None:
    return normalize_slack_user_id(os.environ.get(ENV_OPERATOR_USER_ID))


def env_trusted_user_ids() -> tuple[str, ...]:
    return parse_trusted_user_ids(os.environ.get(ENV_TRUSTED_USER_IDS))


def default_state_root() -> Path:
    home = (os.environ.get("ALFRED_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / "state"
    return Path.home() / ".alfred" / "state"


def trusted_user_ids(
    *,
    operator_user_id: str | None = None,
    state_root: Path | None = None,
    static_user_ids: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    operator = normalize_slack_user_id(operator_user_id) or operator_user_id_from_env()
    env_ids = env_trusted_user_ids() if static_user_ids is None else static_user_ids
    store = SlackTrustStore.from_state_root(state_root or default_state_root())
    ids: list[str] = []
    if operator:
        ids.append(operator)
    ids.extend(env_ids)
    ids.extend(user.user_id for user in store.list_local())
    return _dedupe(ids)


def trusted_users_snapshot(
    *,
    operator_user_id: str | None = None,
    state_root: Path | None = None,
) -> TrustedUsersSnapshot:
    operator = normalize_slack_user_id(operator_user_id) or operator_user_id_from_env()
    store = SlackTrustStore.from_state_root(state_root or default_state_root())
    return store.snapshot(
        operator_user_id=operator,
        env_trusted_user_ids=env_trusted_user_ids(),
    )


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = normalize_slack_user_id(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return tuple(out)


def _source_sort_key(source: str) -> int:
    return {"operator": 0, "env": 1, "local": 2}.get(source, 99)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "LocalTrustedUser",
    "SlackTrustStore",
    "TrustedUserView",
    "TrustedUsersSnapshot",
    "default_state_root",
    "env_trusted_user_ids",
    "normalize_slack_user_id",
    "operator_user_id_from_env",
    "parse_trusted_user_ids",
    "trusted_user_ids",
    "trusted_users_snapshot",
]
