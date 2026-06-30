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
    monkeypatch.setattr(runner, "preflight", lambda *_a, **_kw: pytest.fail("no preflight"))
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_kw: pytest.fail("no lock"))
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


def test_main_parent_repo_does_not_require_gh_org(monkeypatch, capsys):
    runner = _load_runner()
    specs = []

    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_kw: True)
    monkeypatch.setattr(runner, "preflight", lambda spec: specs.append(spec))
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        runner.BatmanLifecycleConfig,
        "from_env",
        classmethod(lambda _cls: runner.BatmanLifecycleConfig(parent_repo="myorg/specs")),
    )
    monkeypatch.setattr(runner, "_list_parent_repo_large_features", lambda _repo: [])

    assert runner.main() == 0
    assert specs and "GH_ORG" not in specs[0].env_vars
    assert "no eligible agent:large-feature issues in myorg/specs" in capsys.readouterr().out


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


def test_lifecycle_finalizes_parent_after_full_child_fanout(monkeypatch, capsys):
    runner = _load_runner()
    edits = []
    closes = []
    ensured = []
    order = []
    reports = []
    plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(SimpleNamespace(repo="myorg/backend"),),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )
    result = SimpleNamespace(
        executed=True,
        reason=runner.EXEC_OK,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
            return result

        def report(self, _plan, reported):
            order.append("report")
            reports.append(reported)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda repo, number, **kw: (
            order.append("label") or edits.append((repo, number, kw)) or True
        ),
    )
    monkeypatch.setattr(
        runner,
        "ensure_labels",
        lambda repo, labels=None: order.append("ensure") or ensured.append((repo, labels)),
    )
    monkeypatch.setattr(
        runner,
        "_close_parent_issue",
        lambda repo, number: (
            order.append("close")
            or closes.append((repo, number))
            or (True, f"{repo}#{number} closed")
        ),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-ready",
    )

    captured = capsys.readouterr()
    assert out == 0
    assert order == ["ensure", "label", "close", "report"]
    assert reports == [result]
    assert ensured == [("myorg/parent", runner.LIFECYCLE_LABELS)]
    assert edits == [("myorg/parent", 83, {"add_labels": ["agent:done"]})]
    assert closes == [("myorg/parent", 83)]
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)
    assert "[BATMAN-PARENT-DONE]" in captured.out
    assert "[BATMAN-PARENT-CLOSED]" in captured.out


