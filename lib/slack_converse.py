"""Conversational, streamed Slack answers for Alfred mentions and thread replies.

This module gives Slack the same conversational surface the desktop Ask /
Compose converse path already has. When a trusted user @-mentions Alfred or
replies in an Alfred-started thread, the listener can route the turn here
instead of immediately building a planning draft. The turn is:

1. CLASSIFIED through the SAME intent path the desktop converse uses
   (``compose_converse.run_turn`` -> ``resolve_intent``). A ``conversation``
   turn (greeting, "what are you", "how does review work", a plain question)
   gets a real, repo-grounded answer. A ``build`` turn gets a prose reply that
   OFFERS to file an issue via the existing approved-Slack-plan-to-issue bridge,
   instead of forcing a planning form on the user.

2. STREAMED into Slack. We post a placeholder message immediately, then tail the
   running turn's stream-json transcript and progressively ``chat.update`` the
   message as assistant text arrives. Updates are throttled so a fast token
   stream cannot trip Slack's per-method rate limit (``chat.update`` is Tier 3,
   roughly 50/min; one update per ``THROTTLE`` seconds keeps us well under).

3. GROUNDED in bounded thread context. Prior messages in the same thread are
   gathered (capped) and threaded into the converse transcript so a reply like
   "and what about the mobile app?" is answered with the earlier turns in view.

SAFETY. This module never mutates anything and never files an issue on its own.
A ``build`` turn only ever produces PROSE that offers the existing approval
path; the issue is created solely by the existing
``SlackIssueBridge``/operator-approval gate the listener already owns. Every
guard the listener applies upstream (trust gating, channel allowlist, the
seen-event de-dup) still runs before we are called.

Everything here is config-driven (``SlackConverseConfig.from_env``) and inert
unless explicitly enabled, so the listener keeps its exact prior behavior by
default. The model engine and the Slack client are both injected, so the unit
tests drive the full path against a fake client and a fake runner with no
network and no live model.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from compose_converse import (
    INTENT_BUILD,
    INTENT_CONVERSATION,
    ConverseMessage,
    ConverseTurn,
)

# Environment knobs. All optional; unset means the feature is off (or a safe
# default), so dropping this module into the listener changes nothing until an
# operator opts in.
ENV_ENABLED = "ALFRED_SLACK_CONVERSE_ENABLED"
ENV_CHANNELS = "ALFRED_SLACK_CONVERSE_CHANNELS"
ENV_ENGINE = "ALFRED_SLACK_CONVERSE_ENGINE"
# Reuse the Compose converse engine as a fallback so an operator who already
# configured the desktop converse surface gets Slack converse for free.
ENV_FALLBACK_ENGINE = "ALFRED_COMPOSE_CONVERSE_ENGINE"
ENV_TIMEOUT = "ALFRED_SLACK_CONVERSE_TIMEOUT"
ENV_THREAD_CONTEXT = "ALFRED_SLACK_CONVERSE_THREAD_CONTEXT"
ENV_THROTTLE = "ALFRED_SLACK_CONVERSE_STREAM_THROTTLE"

DEFAULT_TIMEOUT = 180
# How many prior thread messages to gather as context. Bounded so a long thread
# never blows up the prompt or the Slack read.
DEFAULT_THREAD_CONTEXT = 12
# Minimum seconds between ``chat.update`` calls while streaming. Slack's
# ``chat.update`` is Tier 3 (~50/min). One update per second is ~60/min worst
# case, so we default a touch above that to stay comfortably inside the limit
# even with clock jitter.
DEFAULT_THROTTLE = 1.2
# A Slack message body is capped well below this, but assistant text can be
# long; trim what we stream into a single message so an update never fails for
# length. Full prose still lands in the final reconciled update.
MAX_STREAM_CHARS = 3500

# The placeholder shown the instant a mention lands, before the first token.
PLACEHOLDER = "_Alfred is thinking…_"


class StreamingSlackClient(Protocol):
    """The Slack Web API subset the streaming poster needs.

    ``slack_sdk.WebClient`` satisfies this natively; tests pass a fake with the
    same method names. ``conversations_replies`` is optional (thread context is
    best-effort and degrades to no context when it is absent)."""

    def chat_postMessage(self, **kwargs: Any) -> Any: ...

    def chat_update(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class SlackConverseConfig:
    """Resolved, immutable converse configuration for one listener instance."""

    enabled: bool = False
    channels: frozenset[str] = frozenset()
    engine: str = ""
    timeout: int = DEFAULT_TIMEOUT
    thread_context: int = DEFAULT_THREAD_CONTEXT
    throttle: float = DEFAULT_THROTTLE

    @classmethod
    def from_env(cls) -> SlackConverseConfig:
        return cls(
            enabled=_env_flag(ENV_ENABLED),
            channels=frozenset(_parse_channels(os.environ.get(ENV_CHANNELS))),
            engine=(
                os.environ.get(ENV_ENGINE) or os.environ.get(ENV_FALLBACK_ENGINE) or ""
            ).strip(),
            timeout=_env_int(ENV_TIMEOUT, DEFAULT_TIMEOUT),
            thread_context=_env_int(ENV_THREAD_CONTEXT, DEFAULT_THREAD_CONTEXT),
            throttle=_env_float(ENV_THROTTLE, DEFAULT_THROTTLE),
        )

    def engages(self, channel: str) -> bool:
        """True iff converse should run for ``channel``.

        Off-by-default and, when on, scoped to the channel allowlist. An empty
        allowlist means "every channel the listener already trusts" -- the
        listener has already gated trust and (for ambient) its own allowlist
        before we are reached, so an empty converse allowlist is not a blast
        radius, it just declines to add a second, narrower gate. An operator who
        wants converse limited to specific channels lists them explicitly.
        """
        if not self.enabled or not self.engine:
            return False
        if not self.channels:
            return True
        return channel in self.channels


# ---------------------------------------------------------------------------
# Thread context gathering (bounded, best-effort)
# ---------------------------------------------------------------------------


def gather_thread_context(
    client: Any,
    *,
    channel: str,
    root_ts: str,
    bot_user_id: str = "",
    limit: int = DEFAULT_THREAD_CONTEXT,
    exclude_ts: str = "",
) -> list[ConverseMessage]:
    """Read prior thread messages as converse context, oldest-first and bounded.

    Best-effort: a missing ``conversations_replies`` method, an API error, or a
    not-ok response all degrade to an empty context rather than raising, so a
    transient Slack read never blocks the answer. The bot's own messages map to
    the ``assistant`` role and everyone else to ``user`` so the converse turn
    reads the back-and-forth correctly. ``exclude_ts`` drops the triggering
    message itself (it is supplied separately as the latest user turn).
    """
    if limit <= 0:
        return []
    replies = getattr(client, "conversations_replies", None)
    if replies is None:
        return []
    try:
        resp = replies(channel=channel, ts=root_ts, limit=max(limit + 1, 2))
    except Exception:
        return []
    data = _as_mapping(resp)
    if not data.get("ok", False):
        return []
    bot = (bot_user_id or "").strip()
    out: list[ConverseMessage] = []
    for message in data.get("messages") or []:
        if not isinstance(message, dict):
            continue
        ts = str(message.get("ts") or "")
        if exclude_ts and ts == exclude_ts:
            continue
        raw_text = str(message.get("text") or "")
        text = _clean_text(raw_text)
        if not text:
            continue
        author = str(message.get("user") or "")
        is_bot = bool(message.get("bot_id")) or (bool(bot) and author == bot)
        role = "assistant" if is_bot else "user"
        out.append(ConverseMessage(role=role, content=text))
    # Keep the most recent ``limit`` turns, preserving chronological order.
    return out[-limit:]


# ---------------------------------------------------------------------------
# Streaming poster: placeholder -> throttled chat.update
# ---------------------------------------------------------------------------


class SlackStreamPoster:
    """Post a placeholder, then progressively ``chat.update`` as text arrives.

    The poster owns exactly one Slack message. :meth:`start` posts the
    placeholder and records its ``ts``. :meth:`update` rewrites the message with
    the latest streamed text, but only when at least ``throttle`` seconds have
    passed since the last update (so a fast token stream cannot exceed Slack's
    ``chat.update`` rate limit). :meth:`finalize` always writes the final text,
    ignoring the throttle, so the reconciled answer is never dropped.

    Every Slack call is wrapped: a transport error never propagates, it just
    means that one update is skipped. ``now`` is injectable so tests drive the
    throttle deterministically without sleeping.
    """

    def __init__(
        self,
        client: Any,
        *,
        channel: str,
        thread_ts: str,
        throttle: float = DEFAULT_THROTTLE,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._throttle = max(0.0, throttle)
        self._now = now or time.monotonic
        self._message_ts: str = ""
        self._last_update_at: float = 0.0
        self._last_text: str = ""

    @property
    def message_ts(self) -> str:
        return self._message_ts

    def start(self, placeholder: str = PLACEHOLDER) -> bool:
        """Post the placeholder message. Returns True iff a ts was obtained."""
        post = getattr(self._client, "chat_postMessage", None)
        if post is None:
            return False
        try:
            resp = post(
                channel=self._channel,
                thread_ts=self._thread_ts,
                text=placeholder,
            )
        except Exception:
            return False
        data = _as_mapping(resp)
        self._message_ts = str(data.get("ts") or "")
        self._last_text = placeholder
        self._last_update_at = self._now()
        return bool(self._message_ts)

    def update(self, text: str) -> None:
        """Throttled progressive update. Skips when called too soon or unchanged."""
        text = _trim_stream(text)
        if not self._message_ts or not text or text == self._last_text:
            return
        if self._now() - self._last_update_at < self._throttle:
            return
        self._write(text)

    def finalize(self, text: str) -> None:
        """Final update, never throttled, so the reconciled answer always lands."""
        text = _trim_stream(text)
        if not self._message_ts or not text or text == self._last_text:
            return
        self._write(text)

    def _write(self, text: str) -> None:
        update = getattr(self._client, "chat_update", None)
        if update is None:
            return
        try:
            update(channel=self._channel, ts=self._message_ts, text=text)
        except Exception:
            return
        self._last_text = text
        self._last_update_at = self._now()


# ---------------------------------------------------------------------------
# Streaming runner: run the turn on a worker while tailing the transcript
# ---------------------------------------------------------------------------


@dataclass
class ConverseStreamResult:
    """Outcome of a streamed converse turn."""

    turn: ConverseTurn | None
    streamed: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.turn is not None


def stream_converse_to_slack(
    *,
    run_turn: Callable[[], ConverseTurn | None],
    poster: SlackStreamPoster,
    transcript_path: Path,
    extract_tokens: Callable[[Path], list[str]],
    poll_seconds: float = 0.2,
    render: Callable[[ConverseTurn], str] | None = None,
) -> ConverseStreamResult:
    """Run one converse turn while progressively updating a Slack message.

    ``run_turn`` is the blocking interrogator call (it tees assistant text to
    ``transcript_path``). It runs on a worker thread so this loop can tail the
    transcript with ``extract_tokens`` and ``poster.update`` the partial text as
    it grows. When the turn returns, ``render`` shapes the final reply text and
    ``poster.finalize`` writes it. ``run_turn`` returning ``None`` (no live
    session / unparseable output) yields a result the caller surfaces honestly.

    Pure orchestration: no Slack or model specifics live here, so the unit tests
    drive it with a fake runner that writes a transcript and a fake poster.
    """
    result_box: dict[str, Any] = {}
    done = threading.Event()

    def _worker() -> None:
        try:
            result_box["turn"] = run_turn()
        except Exception as exc:  # never let the worker crash the listener
            result_box["error"] = str(exc) or exc.__class__.__name__
        finally:
            done.set()

    worker = threading.Thread(target=_worker, name="slack-converse-stream", daemon=True)
    worker.start()

    streamed = False
    while not done.wait(poll_seconds):
        partial = _join_tokens(_safe_extract(extract_tokens, transcript_path))
        if partial:
            poster.update(partial)
            streamed = True

    worker.join(1.0)

    if "error" in result_box:
        return ConverseStreamResult(turn=None, streamed=streamed, error=result_box["error"])
    turn = result_box.get("turn")
    if turn is None:
        return ConverseStreamResult(turn=None, streamed=streamed)
    final_text = render(turn) if render is not None else turn.reply
    poster.finalize(final_text)
    return ConverseStreamResult(turn=turn, streamed=streamed)


# ---------------------------------------------------------------------------
# Reply rendering: conversation answer vs build offer
# ---------------------------------------------------------------------------


@dataclass
class ConverseReply:
    """The reply text and whether it offered to file an issue."""

    text: str
    intent: str
    offered_issue: bool = False
    fields: dict[str, Any] = field(default_factory=dict)


def render_converse_reply(turn: ConverseTurn, *, bridge_enabled: bool) -> ConverseReply:
    """Shape a converse turn into the Slack reply text.

    A ``conversation`` turn is returned as-is: a plain, warm answer. A ``build``
    turn keeps the model's prose reply and APPENDS a short, optional offer to
    file an issue through the existing approval bridge -- never a forced form.
    When the bridge is disabled the offer is omitted (we do not advertise a path
    that cannot run); the conversational answer still stands on its own.
    """
    reply = (turn.reply or "").strip()
    if turn.intent == INTENT_CONVERSATION:
        return ConverseReply(text=reply, intent=INTENT_CONVERSATION)

    # build turn: offer, do not force.
    if not bridge_enabled:
        return ConverseReply(text=reply, intent=INTENT_BUILD, offered_issue=False)

    offer = _build_offer(turn)
    text = f"{reply}\n\n{offer}" if reply else offer
    return ConverseReply(text=text, intent=INTENT_BUILD, offered_issue=True)


def _build_offer(turn: ConverseTurn) -> str:
    title = (turn.draft.title or "").strip()
    if title:
        lead = f"I can turn this into a tracked issue (“{title}”) when you are ready."
    else:
        lead = "I can turn this into a tracked issue when you are ready."
    return (
        f"{lead} Reply `ship it` to file it, or keep talking and I will refine the "
        "scope first. Nothing is filed and no code runs until you approve."
    )


# ---------------------------------------------------------------------------
# Top-level orchestration: classify + stream one Slack converse turn
# ---------------------------------------------------------------------------


@dataclass
class SlackConverseOutcome:
    """What the listener needs back after a streamed converse turn."""

    handled: bool
    intent: str = ""
    offered_issue: bool = False
    streamed: bool = False
    detail: str = ""


def run_slack_converse(
    *,
    client: Any,
    config: SlackConverseConfig,
    channel: str,
    thread_ts: str,
    user_message: str,
    bot_user_id: str = "",
    exclude_ts: str = "",
    bridge_enabled: bool = False,
    workdir: Path | None = None,
    build_turn: Callable[..., ConverseTurn | None] | None = None,
    transcript_for: Callable[[str], Path] | None = None,
    extract_tokens: Callable[[Path], list[str]] | None = None,
    now: Callable[[], float] | None = None,
) -> SlackConverseOutcome:
    """Classify, stream, and post one conversational Slack answer.

    The whole pipeline:

    1. Gather bounded prior thread context (best-effort).
    2. Append the triggering ``user_message`` as the latest user turn.
    3. Post a placeholder, then run the converse turn while progressively
       updating the Slack message from the streamed transcript.
    4. Render the final reply: a plain answer for a ``conversation`` turn, or a
       prose answer plus an OPTIONAL offer to file an issue for a ``build`` turn.

    ``build_turn`` runs one interrogator turn and returns a :class:`ConverseTurn`
    (or ``None`` when no live session / unparseable). It defaults to the real
    Compose-grounded runner; tests inject a fake that writes a transcript and
    returns a canned turn, so no model or network is touched. Returns a
    :class:`SlackConverseOutcome`; ``handled`` is False only when there was no
    usable answer (the listener then falls through to its prior behavior).
    """
    if build_turn is None:
        build_turn = _default_build_turn
    if transcript_for is None:
        transcript_for = _default_transcript_for
    if extract_tokens is None:
        extract_tokens = _default_extract_tokens()

    context = gather_thread_context(
        client,
        channel=channel,
        root_ts=thread_ts,
        bot_user_id=bot_user_id,
        limit=config.thread_context,
        exclude_ts=exclude_ts,
    )
    clean_message = _clean_text(user_message)
    if not clean_message:
        return SlackConverseOutcome(handled=False, detail="empty message")
    messages = [*context, ConverseMessage(role="user", content=clean_message)]

    firing_id = _converse_firing_id()
    transcript_path = transcript_for(firing_id)

    poster = SlackStreamPoster(
        client,
        channel=channel,
        thread_ts=thread_ts,
        throttle=config.throttle,
        now=now,
    )
    if not poster.start():
        return SlackConverseOutcome(handled=False, detail="could not post placeholder")

    def _run() -> ConverseTurn | None:
        return build_turn(
            messages=messages,
            engine=config.engine,
            timeout=config.timeout,
            firing_id=firing_id,
            workdir=workdir or Path.cwd(),
        )

    reply_box: dict[str, ConverseReply] = {}

    def _render(turn: ConverseTurn) -> str:
        reply = render_converse_reply(turn, bridge_enabled=bridge_enabled)
        reply_box["reply"] = reply
        return reply.text

    result = stream_converse_to_slack(
        run_turn=_run,
        poster=poster,
        transcript_path=transcript_path,
        extract_tokens=extract_tokens,
        render=_render,
    )

    if not result.ok:
        poster.finalize(
            "I could not reach the conversational engine just now. "
            "Try again in a moment, or send the request as a plan."
        )
        return SlackConverseOutcome(
            handled=True,
            streamed=result.streamed,
            detail=result.error or "live_session_unavailable",
        )

    reply = reply_box.get("reply")
    return SlackConverseOutcome(
        handled=True,
        intent=reply.intent if reply else "",
        offered_issue=bool(reply and reply.offered_issue),
        streamed=result.streamed,
    )


def _default_build_turn(
    *,
    messages: list[ConverseMessage],
    engine: str,
    timeout: int,
    firing_id: str,
    workdir: Path,
) -> ConverseTurn | None:
    """Run one Compose-grounded interrogator turn for Slack (real model path).

    Reuses every Compose converse primitive so the intent classification and
    spec building are identical to the desktop Ask surface: the same system
    prompt, repo grounding, code map, and ``run_turn`` (which calls
    ``resolve_intent``). Streaming is forced so the turn tees assistant tokens to
    the transcript the caller tails. Returns ``None`` on any setup failure so the
    listener degrades to its prior planning intake rather than raising.
    """
    try:
        import compose_converse as cc
        from agent_runner.metadata import load_prompt
    except Exception:
        return None

    repos = _context_repos(messages)
    try:
        workspace_root = _workspace_root()
        repo_grounding = cc.build_repo_grounding(
            repos,
            workspace_root=workspace_root,
            repo_to_local=_repo_to_local(),
        )
        code_map = cc.load_code_map(_code_map_path())
        intake_guidance = cc.intake_guidance_for(os.environ.get("ALFRED_INTAKE_PROFILE") or "")
        system_prompt = cc.render_system_prompt(
            prompt_path=_interrogator_prompt_path(),
            repo_grounding=repo_grounding,
            code_map=code_map,
            intake_guidance=intake_guidance,
            loader=load_prompt,
        )
    except OSError:
        return None
    except Exception:
        return None

    from spec_helper import IssueDraft

    return cc.run_turn(
        system_prompt=system_prompt,
        messages=messages,
        repo_grounding=repo_grounding,
        code_map=code_map,
        intake_guidance=intake_guidance,
        base_draft=IssueDraft(title=""),
        engine=engine,
        workdir=workdir,
        timeout=timeout,
        firing_id=firing_id,
    )


def _default_transcript_for(firing_id: str) -> Path:
    """Resolve the transcript JSONL the converse turn tees to.

    Mirrors ``agent_runner.transcript_path`` bucketing so the tail reads the same
    file the streaming Claude path writes.
    """
    try:
        from agent_runner import transcript_path
        from compose_converse import CONVERSE_AGENT

        return transcript_path(CONVERSE_AGENT, firing_id)
    except Exception:  # pragma: no cover - defensive
        from datetime import UTC, datetime

        base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
        month = datetime.now(UTC).strftime("%Y-%m")
        return (
            Path(base)
            / "state"
            / "transcripts"
            / "compose-interrogator"
            / month
            / f"{firing_id}.jsonl"
        )


def _default_extract_tokens() -> Callable[[Path], list[str]]:
    try:
        from server.streaming import assistant_text_fragments

        return assistant_text_fragments
    except Exception:  # pragma: no cover - defensive
        return _fallback_assistant_text_fragments


def _fallback_assistant_text_fragments(transcript_path: Path) -> list[str]:
    """Minimal stream-json assistant-text extractor (mirror of server helper)."""
    import json

    try:
        text = transcript_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    fragments: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        message = obj.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str) and value:
                    fragments.append(value)
    return fragments


def _converse_firing_id() -> str:
    try:
        from compose_converse import converse_firing_id

        return converse_firing_id()
    except Exception:  # pragma: no cover - defensive
        from datetime import UTC, datetime

        return datetime.now(UTC).strftime("slack-converse-%Y%m%d-%H%M%S-%f")


def _context_repos(messages: Iterable[ConverseMessage]) -> list[str]:
    """Pull any ``owner/repo`` slugs mentioned in the conversation for grounding."""
    seen: set[str] = set()
    out: list[str] = []
    for message in messages:
        for match in re.findall(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b", message.content):
            if match not in seen:
                seen.add(match)
                out.append(match)
    return out


def _workspace_root() -> Path:
    try:
        from agent_runner.paths import WORKSPACE

        return Path(WORKSPACE)
    except Exception:  # pragma: no cover - defensive
        base = os.environ.get("WORKSPACE_ROOT") or os.path.expanduser("~/code")
        return Path(base)


def _repo_to_local() -> dict[str, str]:
    try:
        from agent_runner.github import GH_REPO_TO_LOCAL

        return dict(GH_REPO_TO_LOCAL)
    except Exception:  # pragma: no cover - defensive
        return {}


def _code_map_path() -> Path:
    base = os.environ.get("ALFRED_HOME") or os.path.expanduser("~/.alfred")
    return Path(base) / "state" / "code-map.json"


def _interrogator_prompt_path() -> Path:
    override = os.environ.get("ALFRED_SPEC_INTERROGATOR_PROMPT")
    if override:
        return Path(override)
    relative = Path("prompts") / "spec-interrogator.md"
    candidates: list[Path] = []
    runtime_home = os.environ.get("ALFRED_HOME")
    if runtime_home:
        candidates.append(Path(runtime_home) / relative)
    candidates.append(Path(__file__).resolve().parents[1] / relative)
    candidates.append(Path.cwd() / relative)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_tokens(tokens: Iterable[str]) -> str:
    return "".join(tokens).strip()


def _safe_extract(extract: Callable[[Path], list[str]], path: Path) -> list[str]:
    try:
        return extract(path)
    except Exception:
        return []


def _trim_stream(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_STREAM_CHARS:
        return text
    return text[: MAX_STREAM_CHARS - 1].rstrip() + "…"


def _clean_text(text: str) -> str:
    # Strip Slack mention tokens and link markup so the converse turn reads
    # plain prose, mirroring the listener's own cleaning.
    text = re.sub(r"<@[A-Z0-9]+>", " ", text)
    text = re.sub(r"<mailto:[^|>]+\|([^>]+)>", r"\1", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", text)
    return " ".join(text.split())


def _as_mapping(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        return data
    if hasattr(resp, "get"):
        try:
            return dict(resp)
        except Exception:
            return {}
    return {}


def _parse_channels(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,;\s]+", str(raw or "")):
        channel = item.strip()
        if channel and channel not in seen:
            seen.add(channel)
            out.append(channel)
    return out


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


__all__ = [
    "DEFAULT_THREAD_CONTEXT",
    "DEFAULT_THROTTLE",
    "DEFAULT_TIMEOUT",
    "ENV_CHANNELS",
    "ENV_ENABLED",
    "ENV_ENGINE",
    "ENV_THREAD_CONTEXT",
    "ENV_THROTTLE",
    "ENV_TIMEOUT",
    "PLACEHOLDER",
    "ConverseReply",
    "ConverseStreamResult",
    "SlackConverseConfig",
    "SlackConverseOutcome",
    "SlackStreamPoster",
    "StreamingSlackClient",
    "gather_thread_context",
    "render_converse_reply",
    "run_slack_converse",
    "stream_converse_to_slack",
]
