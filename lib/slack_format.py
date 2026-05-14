"""Block Kit formatters + bot-token-aware posters for per-firing threads.

The legacy webhook surface in ``agent_runner.slack_post`` is
incoming-webhook only: text, no threads, no severity colour. Webhooks
cannot post threaded replies, that requires ``chat.postMessage`` with
a ``xoxb-`` bot token + ``thread_ts``. This module is the bot-token
sibling.

Three public entry points: ``firing_thread_root`` posts a header block
carrying ``codename (role), summary``, ``firing_thread_reply`` posts
in-thread updates, ``firing_thread_close`` summarises with outcome +
duration. All three return ``False`` (or ``None`` for the root) on
missing token / network error / no channel, silent-skip pattern, so
a fleet without Slack configured still runs.

Every post carries a severity-colour attachment (green / yellow / red)
so the Slack channel reader sees the same vertical stripe across the
whole thread.

Stash one ``ThreadHandle`` per firing on the per-firing state and pass
it to every reply call. ``channel + ts`` is what
``chat.postMessage(thread_ts=...)`` needs to thread, and the same
surface a reaction-watcher would poll.

Bot-token resolution chain (env → disk cache → AWS Secrets Manager)
mirrors ``slack_post``'s webhook resolver. Override
``SLACK_BOT_TOKEN_SECRET_ID`` /
``SLACK_BOT_TOKEN_SECRET_REGION`` if the secret lives at a non-default
path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_runner import ALFRED_HOME, codename_with_role, run

SLACK_API = "https://slack.com/api"

# ``$ALFRED_HOME/state/slack-bot-token.cache``, written on first AWS
# resolution, refreshed via TTL. Mirrors the webhook cache in
# ``slack_post`` so the operator only re-touches AWS when secrets rotate.
TOKEN_CACHE = ALFRED_HOME / "state" / "slack-bot-token.cache"
TOKEN_CACHE_TTL = 30 * 24 * 3600  # 30 days

# Default Slack channel for fleet-wide firing posts.
HOME_CHANNEL_ENV = "SLACK_HOME_CHANNEL"
HOME_CHANNEL_DEFAULT = "alfred"

# Severity → attachment colour. Hex codes match the rest of the fleet's
# mental model (green/yellow/red); a single edit re-skins every firing.
SEVERITY_COLOUR = {
    "info": "#36a64f",
    "warn": "#f4a623",
    "alert": "#d82a2a",
}
SEVERITY_EMOJI = {
    "info": "✅",
    "warn": "⚠️",
    "alert": "🚨",
}

# Block Kit limits. Header text is the strictest at 150 chars; plain
# section blocks max out at 3000. Truncation is loud (``...[truncated]``)
# so an operator inspecting the message in Slack sees the cut.
HEADER_MAX = 150
SECTION_MAX = 3000
_TRUNC = "...[truncated]"


@dataclass
class ThreadHandle:
    """One per firing, stash on the firing's per-run state and pass to
    every ``firing_thread_reply`` / ``firing_thread_close`` call so all
    replies thread to the same root.

    ``channel`` is the resolved Slack channel id (``C0123ABC...``), we
    keep the id, not the human-readable name, so later API calls don't
    have to re-resolve. ``ts`` is the message timestamp
    ``chat.postMessage`` returned; Slack's threading model uses it as
    ``thread_ts``.
    """

    channel: str
    ts: str
    permalink: str | None = None


def _resolve_bot_token() -> str | None:
    """Find a bot token: env → disk cache → AWS Secrets Manager.

    Returns ``None`` when no token is configured; callers silent-skip on
    that path. The disk cache reduces AWS calls to roughly one per
    month per host; rotate by deleting the cache or letting TTL expire.
    """
    tok = (os.environ.get("SLACK_BOT_TOKEN") or "").strip()
    if tok:
        return tok
    if TOKEN_CACHE.exists():
        try:
            age = time.time() - TOKEN_CACHE.stat().st_mtime
        except OSError:
            age = TOKEN_CACHE_TTL + 1
        if age < TOKEN_CACHE_TTL:
            try:
                cached = TOKEN_CACHE.read_text().strip()
                if cached:
                    return cached
            except OSError:
                pass
    secret_id = os.environ.get("SLACK_BOT_TOKEN_SECRET_ID", "alfred/slack-bot-token")
    region = os.environ.get("SLACK_BOT_TOKEN_SECRET_REGION", "us-east-1")
    res = run(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            secret_id,
            "--region",
            region,
            "--query",
            "SecretString",
            "--output",
            "text",
        ],
        timeout=8,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    tok = res.stdout.strip()
    try:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(tok)
        TOKEN_CACHE.chmod(0o600)
    except OSError:
        pass
    return tok


def _home_channel(channel: str | None = None) -> str:
    """Resolve the channel for a firing post.

    Resolution order (first non-empty wins):

      1. caller-supplied ``channel`` argument
      2. ``BATMAN_APPROVAL_CHANNEL`` env var, historical name read by
         the alfred Batman approval flow; honoured here so a fleet
         that already routes Batman posts to a non-default channel
         keeps that routing for every firing thread.
      3. ``SLACK_HOME_CHANNEL`` env var, canonical name.
      4. literal ``alfred`` fallback.

    Strips any leading ``#`` so the API accepts it.
    """
    raw = (
        channel
        or os.environ.get("BATMAN_APPROVAL_CHANNEL")
        or os.environ.get(HOME_CHANNEL_ENV)
        or HOME_CHANNEL_DEFAULT
    ).strip()
    return raw.lstrip("#")


def _truncate(text: str, limit: int) -> str:
    """Hard-truncate ``text`` to ``limit`` chars, signalling the cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = max(1, limit - len(_TRUNC))
    return text[:keep] + _TRUNC


