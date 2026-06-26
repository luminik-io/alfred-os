from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from slack_thread_status import (  # noqa: E402
    STATE_CI_FAIL,
    STATE_CI_PASS,
    STATE_CLAIMED,
    STATE_FILED,
    STATE_MERGED,
    STATE_PR_OPEN,
    IssueProgress,
    SlackThreadStatusTracker,
    default_issue_state_fetcher,
    render_status_update,
)

REPO_SLUG = "acme-org/api"
CHANNEL = "C1"
THREAD = "1716480000.000000"
ISSUE_URL = "https://github.com/acme-org/api/issues/42"


class Poster:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def chat_postMessage(self, **kwargs):
        self.messages.append(kwargs)
        return {"ok": True}


class ScriptedFetcher:
    """Returns a queued IssueProgress per call; repeats the last forever."""

    def __init__(self, *snapshots: IssueProgress | None) -> None:
        self.snapshots = list(snapshots)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, *, repo: str, issue_number: int) -> IssueProgress | None:
        self.calls.append((repo, issue_number))
        if not self.snapshots:
            return None
        if len(self.snapshots) == 1:
            return self.snapshots[0]
        return self.snapshots.pop(0)


def _tracker(tmp_path: Path, poster: Poster) -> SlackThreadStatusTracker:
    return SlackThreadStatusTracker(root=tmp_path / "status", poster=poster)


def _register(tracker: SlackThreadStatusTracker) -> None:
    tracker.register_issue_thread(
        channel=CHANNEL,
        thread_ts=THREAD,
        repo=REPO_SLUG,
        issue_number=42,
        issue_url=ISSUE_URL,
        title="Add a thing",
    )


def test_register_persists_record_and_filed_state(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, Poster())
    record = tracker.register_issue_thread(
        channel=CHANNEL, thread_ts=THREAD, repo=REPO_SLUG, issue_number=42, issue_url=ISSUE_URL
    )
    assert record is not None
    assert record.last_state == STATE_FILED
    assert record.posted_states == [STATE_FILED]
    files = list((tmp_path / "status").glob("*.json"))
    assert len(files) == 1


def test_register_rejects_bad_repo_or_issue(tmp_path: Path) -> None:
    tracker = _tracker(tmp_path, Poster())
    assert (
        tracker.register_issue_thread(
            channel=CHANNEL, thread_ts=THREAD, repo="not-a-repo", issue_number=42
        )
        is None
    )
    assert (
        tracker.register_issue_thread(
            channel=CHANNEL, thread_ts=THREAD, repo=REPO_SLUG, issue_number=0
        )
        is None
    )
    assert (
        tracker.register_issue_thread(channel="", thread_ts=THREAD, repo=REPO_SLUG, issue_number=42)
        is None
    )


def test_sweep_posts_claimed_then_pr_then_merged_as_deltas(tmp_path: Path) -> None:
    poster = Poster()
    tracker = _tracker(tmp_path, poster)
    _register(tracker)

    fetcher = ScriptedFetcher(
        IssueProgress(issue_state="OPEN", claimed_by="lucius"),
        IssueProgress(
            issue_state="OPEN",
            claimed_by="lucius",
            pr_url="https://github.com/acme-org/api/pull/7",
            pr_number=7,
            pr_state="OPEN",
            ci_status="PASS",
        ),
        IssueProgress(
            issue_state="CLOSED",
            claimed_by="lucius",
            pr_url="https://github.com/acme-org/api/pull/7",
            pr_number=7,
            pr_state="MERGED",
            ci_status="PASS",
        ),
    )

    first = tracker.sweep(fetcher=fetcher)
    assert first[0]["posted"] == [STATE_CLAIMED]
    assert "Issue claimed" in poster.messages[0]["text"]
    assert "lucius" in poster.messages[0]["text"]

    second = tracker.sweep(fetcher=fetcher)
    assert second[0]["posted"] == [STATE_PR_OPEN, STATE_CI_PASS]
    assert any("Pull request opened" in m["text"] for m in poster.messages)
    assert any("CI is green" in m["text"] for m in poster.messages)

    third = tracker.sweep(fetcher=fetcher)
    assert third[0]["posted"] == [STATE_MERGED]
    assert any("Merged" in m["text"] for m in poster.messages)
    assert poster.messages[-1]["thread_ts"] == THREAD
    assert poster.messages[-1]["channel"] == CHANNEL


def test_sweep_is_idempotent_no_double_posts(tmp_path: Path) -> None:
    poster = Poster()
    tracker = _tracker(tmp_path, poster)
    _register(tracker)
    progress = IssueProgress(
        issue_state="OPEN",
        claimed_by="bane",
        pr_url="https://github.com/acme-org/api/pull/8",
        pr_number=8,
        pr_state="OPEN",
        ci_status="PASS",
    )
    fetcher = ScriptedFetcher(progress)

    tracker.sweep(fetcher=fetcher)
    count_after_first = len(poster.messages)
    second = tracker.sweep(fetcher=fetcher)
    assert second[0]["posted"] == []
    assert len(poster.messages) == count_after_first


def test_terminal_thread_is_skipped(tmp_path: Path) -> None:
    poster = Poster()
    tracker = _tracker(tmp_path, poster)
    _register(tracker)
    merged = IssueProgress(
        issue_state="CLOSED",
        pr_url="https://github.com/acme-org/api/pull/9",
        pr_number=9,
        pr_state="MERGED",
    )
    tracker.sweep(fetcher=ScriptedFetcher(merged))
    posted_count = len(poster.messages)
    # A later sweep, even with a fresh fetcher, must not touch a merged thread.
    extra = tracker.sweep(fetcher=ScriptedFetcher(merged))
    assert extra == []
    assert len(poster.messages) == posted_count


