"""Presentation helpers for the local ``alfred serve`` templates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def parse_timestamp(value: str | None) -> datetime | None:
    """Best-effort ISO timestamp parser for event-log values."""
    if not value:
        return None
    raw = value.strip()
    if not raw or raw.lower() in {"never", "none", "null"}:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def friendly_time(value: str | None, *, now: datetime | None = None) -> str:
    """Render a timestamp for scanning instead of forensic precision."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return "never" if not value or value == "never" else str(value)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    delta = current - parsed
    if timedelta(0) <= delta < timedelta(minutes=1):
        return "just now"
    if timedelta(minutes=1) <= delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes}m ago"
    if timedelta(hours=1) <= delta < timedelta(hours=24):
        hours = int(delta.total_seconds() // 3600)
        return f"{hours}h ago"
    if parsed.date() == current.date() - timedelta(days=1):
        return f"yesterday {parsed.strftime('%H:%M')}"
    if parsed.year == current.year:
        return parsed.strftime("%b %-d, %H:%M")
    return parsed.strftime("%b %-d, %Y")


def timestamp_title(value: str | None) -> str:
    """Full timestamp for ``title`` attributes."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return "never" if not value else str(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def short_firing_id(value: str | None) -> str:
    """Compact firing id label that still preserves the useful suffix."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 22:
        return text
    return f"{text[:15]}...{text[-4:]}"