def test_lifecycle_keeps_completed_marker_when_parent_finalization_fails(monkeypatch, capsys):
    runner = _load_runner()
    edits = []
    closes = []
    ensured = []
    reports = []
    plan = SimpleNamespace(
        bundle_slug="complete-plan",
        children=(SimpleNamespace(repo="myorg/backend"),),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )
    result = SimpleNamespace(
        executed=True,
        reason=runner.EXEC_OK,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
            return result

        def report(self, _plan, _reported):
            reports.append(runner._completed_fanout_marker_state("myorg/parent", 83))

    def fail_label(repo, number, **kw):
        edits.append((repo, number, kw))
        raise RuntimeError("github unavailable")

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(
        runner,
        "ensure_labels",
        lambda repo, labels=None: ensured.append((repo, labels)),
    )
    monkeypatch.setattr(runner, "gh_issue_edit", fail_label)
    monkeypatch.setattr(
        runner,
        "_close_parent_issue",
        lambda repo, number: closes.append((repo, number)) or (True, f"{repo}#{number} closed"),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-finalize-fail",
    )

    captured = capsys.readouterr()
    assert out == 1
    assert reports == ["completed"]
    assert ensured == [("myorg/parent", runner.LIFECYCLE_LABELS)]
    assert edits == [("myorg/parent", 83, {"add_labels": ["agent:done"]})]
    assert closes == []
    assert runner._has_completed_fanout_marker("myorg/parent", 83)
    assert runner._completed_fanout_marker_state("myorg/parent", 83) == "completed"
    assert "[BATMAN-PARENT-DONE-WARN]" in captured.err


def test_lifecycle_completed_marker_uses_executed_children(monkeypatch):
    runner = _load_runner()
    plan = SimpleNamespace(
        bundle_slug="complete-plan",
        children=(
            SimpleNamespace(
                labels=("agent:bundle:complete-plan",),
                repo="myorg/backend",
                title="Implement backend slice",
            ),
            SimpleNamespace(
                labels=("agent:bundle:complete-plan",),
                repo="myorg/mobile",
                title="Implement mobile slice",
            ),
        ),
        affected_repos=("myorg/backend", "myorg/mobile"),
        readiness_blockers=(),
    )
    executed_child = SimpleNamespace(
        labels=("agent:bundle:complete-plan",),
        repo="myorg/backend",
        title="Implement backend slice",
    )
    result = SimpleNamespace(
        children=(executed_child,),
        executed=True,
        reason=runner.EXEC_OK,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            return result

        def report(self, _plan, _reported):
            pass

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "ensure_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("github unavailable")),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-executed-children",
    )

    payload = runner._completed_fanout_marker_payload("myorg/parent", 83)
    assert out == 1
    assert payload is not None
    assert payload["children"] == [
        {
            "labels": ["agent:bundle:complete-plan"],
            "repo": "myorg/backend",
            "title": "Implement backend slice",
        }
    ]


def test_completed_fanout_marker_retries_finalize_without_requeue(monkeypatch, capsys):
    runner = _load_runner()
    finalize_calls = []
    rows = [
        {
            "number": 83,
            "title": "ready",
            "url": "https://github.com/myorg/parent/issues/83",
            "labels": [{"name": runner.LARGE_FEATURE_LABEL}],
            "createdAt": "2026-06-01T00:00:00Z",
            "body": "Bundle: ready",
        }
    ]

    assert runner._save_completed_fanout_marker(
        "myorg/parent",
        83,
        firing_id="fid-done",
        reason=runner.EXEC_OK,
        state="completed",
    )

    monkeypatch.setattr(runner, "gh_json", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        runner,
        "_finalize_parent_after_child_fanout",
        lambda repo, number: finalize_calls.append((repo, number)) or True,
    )

    eligible = runner._list_parent_repo_large_features("myorg/parent")

    captured = capsys.readouterr()
    assert eligible == []
    assert finalize_calls == [("myorg/parent", 83)]
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)
    assert "[BATMAN-PARENT-FINALIZE-RETRY]" in captured.out


def test_executing_fanout_marker_skips_refanout_without_finalize(monkeypatch, capsys):
    runner = _load_runner()
    finalize_calls = []
    rows = [
        {
            "number": 83,
            "title": "ready",
            "url": "https://github.com/myorg/parent/issues/83",
            "labels": [{"name": runner.LARGE_FEATURE_LABEL}],
            "createdAt": "2026-06-01T00:00:00Z",
            "body": "Bundle: ready",
        }
    ]

    assert runner._save_completed_fanout_marker(
        "myorg/parent",
        83,
        firing_id="fid-executing",
        reason="fanout-started",
        state="executing",
    )

    monkeypatch.setattr(runner, "gh_json", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(
        runner,
        "_finalize_parent_after_child_fanout",
        lambda repo, number: finalize_calls.append((repo, number)) or True,
    )

    eligible = runner._list_parent_repo_large_features("myorg/parent")

    captured = capsys.readouterr()
    assert eligible == []
    assert finalize_calls == []
    assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
    assert "state=executing; skipping re-fanout" in captured.err


def test_stale_executing_fanout_marker_clears_and_requeues(monkeypatch, capsys):
    runner = _load_runner()
    finalize_calls = []
    rows = [
        {
            "number": 83,
            "title": "ready",
            "url": "https://github.com/myorg/parent/issues/83",
            "labels": [{"name": runner.LARGE_FEATURE_LABEL}],
            "createdAt": "2026-06-01T00:00:00Z",
            "body": "Bundle: ready",
        }
    ]

    monkeypatch.setenv(runner.ENV_EXECUTING_FANOUT_STALE_AFTER_S, "0")
    assert runner._save_completed_fanout_marker(
        "myorg/parent",
        83,
        firing_id="fid-executing",
        reason="fanout-started",
        state="executing",
        children=[
            {
                "labels": ["agent:bundle:ready-plan"],
                "repo": "myorg/backend",
                "title": "Implement backend slice",
            }
        ],
    )

    def fake_gh_json(cmd, **_kwargs):
        repo = cmd[cmd.index("-R") + 1]
        if repo == "myorg/parent":
            return rows
        assert repo == "myorg/backend"
        return []

    monkeypatch.setattr(runner, "gh_json", fake_gh_json)
    monkeypatch.setattr(
        runner,
        "_finalize_parent_after_child_fanout",
        lambda repo, number: finalize_calls.append((repo, number)) or True,
    )

    eligible = runner._list_parent_repo_large_features("myorg/parent")

    captured = capsys.readouterr()
    assert eligible == rows
    assert finalize_calls == []
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)
    assert "[BATMAN-PARENT-FANOUT-MARKER-STALE]" in captured.err


