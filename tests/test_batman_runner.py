"""Tests for the ``bin/batman.py`` runner shell.

The heavy parser and lifecycle primitives live in ``lib/batman.py``. These
tests cover runner-only wiring that should stay offline and deterministic.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "bin" / "batman.py"


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod in ("batman", "batman_runner", "slack_format"):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    yield


def _load_runner():
    spec = importlib.util.spec_from_file_location("batman_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["batman_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_batman_pickup_blocks_done_large_features():
    runner = _load_runner()
    assert runner._has_batman_pickup_blocker({"agent:large-feature", "agent:done"})


def test_main_noops_when_parent_repo_unconfigured(monkeypatch, capsys):
    runner = _load_runner()

    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_kw: True)
    monkeypatch.setattr(runner, "preflight", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        runner.BatmanLifecycleConfig,
        "from_env",
        classmethod(lambda _cls: runner.BatmanLifecycleConfig(parent_repo="")),
    )
    monkeypatch.setattr(
        runner,
        "_list_parent_repo_large_features",
        lambda *_a, **_kw: pytest.fail("must not query GitHub without BATMAN_PARENT_REPO"),
    )

    assert runner.main() == 0
    assert "BATMAN_PARENT_REPO is not configured" in capsys.readouterr().out


def test_lifecycle_empty_plan_fails_before_approval(monkeypatch, capsys):
    runner = _load_runner()
    reports = []
    slack_posts = []
    cleared = []
    issue_edits = []
    ensured = []

    plan = SimpleNamespace(
        bundle_slug="empty-plan",
        children=(),
        affected_repos=("myorg/backend",),
        readiness_blockers=(
            SimpleNamespace(message="No child issues were parsed from the parent body."),
        ),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            raise AssertionError("should not request approval for a hollow plan")

        def report(self, _plan, result):
            reports.append(result)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(
        runner, "slack_post", lambda message, **kw: slack_posts.append((message, kw))
    )
    monkeypatch.setattr(
        runner,
        "_clear_pending_envelope",
        lambda repo, number: cleared.append(("clear", repo, number)),
    )
    monkeypatch.setattr(
        runner,
        "_unset_pending_approval_label",
        lambda repo, number: cleared.append(("unset", repo, number)),
    )
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda repo, number, **kw: issue_edits.append((repo, number, kw)),
    )
    monkeypatch.setattr(
        runner,
        "ensure_labels",
        lambda repo, labels=None: ensured.append((repo, labels)),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent"),
        parent_issue={"number": 83, "title": "hollow", "body": ""},
        firing_id="fid-empty",
    )

    captured = capsys.readouterr()
    assert out == 0
    assert "[BATMAN-DECOMPOSITION-FAILED]" in captured.out
    assert "parent=myorg/parent#83" in captured.out
    assert slack_posts and "No approval was requested" in slack_posts[0][0]
    assert reports and reports[0].reason == runner.EXEC_NO_CHILDREN
    assert cleared == [
        ("clear", "myorg/parent", 83),
        ("unset", "myorg/parent", 83),
    ]
    assert ensured == [("myorg/parent", runner.LIFECYCLE_LABELS)]
    assert issue_edits == [
        (
            "myorg/parent",
            83,
            {"add_labels": ["needs:human-scope"]},
        )
    ]


def test_lifecycle_prints_awaiting_approval_sentinel(monkeypatch, capsys):
    runner = _load_runner()
    monkeypatch.setitem(
        sys.modules,
        "slack_approval",
        SimpleNamespace(
            SlackApproval=lambda *_args, **_kwargs: object(),
            default_slack_client=lambda: object(),
            operator_user_id_from_env=lambda: "U123",
        ),
    )
    plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(SimpleNamespace(repo="myorg/backend"),),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return runner.ApprovalEnvelope(channel="C123", message_ts="1700.0001", plan=plan)

        def await_approval(self, _envelope):
            return SimpleNamespace(
                approved=False,
                verdict="approval_timeout",
                elapsed_s=12,
                detail="no reaction",
            )

        def report(self, _plan, _result):
            pass

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "_save_pending_envelope", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_set_pending_approval_label", lambda *a, **kw: None)

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(
            parent_repo="myorg/parent",
            auto_execute="approval-gate",
            approval_timeout_s=60,
        ),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-ready",
    )

    captured = capsys.readouterr()
    assert out == 0
    assert "[BATMAN-AWAITING-APPROVAL]" in captured.out
    assert "parent=myorg/parent#83" in captured.out
    assert "message_ts=1700.0001" in captured.out
    assert "timeout_s=60" in captured.out


def test_lifecycle_file_approval_mode_waits_without_slack_gate(monkeypatch, capsys):
    runner = _load_runner()
    awaited = []
    reports = []
    plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(SimpleNamespace(repo="myorg/backend"),),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )

    class FakeLifecycle:
        def __init__(self, **kwargs):
            assert kwargs["gate"] is None

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def await_approval(self, envelope):
            awaited.append(envelope)
            return SimpleNamespace(
                approved=False,
                verdict="approval_timeout",
                elapsed_s=0,
                detail="no marker",
            )

        def report(self, _plan, result):
            reports.append(result)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(
            parent_repo="myorg/parent",
            auto_execute="approval-gate",
            approval_mode=runner.APPROVAL_MODE_FILE,
            approval_timeout_s=0,
        ),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-ready",
    )

    captured = capsys.readouterr()
    assert out == 0
    assert len(awaited) == 1
    assert awaited[0].channel == "file"
    assert awaited[0].message_ts == "issue-83"
    assert "[BATMAN-AWAITING-APPROVAL]" in captured.out
    assert "channel=file" in captured.out
    assert reports and reports[0].reason == "approval_timeout"


# ---------------------------------------------------------------------------
# Issue #115: idempotent approval state.
# ---------------------------------------------------------------------------


def test_has_pending_approval_label_detects_dict_shape(monkeypatch):
    runner = _load_runner()
    issue = {"labels": [{"name": "agent:large-feature"}, {"name": "agent:plan-pending-approval"}]}
    assert runner._has_pending_approval_label(issue) is True


def test_has_pending_approval_label_handles_string_shape(monkeypatch):
    runner = _load_runner()
    issue = {"labels": ["agent:large-feature", "agent:plan-pending-approval"]}
    assert runner._has_pending_approval_label(issue) is True


def test_has_pending_approval_label_returns_false_when_absent(monkeypatch):
    runner = _load_runner()
    issue = {"labels": [{"name": "agent:large-feature"}]}
    assert runner._has_pending_approval_label(issue) is False


def test_pending_envelope_roundtrip(monkeypatch, tmp_path):
    """Saving then loading the envelope yields back the same channel+ts."""
    runner = _load_runner()
    import batman as bm

    plan = bm.parse_parent_issue(
        body="Repos:\n- myorg/backend\n\nChildren:\n- backend: scope\n",
        title="Bundle: t",
        parent_repo="myorg/parent",
        parent_issue_number=42,
        bundle_slug_prefix="",
    )
    env = bm.ApprovalEnvelope(channel="C0LIVE", message_ts="1700000000.123", plan=plan)
    runner._save_pending_envelope("myorg/parent", 42, env, firing_id="fid-1")
    loaded = runner._load_pending_envelope("myorg/parent", 42, plan=plan)
    assert loaded is not None
    assert loaded.channel == "C0LIVE"
    assert loaded.message_ts == "1700000000.123"


def test_pending_envelope_aged_out(monkeypatch, tmp_path):
    """An envelope older than ALFRED_BATMAN_APPROVAL_MAX_AGE_HOURS must
    re-draft, not resume - so an abandoned plan post doesn't hold a
    parent issue hostage forever."""
    runner = _load_runner()
    import json
    from datetime import datetime, timedelta

    import batman as bm

    plan = bm.parse_parent_issue(
        body="Repos:\n- myorg/backend\n\nChildren:\n- backend: scope\n",
        title="Bundle: t",
        parent_repo="myorg/parent",
        parent_issue_number=99,
        bundle_slug_prefix="",
    )
    path = runner._pending_approval_path("myorg/parent", 99)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "channel_id": "C0OLD",
                "message_ts": "1690000000.000",
                "posted_at": (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
                "firing_id": "fid-old",
                "parent_repo": "myorg/parent",
                "parent_issue": 99,
                "bundle_slug": "t",
            }
        )
    )
    monkeypatch.setenv("ALFRED_BATMAN_APPROVAL_MAX_AGE_HOURS", "24")
    out = runner._load_pending_envelope("myorg/parent", 99, plan=plan)
    assert out is None


def test_pending_envelope_clear_is_idempotent(monkeypatch, tmp_path):
    """Clearing an absent state file must not raise."""
    runner = _load_runner()
    # Should be a no-op the first time and the second time.
    runner._clear_pending_envelope("myorg/nope", 1)
    runner._clear_pending_envelope("myorg/nope", 1)


def test_label_set_and_unset_are_best_effort(monkeypatch):
    """``gh_issue_edit`` failures must not crash the firing - label
    management is operator-visible but secondary to the firing's
    primary path (post + poll + execute)."""
    runner = _load_runner()

    def boom(*a, **kw):
        raise RuntimeError("gh down")

    monkeypatch.setattr(runner, "gh_issue_edit", boom)
    runner._set_pending_approval_label("myorg/p", 1)  # must not raise
    runner._unset_pending_approval_label("myorg/p", 1)  # must not raise
