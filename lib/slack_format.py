"""Block Kit formatters + bot-token-aware posters for per-firing threads.

The fleet's legacy Slack transport is
incoming-webhook-only: text, no threads, no severity colour. Webhooks
cannot post threaded replies. That requires ``chat.postMessage`` with a
``xoxb-`` bot token + ``thread_ts``. ``slack_approval`` already proves
the bot-token path for plan approvals; this module generalises it for
agent firings.

Three public entry points, all return ``False`` (or ``None`` for the
root) on missing token / network error / no channel configured. They
mirror the silent-skip pattern of ``slack_post`` so a caller without
Slack configured never blows up — at worst, the firing runs with no
operator visibility.

Design notes
------------

- ``ThreadHandle`` is the persistence anchor. One per firing. Stash it
  on the per-firing state (Lucius / Batman both keep one) and pass it to
  every reply call. ``channel`` + ``ts`` is what Slack's
  ``chat.postMessage`` needs to thread a reply, and what
  ``slack_approval.SlackApproval`` polls for the approval flow —
  same surface, two readers.
- Block Kit ``header`` block has a hard 150-char text limit. The
  per-block plain-text limit is 3000. We truncate aggressively in both
  spots and always with the explicit ``...[truncated]`` suffix so an
  operator inspecting the message in Slack sees that the post lost
  data, not the message itself.
- Severity → colour stripe goes on ``attachments[].color``. Block Kit
  on its own has no first-class severity colour, but legacy
  attachments still render a vertical stripe on the left of the
  message and Slack continues to support it. Green / yellow / red
  matches the rest of the fleet's mental model.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_runner.metadata import codename_with_role
from slack_approval import resolve_bot_token as _resolve_bot_token

SLACK_API = "https://slack.com/api"

# Default Slack channel for fleet-wide firing posts. ``SLACK_HOME_CHANNEL``
# is the canonical name; the ``BATMAN_APPROVAL_CHANNEL`` alias is read by
# ``slack_approval`` for plan approvals so the two paths land in the same
# room by default.
HOME_CHANNEL_ENV = "SLACK_HOME_CHANNEL"
BATMAN_APPROVAL_CHANNEL_ENV = "BATMAN_APPROVAL_CHANNEL"
HOME_CHANNEL_DEFAULT = "alfred"

# Severity → attachment colour. Hex codes match the brief; keeping them
# centralised here means a single edit re-skins every firing.
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
# so an operator sees the cut.
HEADER_MAX = 150
SECTION_MAX = 3000
_TRUNC = "...[truncated]"
_GITHUB_ISSUE_OR_PR_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<number>\d+)$"
)


@dataclass
class ThreadHandle:
    """One per firing — stash on the firing's per-run state (EventLog
    or local var) and pass to every ``firing_thread_reply`` /
    ``firing_thread_close`` call so all replies thread to the same root.

    ``channel`` is the Slack channel id resolved by the API at post time
    (``C0123ABC...``) — we keep that, not the human-readable name, so
    later API calls (post + reactions.get) don't have to re-resolve.
    ``ts`` is the message timestamp ``chat.postMessage`` returned;
    Slack's threading model uses it as ``thread_ts``.
    """

    channel: str
    ts: str
    permalink: str | None = None


def github_issue_link(repo: str, number: int, *, label: str | None = None) -> str:
    display = label or f"{repo}#{number}"
    return f"<https://github.com/{repo}/issues/{number}|{display}>"


def github_url_link(url: str, *, label: str | None = None) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    match = _GITHUB_ISSUE_OR_PR_RE.match(text)
    display = label
    if display is None and match:
        display = f"{match.group('owner')}/{match.group('repo')}#{match.group('number')}"
    return f"<{text}|{display or text}>"


def _home_channel(channel: str | None = None) -> str:
    """Resolve the channel for a firing post.

    Caller-supplied channel wins. Otherwise read ``SLACK_HOME_CHANNEL``
    env var, falling back to the literal ``alfred``. Strip any leading
    ``#`` so the API accepts it.
    """
    raw = (
        channel
        or os.environ.get(BATMAN_APPROVAL_CHANNEL_ENV)
        or os.environ.get(HOME_CHANNEL_ENV)
        or HOME_CHANNEL_DEFAULT
    ).strip()
    return raw.lstrip("#")


def _truncate(text: str, limit: int) -> str:
    """Hard-truncate ``text`` to ``limit`` chars, signalling the cut.

    Always leaves at least one visible char of the original — a 1-char
    limit-equivalent message is still preferable to ``...[truncated]``
    alone.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    keep = max(1, limit - len(_TRUNC))
    return text[:keep] + _TRUNC


def _coerce_severity(severity: str) -> str:
    """Coerce arbitrary strings to one of ``info`` / ``warn`` / ``alert``."""
    sev = (severity or "info").strip().lower()
    if sev not in SEVERITY_COLOUR:
        return "info"
    return sev