def test_executing_fanout_marker_recovers_when_all_children_exist(monkeypatch, capsys):
    runner = _load_runner()
    finalize_calls = []
    rows = [
        {
            "number": 83,
            "title": "ready",
            "url": "https://github.com/myorg/parent/issues/83",
            "labels": [{"name": runner.LARGE_FEATURE_LABEL}],
            "createdAt": "2026-06-01T00:00:00Z",
            "body": "Bundle: ready",
        }
    ]

    assert runner._save_completed_fanout_marker(
        "myorg/parent",
        83,
        firing_id="fid-executing",
        reason="fanout-started",
        state="executing",
        children=[
            {
                "labels": ["agent:bundle:ready-plan"],
                "repo": "myorg/backend",
                "title": "Implement backend slice",
            }
        ],
    )

    def fake_gh_json(cmd, **_kwargs):
        repo = cmd[cmd.index("-R") + 1]
        if repo == "myorg/parent":
            return rows
        assert repo == "myorg/backend"
        assert '"Implement backend slice" in:title' in cmd
        assert "agent:bundle:ready-plan" in cmd
        return [
            {
                "body": (
                    "## Parent\n\n"
                    "- Parent issue: [myorg/parent#83]"
                    "(https://github.com/myorg/parent/issues/83)\n"
                ),
                "title": "Implement backend slice",
                "url": "https://github.com/myorg/backend/issues/44",
            }
        ]

    monkeypatch.setattr(runner, "gh_json", fake_gh_json)
    monkeypatch.setattr(
        runner,
        "_finalize_parent_after_child_fanout",
        lambda repo, number: finalize_calls.append((repo, number)) or True,
    )

    eligible = runner._list_parent_repo_large_features("myorg/parent")

    captured = capsys.readouterr()
    assert eligible == []
    assert finalize_calls == [("myorg/parent", 83)]
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)
    assert "[BATMAN-PARENT-FINALIZE-RECOVER]" in captured.out


def test_executing_fanout_marker_does_not_recover_from_old_child(monkeypatch, capsys):
    runner = _load_runner()
    finalize_calls = []
    rows = [
        {
            "number": 83,
            "title": "ready",
            "url": "https://github.com/myorg/parent/issues/83",
            "labels": [{"name": runner.LARGE_FEATURE_LABEL}],
            "createdAt": "2026-06-01T00:00:00Z",
            "body": "Bundle: ready",
        }
    ]

    assert runner._save_completed_fanout_marker(
        "myorg/parent",
        83,
        firing_id="fid-executing",
        reason="fanout-started",
        state="executing",
        children=[
            {
                "labels": ["agent:bundle:ready-plan"],
                "repo": "myorg/backend",
                "title": "Implement backend slice",
            }
        ],
    )

    def fake_gh_json(cmd, **_kwargs):
        repo = cmd[cmd.index("-R") + 1]
        if repo == "myorg/parent":
            return rows
        assert repo == "myorg/backend"
        return [
            {
                "body": (
                    "## Parent\n\n"
                    "- Parent issue: [myorg/parent#12]"
                    "(https://github.com/myorg/parent/issues/12)\n"
                ),
                "title": "Implement backend slice",
                "url": "https://github.com/myorg/backend/issues/44",
            }
        ]

    monkeypatch.setattr(runner, "gh_json", fake_gh_json)
    monkeypatch.setattr(
        runner,
        "_finalize_parent_after_child_fanout",
        lambda repo, number: finalize_calls.append((repo, number)) or True,
    )

    eligible = runner._list_parent_repo_large_features("myorg/parent")

    captured = capsys.readouterr()
    assert eligible == []
    assert finalize_calls == []
    assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
    assert "state=executing; skipping re-fanout" in captured.err


