"""Tests for ``lib/connectors/runner.py``.

Cover the seams the runner owns:

* Drafts new to the cache produce a ``gh issue create`` call with the
  correct argv (labels, body, footer, target repo).
* Drafts already in the seen-cache are skipped without calling ``gh``.
* ``mark_seen`` is invoked on success, not on failure.
* A failing connector does not break later connectors.
* ``--dry-run`` updates the seen-cache but never calls ``gh``.
* The seen-cache is persisted across runner instances.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    sys.path.insert(0, str(REPO_ROOT / "lib"))
    for mod in list(sys.modules):
        if mod.startswith("connectors"):
            del sys.modules[mod]
    yield


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeGh:
    """Capture argv passed to the fake ``gh`` and return canned results."""

    def __init__(self, *, returncode: int = 0, url: str = "https://github.com/x/y/issues/1"):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.url = url

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            returncode=self.returncode,
            stdout=(self.url + "\n") if self.returncode == 0 else "",
            stderr="" if self.returncode == 0 else "boom",
        )


class _FakeConnector:
    """Minimal Connector for runner-focused tests."""

    def __init__(
        self,
        name: str,
        drafts,
        *,
        default_repo: str | None = "org/repo",
        default_labels=None,
        crash_on_poll: bool = False,
    ):
        from connectors import IssueDraft  # noqa: F401 — module presence check

        self.name = name
        self.default_repo = default_repo
        self.default_labels = list(default_labels or [])
        self._drafts = drafts
        self._crash = crash_on_poll
        self.marked: list[str] = []

    def poll(self, since):
        if self._crash:
            raise RuntimeError("simulated poll crash")
        return list(self._drafts)

    def mark_seen(self, draft) -> None:
        self.marked.append(draft.source_id)


def _draft(source_id: str, **overrides):
    from connectors import IssueDraft

    base = {
        "source": "fake",
        "source_id": source_id,
        "title": f"title {source_id}",
        "body": f"body {source_id}",
        "labels": [],
        "severity": "info",
        "target_repo": None,
        "source_url": f"https://example.test/{source_id}",
    }
    base.update(overrides)
    return IssueDraft(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_runner_files_one_issue_per_new_draft():
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector("fake", [_draft("A"), _draft("B")])
    report = ConnectorRunner([conn], gh_runner=gh).sync()

    assert report.filed_count == 2
    assert report.failed_count == 0
    assert len(gh.calls) == 2
    # First call argv shape.
    argv = gh.calls[0]
    assert argv[:5] == ["gh", "issue", "create", "-R", "org/repo"]
    assert "--title" in argv
    assert "--body" in argv
    # agent:implement always present.
    assert "agent:implement" in argv
    # mark_seen called on both.
    assert conn.marked == ["A", "B"]


def test_runner_dedups_against_seen_cache(tmp_path, monkeypatch):
    """A second sync with the same draft does not re-file."""
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    drafts = [_draft("A")]
    conn = _FakeConnector("fake", drafts)
    first = ConnectorRunner([conn], gh_runner=gh).sync()
    assert first.filed_count == 1

    gh2 = _FakeGh()
    conn2 = _FakeConnector("fake", drafts)
    second = ConnectorRunner([conn2], gh_runner=gh2).sync()
    assert second.filed_count == 0
    assert second.skipped_count == 1
    assert gh2.calls == []


def test_runner_failure_does_not_mark_seen(tmp_path):
    from connectors.runner import ConnectorRunner

    gh = _FakeGh(returncode=1)
    conn = _FakeConnector("fake", [_draft("A")])
    report = ConnectorRunner([conn], gh_runner=gh).sync()

    assert report.filed_count == 0
    assert report.failed_count == 1
    assert conn.marked == []  # not seen


def test_one_bad_connector_does_not_kill_others():
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    bad = _FakeConnector("bad", [], crash_on_poll=True)
    good = _FakeConnector("good", [_draft("G")])
    report = ConnectorRunner([bad, good], gh_runner=gh).sync()

    assert report.filed_count == 1
    assert report.failed_count == 1
    assert any(r.source == "bad" for r in report.failed)
    assert any(r.source == "good" for r in report.filed)


def test_dry_run_does_not_call_gh_but_updates_seen_cache():
    from connectors._state import load_state
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector("fake", [_draft("A")])
    report = ConnectorRunner([conn], dry_run=True, gh_runner=gh).sync()

    assert report.filed_count == 1
    assert gh.calls == []
    state = load_state("fake")
    assert "A" in state["seen_ids"]


def test_labels_stack_runner_then_connector_then_draft_then_severity():
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector(
        "fake",
        [_draft("A", labels=["draft-label"], severity="blocker")],
        default_labels=["source:fake"],
    )
    ConnectorRunner([conn], gh_runner=gh).sync()

    argv = gh.calls[0]
    labels: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--label" and i + 1 < len(argv):
            labels.append(argv[i + 1])
            i += 2
        else:
            i += 1
    assert labels[0] == "agent:implement"
    assert "connector" in labels
    assert "source:fake" in labels
    assert "draft-label" in labels
    assert "connector:blocker" in labels


def test_body_footer_links_source_url():
    from connectors.runner import _compose_body

    body = _compose_body(_draft("A", source_url="https://example.test/A"))
    assert "https://example.test/A" in body
    assert "Filed by Alfred connector `fake` from `A`" in body
    assert body.rstrip().endswith("https://example.test/A")


def test_title_truncation_keeps_under_200_chars():
    from connectors.runner import _truncate_title

    long = "x" * 500
    out = _truncate_title(long)
    assert len(out) <= 200
    assert out.endswith("…")


def test_draft_with_explicit_target_repo_overrides_connector_default():
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector("fake", [_draft("A", target_repo="other/repo")])
    ConnectorRunner([conn], gh_runner=gh).sync()

    argv = gh.calls[0]
    assert argv[4] == "other/repo"


def test_no_target_repo_is_reported_as_failure():
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector("fake", [_draft("A")], default_repo=None)
    report = ConnectorRunner([conn], gh_runner=gh).sync()
    assert report.failed_count == 1
    assert "no target_repo" in (report.failed[0].error or "")


def test_seen_cache_file_lives_under_alfred_home(tmp_path, monkeypatch):
    from connectors._state import state_path
    from connectors.runner import ConnectorRunner

    gh = _FakeGh()
    conn = _FakeConnector("fake", [_draft("A")])
    ConnectorRunner([conn], gh_runner=gh).sync()

    p = state_path("fake")
    assert p.exists()
    payload = json.loads(p.read_text())
    assert "A" in payload["seen_ids"]
    # last_poll_at is set after the first sync.
    assert payload["last_poll_at"] is not None
    # And it lives under ALFRED_HOME/state/connectors/.
    assert str(p).startswith(str(tmp_path / "alfred" / "state" / "connectors"))
