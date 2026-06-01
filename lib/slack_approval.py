"""Slack reaction-based plan approval gate for Alfred agents.

This is the engine behind Alfred's plan-mode approval story: an agent
posts its plan to a Slack channel, the operator reacts with a configured
emoji (``white_check_mark`` by default), and the agent proceeds. Any
reaction from a user that is not the configured operator is ignored, so a
teammate cannot accidentally green-light a plan.

The module is intentionally narrow:

- One operator, one plan, one reaction. No multi-approver workflows.
- Reaction-based by default. Reply-based approval is not in scope; the
  cost of parsing free-text replies (and the corresponding mis-approval
  risk) outweighs the convenience.
- Token resolution is a chain of pluggable strategies: env var, AWS
  Secrets Manager (opt-in), file cache. New strategies plug in by
  passing a callable into ``SlackApproval`` or by appending to
  ``default_token_resolvers()``.
- The Slack client is behind a ``SlackClient`` ``Protocol`` so tests can
  inject a fake without touching the network. The default implementation
  wraps ``slack_sdk.WebClient`` and is loaded lazily; ``slack-sdk`` is an
  optional dependency declared under the ``[slack]`` extra in
  ``pyproject.toml``.

Configuration is via env vars (12-factor):

================================  =================================================
``ALFRED_OPERATOR_SLACK_USER_ID``  Slack user id of the only person whose reactions
                                  count, e.g. ``U0123ABCDEF``. Required.
``ALFRED_TRUSTED_SLACK_USER_IDS``  Optional comma-separated Slack user ids whose
                                  thread replies can amend a plan. Their
                                  reactions still do not approve or reject.
``SLACK_BOT_TOKEN``                Slack bot token (``xoxb-...``). Used directly when
                                  set; falls through to the next strategy otherwise.
``ALFRED_SECRETS_BACKEND``         Set to ``aws`` to enable the AWS Secrets Manager
                                  token resolver. Any other value disables it.
``ALFRED_SLACK_BOT_TOKEN_SECRET_ID``
                                  Secret id read by the AWS resolver. Defaults to
                                  ``alfred/slack-bot-token``.
``ALFRED_SLACK_BOT_TOKEN_SECRET_REGION``
                                  AWS region of the secret. Defaults to
                                  ``us-east-1``.
``ALFRED_SLACK_BOT_TOKEN_CACHE``   Path to the on-disk token cache. Defaults to
                                  ``$ALFRED_HOME/state/slack-bot-token.cache`` if
                                  ``ALFRED_HOME`` is set, otherwise unused.
================================  =================================================

Sample usage::

    from slack_approval import SlackApproval, default_slack_client

    client = default_slack_client()  # raises ImportError if slack-sdk is missing
    gate = SlackApproval(client, operator_user_id="U0123ABCDEF")
    result = gate.await_approval(
        channel="alfred",
        message_ts="1716480000.123456",
        timeout_s=900,
    )
    if result.approved:
        proceed()
    else:
        abort(result.verdict)

See ``docs/SLACK_APPROVAL.md`` for the full walkthrough.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from labels import LABEL_AGENT_PLAN_PENDING_APPROVAL

logger = logging.getLogger("alfred.slack_approval")

# ---------- Public verdicts (strings, JSON-friendly) ----------

APPROVAL_GRANTED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_TIMEOUT = "timeout"
APPROVAL_TRANSPORT_DOWN = "transport-unavailable"

# After this many consecutive ``reactions.get`` failures the gate gives up
# and surfaces ``APPROVAL_TRANSPORT_DOWN``. At the default 30s poll
# interval this is ~2.5 minutes, enough to ride out a transient blip but
# short enough to alert on a rotated token or removed scope before the
# wall-clock timeout.
TRANSPORT_FAIL_THRESHOLD = 5

# Default emoji sets. ``white_check_mark`` is the canonical "I approve"
# reaction. ``x`` is the canonical "I reject" reaction. Skin-tone
# variants (e.g. ``thumbsup::skin-tone-2``) match the bare name.
DEFAULT_APPROVE_EMOJIS: tuple[str, ...] = ("white_check_mark", "thumbsup", "+1")
DEFAULT_REJECT_EMOJIS: tuple[str, ...] = ("x", "thumbsdown", "-1")

# Env var names
ENV_OPERATOR_USER_ID = "ALFRED_OPERATOR_SLACK_USER_ID"
ENV_TRUSTED_FEEDBACK_USER_IDS = "ALFRED_TRUSTED_SLACK_USER_IDS"
ENV_BOT_TOKEN = "SLACK_BOT_TOKEN"
ENV_SECRETS_BACKEND = "ALFRED_SECRETS_BACKEND"
ENV_SECRET_ID = "ALFRED_SLACK_BOT_TOKEN_SECRET_ID"
ENV_SECRET_REGION = "ALFRED_SLACK_BOT_TOKEN_SECRET_REGION"
ENV_TOKEN_CACHE = "ALFRED_SLACK_BOT_TOKEN_CACHE"
ENV_ALFRED_HOME = "ALFRED_HOME"

DEFAULT_SECRET_ID = "alfred/slack-bot-token"
DEFAULT_SECRET_REGION = "us-east-1"
TOKEN_CACHE_TTL_S = 30 * 24 * 3600  # 30 days


# ---------- Dataclasses ----------


@dataclass(frozen=True)
class ApprovalRequest:
    """Inputs for one approval cycle."""

    channel: str
    message_ts: str
    timeout_s: int = 900
    poll_interval_s: int = 30
    approve_emojis: tuple[str, ...] = DEFAULT_APPROVE_EMOJIS
    reject_emojis: tuple[str, ...] = DEFAULT_REJECT_EMOJIS


@dataclass(frozen=True)
class ThreadFeedback:
    """Operator-authored text captured from the approval thread."""

    author: str
    text: str
    ts: str


@dataclass(frozen=True)
class ApprovalResult:
    """Outcome of one approval cycle."""

    verdict: str
    reactor: str | None = None
    elapsed_s: float = 0.0
    detail: str = ""
    label_hint: str = field(default=LABEL_AGENT_PLAN_PENDING_APPROVAL)
    feedback: tuple[ThreadFeedback, ...] = ()

    @property
    def approved(self) -> bool:
        return self.verdict == APPROVAL_GRANTED

    @property
    def rejected(self) -> bool:
        return self.verdict == APPROVAL_REJECTED


# ---------- Protocols (dependency inversion) ----------


@runtime_checkable
class SlackClient(Protocol):
    """Subset of the Slack Web API this module needs.

    Anything implementing ``reactions_get`` (and optionally
    ``chat_postMessage``) satisfies the contract. The default
    implementation in ``_SlackSdkClient`` wraps ``slack_sdk.WebClient``,
    which already has these method names, so it's compatible without an
    adapter."""

    def reactions_get(self, *, channel: str, timestamp: str, full: bool = True) -> Any:
        """Return reaction data for one message.

        Must return an object with dict-like access. ``slack_sdk``'s
        ``SlackResponse`` satisfies this. Tests can return a plain
        ``dict`` shaped like a Slack API response."""
        ...


@runtime_checkable
class SecretsResolver(Protocol):
    """A pluggable token-resolution strategy.

    Each resolver returns the bot token string, or ``None`` to fall
    through to the next strategy in the chain. Resolvers must never
    raise on a miss; raising aborts the whole chain."""

    def __call__(self) -> str | None: ...


# ---------- Token resolvers ----------


def env_token_resolver() -> str | None:
    """Strategy 1: read the bot token from the env var."""
    tok = (os.environ.get(ENV_BOT_TOKEN) or "").strip()
    return tok or None


def aws_secrets_token_resolver(
    secret_id: str | None = None,
    region: str | None = None,
    *,
    backend_env: str = ENV_SECRETS_BACKEND,
    boto3_module: Any = None,
) -> str | None:
    """Strategy 2 (opt-in): read the bot token from AWS Secrets Manager.

    Disabled by default. Set ``ALFRED_SECRETS_BACKEND=aws`` to enable.
    ``boto3`` is an optional dependency declared under the ``[aws]``
    extra; if the gate is configured to use AWS but ``boto3`` is not
    installed, this resolver logs a warning and returns ``None`` (the
    chain continues; the gate fails loud only if every strategy misses).
    """
    if (os.environ.get(backend_env) or "").strip().lower() != "aws":
        return None
    sid = secret_id or os.environ.get(ENV_SECRET_ID) or DEFAULT_SECRET_ID
    reg = region or os.environ.get(ENV_SECRET_REGION) or DEFAULT_SECRET_REGION
    boto3 = boto3_module
    if boto3 is None:
        try:
            boto3 = importlib.import_module("boto3")
        except ImportError:
            logger.warning(
                "ALFRED_SECRETS_BACKEND=aws but boto3 is not installed; "
                "install alfred-os[aws] to enable. Falling through."
            )
            return None
    try:
        sm = boto3.client("secretsmanager", region_name=reg)
        resp = sm.get_secret_value(SecretId=sid)
    except Exception:
        logger.warning(
            "AWS Secrets Manager lookup failed for the configured Slack bot token secret."
        )
        return None
    val = (resp.get("SecretString") or "").strip()
    return val or None


def file_cache_token_resolver(
    cache_path: Path | None = None,
    *,
    ttl_s: int = TOKEN_CACHE_TTL_S,
    now: Callable[[], float] = time.time,
) -> str | None:
    """Strategy 3 (last resort): read a previously-cached token from disk.

    The cache is intentionally stale-tolerant: if AWS is briefly down we
    would rather use a possibly-rotated token (and let the API reject it,
    surfacing ``transport-unavailable``) than block the firing entirely.
    """
    path = cache_path or _default_cache_path()
    if path is None or not path.exists():
        return None
    try:
        age = now() - path.stat().st_mtime
    except OSError:
        return None
    # Fresh-ttl miss is tolerated up to 2x the TTL so callers that are happy
    # to use a stale token (e.g. retry during an AWS outage) still get a
    # value; callers that want a hard TTL pass ``ttl_s=0``.
    if ttl_s > 0 and age > ttl_s * 2:
        return None
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _default_cache_path() -> Path | None:
    """The disk cache path, if ``ALFRED_HOME`` or an explicit override is set."""
    explicit = (os.environ.get(ENV_TOKEN_CACHE) or "").strip()
    if explicit:
        return Path(explicit)
    home = (os.environ.get(ENV_ALFRED_HOME) or "").strip()
    if home:
        return Path(home) / "state" / "slack-bot-token.cache"
    return None


def default_token_resolvers() -> list[SecretsResolver]:
    """The standard env -> AWS -> file-cache chain."""
    return [env_token_resolver, aws_secrets_token_resolver, file_cache_token_resolver]


def resolve_bot_token(
    resolvers: Iterable[SecretsResolver] | None = None,
) -> str | None:
    """Walk the resolver chain and return the first non-empty token."""
    chain = list(resolvers) if resolvers is not None else default_token_resolvers()
    for resolver in chain:
        try:
            tok = resolver()
        except Exception as e:
            logger.warning(
                "token resolver %s raised: %s", getattr(resolver, "__name__", resolver), e
            )
            continue
        if tok:
            return tok
    return None


# ---------- Default SlackClient (slack-sdk) ----------


def default_slack_client(token: str | None = None) -> SlackClient:
    """Build the default ``slack_sdk.WebClient``-backed client.

    Resolves the bot token via the standard chain if one is not passed
    explicitly. Raises ``ImportError`` with an actionable message if
    ``slack-sdk`` is not installed, and ``RuntimeError`` if no token can
    be resolved (so the gate fails loud at startup rather than silently
    polling forever)."""
    try:
        from slack_sdk import WebClient
    except ImportError as e:
        raise ImportError(
            "slack-sdk is not installed but the Slack approval gate was "
            "requested. Install with `pip install alfred-os[slack]` or "
            "`pip install slack-sdk`."
        ) from e
    resolved = token or resolve_bot_token()
    if not resolved:
        raise RuntimeError(
            "No Slack bot token available. Set SLACK_BOT_TOKEN, configure "
            "ALFRED_SECRETS_BACKEND=aws with ALFRED_SLACK_BOT_TOKEN_SECRET_ID, "
            "or pre-populate the cache at "
            f"$ALFRED_HOME/state/slack-bot-token.cache (or {ENV_TOKEN_CACHE})."
        )
    return WebClient(token=resolved)


# ---------- The gate ----------


def operator_user_id_from_env() -> str | None:
    """Read the configured operator Slack user id, if any."""
    val = (os.environ.get(ENV_OPERATOR_USER_ID) or "").strip()
    return val or None


def trusted_feedback_user_ids_from_env(
    operator_user_id: str | None = None,
    state_root: Path | None = None,
) -> tuple[str, ...]:
    """Read trusted Slack users whose thread replies can amend a plan."""
    try:
        from slack_trust import trusted_user_ids

        return trusted_user_ids(operator_user_id=operator_user_id, state_root=state_root)
    except Exception as exc:
        logger.warning(
            "Slack trusted user store unavailable; falling back to env trusted users: %s",
            exc,
        )
        raw = (os.environ.get(ENV_TRUSTED_FEEDBACK_USER_IDS) or "").strip()
        ids = [operator_user_id.strip()] if operator_user_id and operator_user_id.strip() else []
        for item in re.split(r"[,;\s]+", raw):
            cleaned = item.strip()
            if cleaned:
                ids.append(cleaned)
        return tuple(_dedupe_user_ids(ids))


class SlackApproval:
    """Reaction-based plan approval gate.

    Construct once per agent with the Slack client and the operator user
    id, then call :meth:`await_approval` per plan. The instance is
    stateless across calls; sharing one across many plan posts is fine."""

    def __init__(
        self,
        client: SlackClient,
        operator_user_id: str,
        *,
        approve_emojis: tuple[str, ...] = DEFAULT_APPROVE_EMOJIS,
        reject_emojis: tuple[str, ...] = DEFAULT_REJECT_EMOJIS,
        feedback_user_ids: Iterable[str] | None = None,
        transport_fail_threshold: int = TRANSPORT_FAIL_THRESHOLD,
    ) -> None:
        if not operator_user_id:
            raise ValueError(
                "operator_user_id is required. Approvals from "
                "unconfigured operators are never silently accepted."
            )
        self._client = client
        self._operator_user_id = operator_user_id
        trusted_feedback = (
            tuple(feedback_user_ids)
            if feedback_user_ids is not None
            else trusted_feedback_user_ids_from_env(operator_user_id)
        )
        self._feedback_user_ids = frozenset(_dedupe_user_ids(trusted_feedback))
        self._approve_emojis = approve_emojis
        self._reject_emojis = reject_emojis
        self._transport_fail_threshold = transport_fail_threshold

    @property
    def operator_user_id(self) -> str:
        return self._operator_user_id

    def await_approval(
        self,
        channel: str,
        message_ts: str,
        *,
        timeout_s: int = 900,
        poll_interval_s: int = 30,
        kill_check: Callable[[], bool] | None = None,
        feedback_callback: Callable[[tuple[ThreadFeedback, ...]], None] | None = None,
        _now: Callable[[], float] = time.time,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> ApprovalResult:
        """Poll the message's reactions until the operator approves,
        rejects, or the wall-clock timeout expires.

        ``kill_check`` is invoked on every iteration; truthy returns
        abort the poll with ``APPROVAL_REJECTED`` and ``detail="killed"``
        so a caller can pause mid-wait without losing the firing.

        ``_now`` and ``_sleep`` are unit-test seams. Production callers
        leave them at their defaults."""
        request = ApprovalRequest(
            channel=channel,
            message_ts=message_ts,
            timeout_s=timeout_s,
            poll_interval_s=max(1, poll_interval_s),
            approve_emojis=self._approve_emojis,
            reject_emojis=self._reject_emojis,
        )
        start = _now()
        deadline = start + timeout_s
        consecutive_failures = 0
        seen_feedback_ts: set[str] = set()
        logger.info(
            "approval poll starting: channel=%s ts=%s operator=%s timeout_s=%d",
            channel,
            message_ts,
            self._operator_user_id,
            timeout_s,
        )
        while True:
            current_feedback: tuple[ThreadFeedback, ...] | None = None
            if kill_check is not None:
                try:
                    if kill_check():
                        return ApprovalResult(
                            verdict=APPROVAL_REJECTED,
                            elapsed_s=_now() - start,
                            detail="killed",
                        )
                except Exception as e:
                    logger.warning("kill_check raised, treating as no-kill: %s", e)
            try:
                reactions = self._fetch_reactions(request)
            except _TransportError as e:
                consecutive_failures += 1
                logger.warning(
                    "reactions.get failure %d/%d: %s",
                    consecutive_failures,
                    self._transport_fail_threshold,
                    e,
                )
                if consecutive_failures >= self._transport_fail_threshold:
                    return ApprovalResult(
                        verdict=APPROVAL_TRANSPORT_DOWN,
                        elapsed_s=_now() - start,
                        detail=(
                            f"reactions.get failed {consecutive_failures} consecutive "
                            f"polls; check the bot token, the reactions:read scope, "
                            f"and that the plan message still exists"
                        ),
                    )
            else:
                consecutive_failures = 0
                if feedback_callback is not None:
                    current_feedback = self._fetch_thread_feedback(request)
                    new_feedback = tuple(
                        item
                        for item in current_feedback
                        if item.ts and item.ts not in seen_feedback_ts
                    )
                    if new_feedback:
                        seen_feedback_ts.update(item.ts for item in new_feedback if item.ts)
                        try:
                            feedback_callback(new_feedback)
                        except Exception as exc:
                            logger.warning("plan feedback callback failed: %s", exc)
                if self._operator_reacted_with(reactions, self._approve_emojis):
                    if current_feedback is None:
                        current_feedback = self._fetch_thread_feedback(request)
                    return ApprovalResult(
                        verdict=APPROVAL_GRANTED,
                        reactor=self._operator_user_id,
                        elapsed_s=_now() - start,
                        feedback=current_feedback,
                    )
                if self._operator_reacted_with(reactions, self._reject_emojis):
                    if current_feedback is None:
                        current_feedback = self._fetch_thread_feedback(request)
                    return ApprovalResult(
                        verdict=APPROVAL_REJECTED,
                        reactor=self._operator_user_id,
                        elapsed_s=_now() - start,
                        feedback=current_feedback,
                    )
            now = _now()
            if now >= deadline:
                return ApprovalResult(
                    verdict=APPROVAL_TIMEOUT,
                    elapsed_s=now - start,
                )
            # Never oversleep past the deadline.
            _sleep(float(min(request.poll_interval_s, max(1, int(deadline - now)))))

    # ---------- internals ----------

    def _fetch_reactions(self, request: ApprovalRequest) -> list[dict[str, Any]]:
        """Call ``reactions.get`` and normalise the response.

        Returns the list of reactions, or an empty list when there are
        none. Raises ``_TransportError`` on any API failure so callers
        can count consecutive errors uniformly."""
        try:
            resp = self._client.reactions_get(
                channel=request.channel,
                timestamp=request.message_ts,
                full=True,
            )
        except Exception as e:
            raise _TransportError(f"{type(e).__name__}: {e}") from e
        data = _as_mapping(resp)
        if not data.get("ok", False):
            raise _TransportError(f"slack api not-ok: {data.get('error') or 'unknown'}")
        message = data.get("message") or {}
        return list(message.get("reactions") or [])

    def _operator_reacted_with(
        self,
        reactions: list[dict[str, Any]],
        emoji_names: tuple[str, ...],
    ) -> bool:
        """True iff the configured operator user reacted with any of the
        listed emoji.

        Slack returns reactions like
        ``[{"name": "thumbsup", "users": ["U..."], "count": 1}]``. Skin
        tone variants (``thumbsup::skin-tone-2``) match the bare name."""
        for entry in reactions:
            name = (entry.get("name") or "").split("::", 1)[0]
            if name not in emoji_names:
                continue
            users = entry.get("users") or []
            if self._operator_user_id in users:
                return True
        return False

    def _fetch_thread_feedback(self, request: ApprovalRequest) -> tuple[ThreadFeedback, ...]:
        """Best-effort read of trusted replies on the plan thread.

        Slack replies are treated as explicit amendments only when they come
        from the configured operator or trusted feedback users. API failures are non-fatal:
        reaction approval still works even when the bot lacks
        ``channels:history`` / ``groups:history``.
        """
        return collect_trusted_thread_feedback(
            self._client,
            channel=request.channel,
            message_ts=request.message_ts,
            feedback_user_ids=self._feedback_user_ids,
            purpose="plan feedback",
        )


class _TransportError(Exception):
    """Internal marker for an API-level failure (raised in
    ``_fetch_reactions``, caught in ``await_approval``). Not part of the
    public surface."""


def collect_trusted_thread_feedback(
    client: SlackClient,
    *,
    channel: str,
    message_ts: str,
    feedback_user_ids: Iterable[str],
    purpose: str = "thread feedback",
) -> tuple[ThreadFeedback, ...]:
    """Best-effort read of trusted replies on any Alfred Slack thread.

    This is shared by the plan approval gate and follow-up workflows so
    post-report or post-PR replies can be captured without granting any
    extra approval authority.
    """

    replies = getattr(client, "conversations_replies", None)
    if replies is None:
        return ()
    trusted = set(_dedupe_user_ids(feedback_user_ids))
    if not trusted:
        return ()
    try:
        resp = replies(channel=channel, ts=message_ts, limit=100)
    except Exception as exc:
        logger.warning("conversations.replies failed while collecting %s: %s", purpose, exc)
        return ()
    data = _as_mapping(resp)
    if not data.get("ok", False):
        logger.warning(
            "conversations.replies not-ok while collecting %s: %s",
            purpose,
            data.get("error") or "unknown",
        )
        return ()
    out: list[ThreadFeedback] = []
    for message in data.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if str(message.get("ts") or "") == message_ts:
            continue
        author = str(message.get("user") or "")
        if author not in trusted:
            continue
        text = _clean_thread_text(str(message.get("text") or ""))
        if not text:
            continue
        out.append(
            ThreadFeedback(
                author=author,
                text=text,
                ts=str(message.get("ts") or ""),
            )
        )
    return tuple(out)


def _as_mapping(resp: Any) -> dict[str, Any]:
    """Coerce ``slack_sdk.SlackResponse`` (or a test dict) to a plain
    dict. ``SlackResponse`` supports both ``.data`` and item access; we
    prefer ``.data`` when it exposes a dict."""
    if isinstance(resp, dict):
        return resp
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        return data
    # Last resort: assume mapping-like with .get().
    try:
        return dict(resp)
    except Exception:
        return {}


def _clean_thread_text(text: str) -> str:
    return "\n".join(
        re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()
    )


def _dedupe_user_ids(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


__all__ = [
    "APPROVAL_GRANTED",
    "APPROVAL_REJECTED",
    "APPROVAL_TIMEOUT",
    "APPROVAL_TRANSPORT_DOWN",
    "DEFAULT_APPROVE_EMOJIS",
    "DEFAULT_REJECT_EMOJIS",
    "ENV_OPERATOR_USER_ID",
    "ENV_TRUSTED_FEEDBACK_USER_IDS",
    "TRANSPORT_FAIL_THRESHOLD",
    "ApprovalRequest",
    "ApprovalResult",
    "SecretsResolver",
    "SlackApproval",
    "SlackClient",
    "ThreadFeedback",
    "aws_secrets_token_resolver",
    "collect_trusted_thread_feedback",
    "default_slack_client",
    "default_token_resolvers",
    "env_token_resolver",
    "file_cache_token_resolver",
    "operator_user_id_from_env",
    "resolve_bot_token",
    "trusted_feedback_user_ids_from_env",
]