def test_lifecycle_leaves_parent_open_after_partial_child_fanout(monkeypatch):
    runner = _load_runner()
    from batman import EXEC_PARTIAL

    edits = []
    closes = []
    plan = SimpleNamespace(
        bundle_slug="partial-plan",
        children=(SimpleNamespace(repo="myorg/backend"), SimpleNamespace(repo="myorg/frontend")),
        affected_repos=("myorg/backend", "myorg/frontend"),
        readiness_blockers=(),
    )
    result = SimpleNamespace(
        executed=True,
        reason=EXEC_PARTIAL,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=("myorg/frontend",),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
            return result

        def report(self, _plan, _reported):
            pass

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda repo, number, **kw: edits.append((repo, number, kw)) or True,
    )
    monkeypatch.setattr(
        runner,
        "_close_parent_issue",
        lambda repo, number: closes.append((repo, number)) or (True, f"{repo}#{number} closed"),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-partial",
    )

    assert out == 0
    assert edits == []
    assert closes == []
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)


def test_lifecycle_aborts_before_fanout_when_marker_save_fails(monkeypatch):
    runner = _load_runner()
    reports = []
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
            return None

        def execute(self, _plan):
            raise AssertionError("fanout should not run without a durable marker")

        def report(self, _plan, reported):
            reports.append(reported.reason)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "_save_completed_fanout_marker", lambda *_args, **_kwargs: False)

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-marker-fails",
    )

    assert out == 1
    assert reports == ["failure-parent-fanout-marker-failed"]


def test_lifecycle_preserves_executing_marker_when_fanout_raises(monkeypatch, capsys):
    runner = _load_runner()
    reports = []
    plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/backend",
                title="Implement backend slice",
            ),
        ),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
            raise RuntimeError("fanout crashed")

        def report(self, _plan, reported):
            reports.append(reported)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())

    with pytest.raises(RuntimeError, match="fanout crashed"):
        runner._run_lifecycle(
            config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
            parent_issue={"number": 83, "title": "ready", "body": ""},
            firing_id="fid-fanout-crash",
        )

    assert reports == []
    captured = capsys.readouterr()
    assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
    assert "[BATMAN-FANOUT-CRASH-MARKER-KEPT]" in captured.err


def test_lifecycle_executing_marker_uses_execution_plan(monkeypatch):
    runner = _load_runner()
    from batman import EXEC_PARTIAL

    reports = []
    original_plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/backend",
                title="Implement backend slice",
            ),
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/mobile",
                title="Implement mobile slice",
            ),
        ),
        affected_repos=("myorg/backend", "myorg/mobile"),
        readiness_blockers=(),
    )
    execution_plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/backend",
                title="Implement backend slice",
            ),
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/admin",
                title="Implement admin slice",
            ),
        ),
        affected_repos=("myorg/backend", "myorg/admin"),
        readiness_blockers=(),
    )
    result = SimpleNamespace(
        executed=True,
        reason=EXEC_PARTIAL,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=("myorg/admin",),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return original_plan

        def request_approval(self, _plan):
            return None

        def execution_plan(self, plan):
            assert plan is original_plan
            return execution_plan

        def execute(self, plan):
            assert plan is execution_plan
            payload = runner._completed_fanout_marker_payload("myorg/parent", 83)
            assert payload is not None
            assert payload["children"] == [
                {
                    "labels": ["agent:bundle:ready-plan"],
                    "repo": "myorg/backend",
                    "title": "Implement backend slice",
                },
                {
                    "labels": ["agent:bundle:ready-plan"],
                    "repo": "myorg/admin",
                    "title": "Implement admin slice",
                },
            ]
            return result

        def report(self, plan, reported):
            reports.append((plan, reported))

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-execution-plan",
    )

    assert out == 0
    assert reports == [(execution_plan, result)]
    assert not runner._has_completed_fanout_marker("myorg/parent", 83)