def test_ci_fail_then_pass_each_post_once(tmp_path: Path) -> None:
    poster = Poster()
    tracker = _tracker(tmp_path, poster)
    _register(tracker)
    pr_open_fail = IssueProgress(
        issue_state="OPEN",
        claimed_by="nightwing",
        pr_url="https://github.com/acme-org/api/pull/10",
        pr_number=10,
        pr_state="OPEN",
        ci_status="FAIL",
    )
    pr_open_pass = IssueProgress(
        issue_state="OPEN",
        claimed_by="nightwing",
        pr_url="https://github.com/acme-org/api/pull/10",
        pr_number=10,
        pr_state="OPEN",
        ci_status="PASS",
    )
    fetcher = ScriptedFetcher(pr_open_fail, pr_open_pass, pr_open_pass)
    first = tracker.sweep(fetcher=fetcher)
    assert STATE_CI_FAIL in first[0]["posted"]
    second = tracker.sweep(fetcher=fetcher)
    assert second[0]["posted"] == [STATE_CI_PASS]
    third = tracker.sweep(fetcher=fetcher)
    assert third[0]["posted"] == []


def test_read_failure_leaves_record_untouched(tmp_path: Path) -> None:
    poster = Poster()
    tracker = _tracker(tmp_path, poster)
    _register(tracker)
    results = tracker.sweep(fetcher=ScriptedFetcher(None))
    assert results[0]["posted"] == []
    assert poster.messages == []
    # Record still readable and still at filed state.
    record = tracker._load(tracker._path(CHANNEL, THREAD))
    assert record is not None
    assert record.last_state == STATE_FILED


def test_post_failure_preserves_at_most_once_ordering(tmp_path: Path) -> None:
    class FailingPoster:
        def __init__(self) -> None:
            self.attempts = 0

        def chat_postMessage(self, **kwargs):
            self.attempts += 1
            raise RuntimeError("transport down")

    poster = FailingPoster()
    tracker = SlackThreadStatusTracker(root=tmp_path / "status", poster=poster)
    _register(tracker)
    progress = IssueProgress(issue_state="OPEN", claimed_by="robin")
    out = tracker.sweep(fetcher=ScriptedFetcher(progress))
    assert out[0]["posted"] == []
    # claimed not marked posted, so a recovered poster re-delivers it.
    good = Poster()
    tracker.poster = good
    out2 = tracker.sweep(fetcher=ScriptedFetcher(progress))
    assert out2[0]["posted"] == [STATE_CLAIMED]
    assert len(good.messages) == 1


def test_default_fetcher_reads_issue_and_pr(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_gh_json(cmd, default=None):
        calls.append(cmd)
        if cmd[1] == "issue":
            return {
                "state": "OPEN",
                "number": 42,
                "labels": [{"name": "agent:in-flight"}],
                "comments": [
                    {"body": "<!-- agent-claim: codename=lucius firing_id=f1 ts=x -->"},
                ],
            }
        if cmd[1] == "pr":
            return [
                {
                    "number": 7,
                    "url": "https://github.com/acme-org/api/pull/7",
                    "state": "OPEN",
                    "title": "Fix thing (closes #42)",
                    "body": "resolves #42",
                    "statusCheckRollup": [
                        {"status": "COMPLETED", "conclusion": "SUCCESS"},
                    ],
                }
            ]
        return default

    progress = default_issue_state_fetcher(repo=REPO_SLUG, issue_number=42, gh_json=fake_gh_json)
    assert progress is not None
    assert progress.claimed_by == "lucius"
    assert progress.pr_url == "https://github.com/acme-org/api/pull/7"
    assert progress.pr_state == "OPEN"
    assert progress.ci_status == "PASS"


def test_default_fetcher_token_isolation_avoids_substring_match(tmp_path: Path) -> None:
    def fake_gh_json(cmd, default=None):
        if cmd[1] == "issue":
            return {"state": "OPEN", "number": 12, "comments": []}
        if cmd[1] == "pr":
            # Only a PR that closes #1234 - must NOT match issue #12.
            return [
                {
                    "number": 99,
                    "url": "https://github.com/acme-org/api/pull/99",
                    "state": "OPEN",
                    "title": "Big work",
                    "body": "closes #1234",
                    "statusCheckRollup": [],
                }
            ]
        return default

    progress = default_issue_state_fetcher(repo=REPO_SLUG, issue_number=12, gh_json=fake_gh_json)
    assert progress is not None
    assert progress.pr_url == ""


def test_default_fetcher_returns_none_on_issue_read_failure(tmp_path: Path) -> None:
    def fake_gh_json(cmd, default=None):
        return default

    assert (
        default_issue_state_fetcher(repo=REPO_SLUG, issue_number=42, gh_json=fake_gh_json) is None
    )


def test_render_uses_issue_url_when_present() -> None:
    from slack_thread_status import ThreadStatusRecord

    record = ThreadStatusRecord(
        channel=CHANNEL,
        thread_ts=THREAD,
        repo=REPO_SLUG,
        issue_number=42,
        issue_url=ISSUE_URL,
    )
    text = render_status_update(record, STATE_CLAIMED, IssueProgress(claimed_by="lucius"))
    assert ISSUE_URL in text


def test_sweep_requires_a_fetcher(tmp_path: Path) -> None:
    tracker = SlackThreadStatusTracker(root=tmp_path / "status", poster=Poster())
    import pytest

    with pytest.raises(ValueError):
        tracker.sweep()
