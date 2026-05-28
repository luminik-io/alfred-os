"""Unit tests for ``lib/slack_approval.py``.

The Slack API is never touched. A ``FakeSlackClient`` returns canned
reaction payloads, and the wall-clock seams (``_now``, ``_sleep``) are
replaced with deterministic stand-ins so the suite runs in milliseconds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_approval import (  # noqa: E402
    APPROVAL_GRANTED,
    APPROVAL_REJECTED,
    APPROVAL_TIMEOUT,
    APPROVAL_TRANSPORT_DOWN,
    TRANSPORT_FAIL_THRESHOLD,
    SlackApproval,
    aws_secrets_token_resolver,
    env_token_resolver,
    file_cache_token_resolver,
    operator_user_id_from_env,
    resolve_bot_token,
)

OPERATOR = "U0123ABCDEF"
TEAMMATE = "U999ZZZZZZZ"
CHANNEL = "C01234567"
TS = "1716480000.123456"


# ---------- Fakes ----------


class FakeSlackClient:
    """Deterministic stand-in for ``slack_sdk.WebClient``.

    ``responses`` is a list of dicts (or ``Exception`` instances). Each
    call to ``reactions_get`` consumes the next entry. The last entry is
    repeated indefinitely if the caller polls past the supplied script."""

    def __init__(
        self, responses: list[Any], *, replies: list[dict[str, Any]] | None = None
    ) -> None:
        if not responses:
            raise ValueError("FakeSlackClient needs at least one scripted response")
        self._responses = responses
        self._replies = replies or []
        self.calls: list[dict[str, Any]] = []
        self.reply_calls: list[dict[str, Any]] = []

    def reactions_get(self, *, channel: str, timestamp: str, full: bool = True) -> Any:
        self.calls.append({"channel": channel, "timestamp": timestamp, "full": full})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[idx]
        if isinstance(item, Exception):
            raise item
        return item

    def conversations_replies(self, *, channel: str, ts: str, limit: int = 100) -> Any:
        self.reply_calls.append({"channel": channel, "ts": ts, "limit": limit})
        return {"ok": True, "messages": self._replies}


def _ok(reactions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": True, "message": {"reactions": reactions}}


def _fail(error: str = "channel_not_found") -> dict[str, Any]:
    return {"ok": False, "error": error}


class _Clock:
    """Deterministic ``time.time`` substitute that advances on ``sleep``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


# ---------- SlackApproval: happy path + auth gates ----------


def test_constructor_requires_operator_user_id() -> None:
    with pytest.raises(ValueError):
        SlackApproval(FakeSlackClient([_ok([])]), operator_user_id="")


def test_operator_approves_returns_granted() -> None:
    client = FakeSlackClient(
        [
            _ok([]),
            _ok([{"name": "white_check_mark", "users": [OPERATOR], "count": 1}]),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=600,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_GRANTED
    assert result.approved is True
    assert result.reactor == OPERATOR
    assert len(client.calls) == 2


def test_operator_thread_replies_return_as_feedback() -> None:
    client = FakeSlackClient(
        [_ok([{"name": "white_check_mark", "users": [OPERATOR], "count": 1}])],
        replies=[
            {"ts": TS, "user": OPERATOR, "text": "root message ignored"},
            {"ts": "1716480001.000001", "user": TEAMMATE, "text": "not the operator"},
            {
                "ts": "1716480002.000002",
                "user": OPERATOR,
                "text": "Please keep copy simple and add the mobile case.",
            },
        ],
    )
    clock = _Clock()

    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=60,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )

    assert result.verdict == APPROVAL_GRANTED
    assert [item.text for item in result.feedback] == [
        "Please keep copy simple and add the mobile case."
    ]
    assert client.reply_calls == [{"channel": CHANNEL, "ts": TS, "limit": 100}]


def test_operator_thread_replies_trigger_feedback_callback_once() -> None:
    client = FakeSlackClient(
        [
            _ok([]),
            _ok([]),
            _ok([{"name": "white_check_mark", "users": [OPERATOR], "count": 1}]),
        ],
        replies=[
            {"ts": TS, "user": OPERATOR, "text": "root message ignored"},
            {
                "ts": "1716480002.000002",
                "user": OPERATOR,
                "text": "remove repo: web\nadd repo: mobile",
            },
        ],
    )
    clock = _Clock()
    callbacks: list[list[str]] = []

    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=60,
        poll_interval_s=10,
        feedback_callback=lambda items: callbacks.append([item.text for item in items]),
        _now=clock.now,
        _sleep=clock.sleep,
    )

    assert result.verdict == APPROVAL_GRANTED
    assert callbacks == [["remove repo: web\nadd repo: mobile"]]


