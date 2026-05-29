from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_thread_registry import SlackThreadRecord, SlackThreadRegistry  # noqa: E402


def test_registry_round_trips_thread_record(tmp_path: Path) -> None:
    registry = SlackThreadRegistry(tmp_path)

    saved = registry.register(
        SlackThreadRecord(
            kind="plan",
            channel="C123",
            thread_ts="1716480000.123456",
            codename="batman",
            firing_id="fid",
            title="Plan a clean thing",
            parent_repo="owner/repo",
            parent_issue=42,
            metadata={"bundle_slug": "clean-thing"},
        )
    )
    loaded = registry.lookup("C123", "1716480000.123456")

    assert loaded == saved
    assert loaded is not None
    assert loaded.metadata == {"bundle_slug": "clean-thing"}


def test_registry_appends_feedback_jsonl(tmp_path: Path) -> None:
    registry = SlackThreadRegistry(tmp_path)
    record = registry.register(SlackThreadRecord(kind="report", channel="C", thread_ts="1.2"))

    path = registry.append_feedback(record, author="U1", text="fix: simplify copy", ts="1.3")

    assert path.exists()
    assert "fix: simplify copy" in path.read_text(encoding="utf-8")