def _coerce_severity(severity: str) -> str:
    sev = (severity or "info").strip().lower()
    if sev not in SEVERITY_COLOUR:
        return "info"
    return sev


def _api_post(method: str, payload: dict, *, token: str) -> dict:
    """Tiny ``application/json`` Slack Web API wrapper.

    Block Kit requires a JSON body, ``form-encoded`` requests Slack
    accepts for plain text get rejected for ``blocks`` /
    ``attachments``. Returns the parsed response or ``{"ok": False}``
    on transport failure, never raises.
    """
    url = f"{SLACK_API}/{method}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except Exception as e:
        print(f"[slack-format] {method} error: {type(e).__name__}: {e}", file=sys.stderr)
        return {"ok": False, "error": f"transport:{type(e).__name__}"}


def _get_permalink(channel: str, ts: str, *, token: str) -> str | None:
    """Best-effort ``chat.getPermalink``. None on failure."""
    url = f"{SLACK_API}/chat.getPermalink"
    qs = urllib.parse.urlencode({"channel": channel, "message_ts": ts})
    req = urllib.request.Request(
        f"{url}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
        parsed = json.loads(body)
        if parsed.get("ok"):
            return parsed.get("permalink")
    except Exception:
        return None
    return None


def _now_utc_short() -> str:
    """``2026-05-09 14:32 UTC``, short, easy to scan in Slack context."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_root_text(codename: str, summary_one_liner: str, severity: str) -> str:
    """Header-block plain text: ``<emoji> <codename> (<role>), <summary>``."""
    label = codename_with_role(codename)
    emoji = SEVERITY_EMOJI[severity]
    raw = f"{emoji} {label}, {summary_one_liner}".strip()
    return _truncate(raw, HEADER_MAX)


def firing_thread_root(
    *,
    codename: str,
    firing_id: str,
    summary_one_liner: str,
    severity: str = "info",
    channel: str | None = None,
    body: str | None = None,
) -> ThreadHandle | None:
    """Post the per-firing thread root via ``chat.postMessage``.

    Returns a ``ThreadHandle`` on success, ``None`` on any failure
    (missing bot token, missing channel, transport error, Slack-side
    refusal). Callers that need any visibility at all should fall back
    to ``slack_post`` on the ``None`` branch, the legacy webhook path
    still posts top-level even without the bot token.

    Block layout:

      header   ``<emoji> <codename> (<role>), <one-liner>``
      divider
      context  ``firing_id=<id> · <UTC start ts>``
      [body]   optional mrkdwn section, only when ``body`` is supplied.

    Severity colour goes on an attachment so the thread root carries
    the same vertical stripe as its replies.
    """
    sev = _coerce_severity(severity)
    token = _resolve_bot_token()
    if not token:
        return None
    channel_name = _home_channel(channel)
    if not channel_name:
        return None

    header_text = _format_root_text(codename, summary_one_liner, sev)
    context_text = _truncate(f"firing_id={firing_id} · started {_now_utc_short()}", SECTION_MAX)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context_text}],
        },
    ]
    if body:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate(body, SECTION_MAX)},
            }
        )
    attachments = [
        {
            "color": SEVERITY_COLOUR[sev],
            "blocks": blocks,
            # ``fallback`` shows up in notifications + on clients that
            # can't render Block Kit (mobile lock-screen, screen
            # readers). Mirror the header so a quick glance still
            # works.
            "fallback": header_text,
        }
    ]
    # Top-level ``text`` is required by chat.postMessage but renders as
    # PLAIN TEXT above the attachment in modern Slack clients, when we
    # also put the same string in the attachment's header block, the
    # message appears DUPLICATED back-to-back. Use a terse generic
    # notification preview here so the channel UI shows only the Block
    # Kit attachment, while phone / screen-reader / push-notification
    # surfaces still get something meaningful via this field.
    notify_preview = f"alfred fleet · {codename} firing"
    resp = _api_post(
        "chat.postMessage",
        {
            "channel": channel_name,
            "text": notify_preview,
            "attachments": attachments,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        token=token,
    )
    if not resp.get("ok"):
        print(
            f"[slack-format] firing_thread_root postMessage failed: {resp.get('error')}",
            file=sys.stderr,
        )
        return None
    ts = resp.get("ts")
    if not ts:
        return None
    posted_channel = resp.get("channel", channel_name)
    permalink = _get_permalink(posted_channel, ts, token=token)
    return ThreadHandle(channel=posted_channel, ts=ts, permalink=permalink)


def firing_thread_reply(
    handle: ThreadHandle | None,
    *,
    text: str,
    severity: str = "info",
) -> bool:
    """Post an in-thread reply with a severity-colour attachment.

    Returns ``False`` on missing handle (caller never got a thread root
   , silent skip) or any API error. Severity colour stripe goes on the
    attachment so the reply carries the same green/yellow/red as the
    root.

    Callers can pass ``None`` for the handle to express "post nothing,
    we have no thread", useful for try-and-fall-through flows.
    """
    if handle is None:
        return False
    text = (text or "").strip()
    if not text:
        return False
    sev = _coerce_severity(severity)
    token = _resolve_bot_token()
    if not token:
        return False

    body = _truncate(text, SECTION_MAX)
    attachments = [
        {
            "color": SEVERITY_COLOUR[sev],
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": body}},
            ],
            "fallback": body,
        }
    ]
    notify_preview = "alfred fleet · thread reply"
    resp = _api_post(
        "chat.postMessage",
        {
            "channel": handle.channel,
            "thread_ts": handle.ts,
            "text": notify_preview,
            "attachments": attachments,
            "unfurl_links": False,
            "unfurl_media": False,
        },
        token=token,
    )
    if not resp.get("ok"):
        print(
            f"[slack-format] firing_thread_reply postMessage failed: {resp.get('error')}",
            file=sys.stderr,
        )
        return False
    return True


def _format_close_text(
    codename: str, firing_id: str, outcome: str, duration_seconds: float, severity: str
) -> str:
    """Mrkdwn body for the firing-close summary post."""
    minutes, seconds = divmod(int(max(0.0, duration_seconds)), 60)
    label = codename_with_role(codename)
    emoji = SEVERITY_EMOJI[severity]
    return (
        f"{emoji} *{label}* firing complete\n"
        f"• outcome: `{outcome}`\n"
        f"• duration: {minutes}m {seconds}s\n"
        f"• firing_id: `{firing_id}`"
    )


def firing_thread_close(
    handle: ThreadHandle | None,
    *,
    codename: str,
    firing_id: str,
    outcome: str,
    duration_seconds: float,
    severity: str = "info",
) -> bool:
    """Post the final summary reply: outcome + duration + firing_id.

    Convenience wrapper over ``firing_thread_reply`` with a structured
    body the operator can grep. Severity drives the stripe colour as
    usual; pass ``alert`` on hard-failure outcomes and ``warn`` on
    salvage paths so the close stripe matches the firing's tail event.
    """
    if handle is None:
        return False
    sev = _coerce_severity(severity)
    text = _format_close_text(codename, firing_id, outcome, duration_seconds, sev)
    return firing_thread_reply(handle, text=text, severity=sev)


__all__ = [
    "HOME_CHANNEL_DEFAULT",
    "HOME_CHANNEL_ENV",
    "SEVERITY_COLOUR",
    "SEVERITY_EMOJI",
    "ThreadHandle",
    "firing_thread_close",
    "firing_thread_reply",
    "firing_thread_root",
]