def test_non_operator_reaction_is_ignored_until_timeout() -> None:
    client = FakeSlackClient(
        [
            _ok([{"name": "white_check_mark", "users": [TEAMMATE], "count": 1}]),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=30,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_TIMEOUT
    assert result.approved is False
    assert result.reactor is None


def test_operator_rejects_returns_rejected() -> None:
    client = FakeSlackClient(
        [
            _ok([{"name": "x", "users": [OPERATOR], "count": 1}]),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=60,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_REJECTED
    assert result.rejected is True
    assert result.reactor == OPERATOR


def test_skin_tone_variants_match_bare_name() -> None:
    client = FakeSlackClient(
        [
            _ok([{"name": "thumbsup::skin-tone-4", "users": [OPERATOR], "count": 1}]),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=60,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_GRANTED


def test_timeout_returns_timeout_verdict() -> None:
    client = FakeSlackClient([_ok([])])
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=5,
        poll_interval_s=2,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_TIMEOUT
    assert result.elapsed_s >= 5


def test_transport_failures_surface_after_threshold() -> None:
    # First N polls fail; the gate must surface APPROVAL_TRANSPORT_DOWN
    # rather than masking a rotated token.
    failures = [_fail("not_authed") for _ in range(TRANSPORT_FAIL_THRESHOLD)]
    client = FakeSlackClient(failures)
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=10_000,
        poll_interval_s=1,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_TRANSPORT_DOWN
    assert "consecutive" in result.detail


def test_transient_blip_recovers() -> None:
    # One failure, then operator approves. The gate must not give up.
    client = FakeSlackClient(
        [
            _fail("ratelimited"),
            _ok([{"name": "white_check_mark", "users": [OPERATOR], "count": 1}]),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=10_000,
        poll_interval_s=1,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_GRANTED


def test_sdk_exception_counts_as_transport_failure() -> None:
    class BoomError(Exception):
        pass

    client = FakeSlackClient([BoomError("network down")] * TRANSPORT_FAIL_THRESHOLD)
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=10_000,
        poll_interval_s=1,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_TRANSPORT_DOWN


def test_kill_check_short_circuits_with_rejected_killed() -> None:
    client = FakeSlackClient([_ok([])])
    clock = _Clock()

    calls = {"n": 0}

    def kill() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # trip on the 2nd poll

    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=10_000,
        poll_interval_s=1,
        kill_check=kill,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_REJECTED
    assert result.detail == "killed"


def test_response_with_data_attribute_is_handled() -> None:
    """slack_sdk's ``SlackResponse`` exposes ``.data``; the gate must
    cope without an adapter."""

    class SlackResponseLike:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

    client = FakeSlackClient(
        [
            SlackResponseLike(_ok([{"name": "+1", "users": [OPERATOR], "count": 1}])),
        ]
    )
    clock = _Clock()
    result = SlackApproval(client, OPERATOR).await_approval(
        CHANNEL,
        TS,
        timeout_s=60,
        poll_interval_s=10,
        _now=clock.now,
        _sleep=clock.sleep,
    )
    assert result.verdict == APPROVAL_GRANTED


# ---------- Token resolver chain ----------


def test_env_resolver_returns_value_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-1234")
    assert env_token_resolver() == "xoxb-test-1234"


def test_env_resolver_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert env_token_resolver() is None


def test_aws_resolver_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_SECRETS_BACKEND", raising=False)
    assert aws_secrets_token_resolver() is None


def test_aws_resolver_uses_injected_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_SECRETS_BACKEND", "aws")

    class FakeSM:
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
            assert SecretId == "alfred/slack-bot-token"
            return {"SecretString": "xoxb-aws-resolved"}

    class FakeBoto3:
        @staticmethod
        def client(name: str, region_name: str = "") -> FakeSM:
            assert name == "secretsmanager"
            return FakeSM()

    assert aws_secrets_token_resolver(boto3_module=FakeBoto3) == "xoxb-aws-resolved"


def test_aws_resolver_returns_none_on_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_SECRETS_BACKEND", "aws")

    class FakeSM:
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
            raise RuntimeError("ResourceNotFoundException")

    class FakeBoto3:
        @staticmethod
        def client(name: str, region_name: str = "") -> FakeSM:
            return FakeSM()

    assert aws_secrets_token_resolver(boto3_module=FakeBoto3) is None


def test_aws_resolver_does_not_log_secret_lookup_details(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("ALFRED_SECRETS_BACKEND", "aws")

    class FakeSM:
        def get_secret_value(self, *, SecretId: str) -> dict[str, Any]:
            raise RuntimeError(f"lookup failed for {SecretId} with xoxb-sensitive")

    class FakeBoto3:
        @staticmethod
        def client(name: str, region_name: str = "") -> FakeSM:
            return FakeSM()

    with caplog.at_level("WARNING", logger="alfred.slack_approval"):
        assert aws_secrets_token_resolver(boto3_module=FakeBoto3) is None

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "configured Slack bot token secret" in logged
    assert "alfred/slack-bot-token" not in logged
    assert "xoxb-sensitive" not in logged


def test_file_cache_resolver_reads_token(tmp_path: Path) -> None:
    cache = tmp_path / "slack-bot-token.cache"
    cache.write_text("xoxb-from-disk\n")
    assert file_cache_token_resolver(cache_path=cache) == "xoxb-from-disk"


def test_file_cache_resolver_handles_missing_file(tmp_path: Path) -> None:
    cache = tmp_path / "missing.cache"
    assert file_cache_token_resolver(cache_path=cache) is None


def test_resolve_bot_token_walks_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    calls: list[str] = []

    def r1() -> str | None:
        calls.append("r1")
        return None

    def r2() -> str | None:
        calls.append("r2")
        return "xoxb-from-r2"

    def r3() -> str | None:
        calls.append("r3")
        return "xoxb-should-not-reach"

    assert resolve_bot_token(resolvers=[r1, r2, r3]) == "xoxb-from-r2"
    assert calls == ["r1", "r2"]  # r3 must not be called once r2 wins


def test_resolve_bot_token_returns_none_when_all_miss() -> None:
    assert resolve_bot_token(resolvers=[lambda: None, lambda: None]) is None


def test_resolver_that_raises_does_not_abort_chain() -> None:
    def boom() -> str | None:
        raise RuntimeError("flaky")

    assert resolve_bot_token(resolvers=[boom, lambda: "xoxb-ok"]) == "xoxb-ok"


# ---------- Env helpers ----------


def test_operator_user_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALFRED_OPERATOR_SLACK_USER_ID", "U0123ABCDEF")
    assert operator_user_id_from_env() == "U0123ABCDEF"


def test_operator_user_id_from_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALFRED_OPERATOR_SLACK_USER_ID", raising=False)
    assert operator_user_id_from_env() is None