def test_lifecycle_warns_when_completed_marker_upgrade_fails(monkeypatch, capsys):
    runner = _load_runner()
    real_save = runner._save_completed_fanout_marker
    reports = []
    plan = SimpleNamespace(
        bundle_slug="ready-plan",
        children=(
            SimpleNamespace(
                labels=("agent:bundle:ready-plan",),
                repo="myorg/backend",
                title="Implement backend slice",
            ),
        ),
        affected_repos=("myorg/backend",),
        readiness_blockers=(),
    )
    result = SimpleNamespace(
        executed=True,
        reason=runner.EXEC_OK,
        created_issue_urls=("https://github.com/myorg/backend/issues/44",),
        failed_repos=(),
    )

    class FakeLifecycle:
        def __init__(self, **_kwargs):
            pass

        def plan(self, **_kwargs):
            return plan

        def request_approval(self, _plan):
            return None

        def execute(self, _plan):
            return result

        def report(self, _plan, _reported):
            reports.append(runner._completed_fanout_marker_state("myorg/parent", 83))

    def flaky_save(*args, **kwargs):
        if kwargs["state"] == "completed":
            return False
        return real_save(*args, **kwargs)

    monkeypatch.setattr(runner, "BatmanLifecycle", FakeLifecycle)
    monkeypatch.setattr(runner, "SlackReporter", lambda **_kwargs: object())
    monkeypatch.setattr(runner, "_save_completed_fanout_marker", flaky_save)
    monkeypatch.setattr(runner, "ensure_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("github unavailable")),
    )

    out = runner._run_lifecycle(
        config=runner.BatmanLifecycleConfig(parent_repo="myorg/parent", auto_execute="1"),
        parent_issue={"number": 83, "title": "ready", "body": ""},
        firing_id="fid-upgrade-fails",
    )

    captured = capsys.readouterr()
    assert out == 1
    assert reports == ["executing"]
    assert runner._completed_fanout_marker_state("myorg/parent", 83) == "executing"
    assert "[BATMAN-COMPLETED-FANOUT-UPGRADE-WARN]" in captured.err


def test_finalize_parent_treats_close_failure_as_best_effort(monkeypatch, capsys):
    runner = _load_runner()
    edits = []
    closes = []

    monkeypatch.setattr(runner, "ensure_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "gh_issue_edit",
        lambda repo, number, **kw: edits.append((repo, number, kw)) or True,
    )
    monkeypatch.setattr(
        runner,
        "_close_parent_issue",
        lambda repo, number: closes.append((repo, number)) or (False, "close refused"),
    )

    ok = runner._finalize_parent_after_child_fanout("myorg/parent", 83)

    captured = capsys.readouterr()
    assert ok is True
    assert edits == [("myorg/parent", 83, {"add_labels": ["agent:done"]})]
    assert closes == [("myorg/parent", 83)]
    assert "[BATMAN-PARENT-DONE]" in captured.out
    assert "[BATMAN-PARENT-CLOSE-WARN]" in captured.err


def test_close_parent_issue_uses_configured_parent_repo(monkeypatch):
    runner = _load_runner()
    calls = []

    monkeypatch.setattr(runner, "is_dry_run", lambda: False)

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(runner, "run", fake_run)

    ok, detail = runner._close_parent_issue("myorg/specs", 83)

    assert ok is True
    assert detail == "myorg/specs#83 closed"
    assert calls == [
        (
            ["gh", "issue", "close", "83", "-R", "myorg/specs"],
            {"timeout": 30},
        )
    ]


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