def _api_post(method: str, payload: dict, *, token: str) -> dict:
    """Tiny ``application/json`` Slack Web API wrapper.

    Block Kit requires a JSON body — the ``slack_approval`` module's
    ``_api_call`` form-encodes its parameters, which Slack accepts for
    plain-text posts but rejects for ``blocks`` / ``attachments``. Keep
    this caller separate so the two code paths don't fight over content
    type. Returns the parsed response or a synthetic ``{"ok": False}``
    on transport failure — never raises.
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


def build_chat_postmessage_payload(
    *,
    channel: str,
    text: str,
    severity: str = "info",
    thread_ts: str | None = None,
    blocks: list | None = None,
    notification_preview: str | None = None,
) -> dict:
    """Single source of truth for ``chat.postMessage`` payloads across the
    fleet. Every Slack post funnels through this builder.

    Two invariants:

    1. The body lives in exactly one place per payload. With ``blocks``,
       the body is in the blocks and top-level ``text`` is a notification
       preview only. Without ``blocks``, the body is in top-level
       ``text`` and the colour-stripe attachment carries only ``color``
       + ``fallback``.
    2. The colour stripe always rides on a legacy attachment (Block Kit
       has no severity-colour primitive). The attachment exists only to
       paint the stripe.
    """
    sev = _coerce_severity(severity)
    payload: dict = {
        "channel": channel.lstrip("#"),
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts

    if blocks is not None:
        # Block-Kit caller. The body is in the blocks; the top-level
        # ``text`` is the notification preview only — must NOT echo
        # any block content, or Slack stacks it on top of the rendered
        # Block Kit.
        payload["text"] = (notification_preview or "alfred fleet update").strip()
        payload["attachments"] = [
            {
                "color": SEVERITY_COLOUR[sev],
                "blocks": blocks,
                "fallback": text,
            }
        ]
    else:
        # Flat caller (the everyday ``slack_post`` path). Body in the
        # top-level ``text``; attachment carries colour + fallback only.
        payload["text"] = text
        payload["attachments"] = [
            {
                "color": SEVERITY_COLOUR[sev],
                "fallback": text,
            }
        ]
    return payload


def _get_permalink(channel: str, ts: str, *, token: str) -> str | None:
    """Best-effort ``chat.getPermalink``. None on failure — the link is
    convenience, not load-bearing."""
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
    """``2026-05-09 14:32 UTC`` — short, unambiguous, easy to scan in
    Slack's small-text context block."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_root_text(codename: str, summary_one_liner: str, severity: str) -> str:
    """Header-block plain text: ``<emoji> <codename> (<role>) — <summary>``.

    Roles come from ``codename_with_role`` (``ALFRED_<NAME>_ROLE`` env
    var). When unset the codename appears alone — operators on a forked
    install without role wiring still get readable posts.
    """
    label = codename_with_role(codename)
    emoji = SEVERITY_EMOJI[severity]
    raw = f"{emoji} {label}: {summary_one_liner}".strip()
    return _truncate(raw, HEADER_MAX)


def firing_thread_root(
    *,
    codename: str,
    firing_id: str,
    summary_one_liner: str,
    severity: str = "info",
    channel: str | None = None,
    body: str | None = None,
    plain_summary: str | None = None,
) -> ThreadHandle | None:
    """Post the per-firing thread root via ``chat.postMessage``.

    Returns a ``ThreadHandle`` on success, ``None`` on any failure
    (missing bot token, missing channel, transport error, Slack-side
    refusal). Callers that need any visibility at all should fall back
    to ``slack_post`` on the ``None`` branch — the legacy webhook path
    still posts top-level even without the bot token.

    Block layout:

      header    ``<emoji> <codename> (<role>) — <one-liner>``
      divider
      context   ``firing_id=<id> · <UTC start ts>``
      [summary] optional small-text context block giving a plain-English
                "what this issue/plan IS" (what changes, why, blast
                radius). Supplied by callers via ``issue_summary`` so an
                operator scanning the channel understands the firing
                without opening the linked issue.
      [body]    optional mrkdwn section, only when ``body`` is supplied
                (Batman uses this for the plan preview so the owner's
                reaction lands on the same message that carries the
                plan they're approving — one source of truth per firing).

    Severity colour goes on an attachment so the thread root carries the
    same vertical stripe as its replies.
    """
    sev = _coerce_severity(severity)
    token = _resolve_bot_token()
    if not token:
        return None
    channel_name = _home_channel(channel)
    if not channel_name:
        return None

    header_text = _format_root_text(codename, summary_one_liner, sev)
    context_text = _truncate(
        f"firing_id={firing_id} · started {_now_utc_short()}",
        SECTION_MAX,
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": context_text},
            ],
        },
    ]
    summary_text = (plain_summary or "").strip()
    if summary_text:
        # Small-text "what is this" block. Truncated to the context-block
        # ceiling so a long summary never overflows the post.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": _truncate(summary_text, SECTION_MAX)},
                ],
            }
        )
    if body:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": _truncate(body, SECTION_MAX)},
            }
        )
    payload = build_chat_postmessage_payload(
        channel=channel_name,
        text=header_text,
        severity=sev,
        blocks=blocks,
        notification_preview=f"Alfred · {codename} firing",
    )
    resp = _api_post("chat.postMessage", payload, token=token)
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


def firing_thread_reply(handle: ThreadHandle | None, *, text: str, severity: str = "info") -> bool:
    """Post an in-thread reply with a severity-colour attachment.

    Returns ``False`` on missing handle (caller never got a thread root
    — silent skip) or any API error. Severity colour stripe goes on the
    attachment so the reply carries the same green/yellow/red as the
    root.

    Callers can pass ``None`` for the handle to express "post nothing,
    we have no thread" — useful for callers that try-and-fall-through
    rather than branching on the root post's return.
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
    payload = build_chat_postmessage_payload(
        channel=handle.channel,
        text=body,
        severity=sev,
        thread_ts=handle.ts,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            }
        ],
        notification_preview="Alfred · thread reply",
    )
    resp = _api_post("chat.postMessage", payload, token=token)
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
    "github_issue_link",
    "github_url_link",
]
