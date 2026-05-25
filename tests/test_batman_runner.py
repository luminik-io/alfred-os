"""Tests for the ``bin/batman.py`` runner shell.

The heavy bundle and parser primitives live in ``lib/batman.py``. These
tests cover runner-only wiring that should stay offline and deterministic:
configured repo scoping for GitHub issue search.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC
from pathlib import Path

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


def test_list_large_features_returns_no_work_when_scan_repos_unset(monkeypatch):
    runner = _load_runner()
    calls: list[list[str]] = []

    def fake_gh_json(cmd, *, default):
        calls.append(cmd)
        return default

    monkeypatch.delenv("BATMAN_SCAN_REPOS", raising=False)
    monkeypatch.setattr(runner, "gh_json", fake_gh_json)

    assert runner._list_large_features() == []
    assert calls == []


def test_list_large_features_scopes_search_to_configured_repos(monkeypatch):
    runner = _load_runner()
    runner.GH_REPO_TO_LOCAL.update({"myorg-backend": "backend"})
    calls: list[list[str]] = []

    def fake_gh_json(cmd, *, default):
        calls.append(cmd)
        return [
            {
                "number": 1,
                "title": "eligible",
                "url": "https://github.com/myorg/myorg-backend/issues/1",
                "labels": [{"name": "agent:large-feature"}],
                "createdAt": "2026-05-09T10:00:00Z",
                "body": "",
            },
            {
                "number": 2,
                "title": "claimed",
                "url": "https://github.com/myorg/frontend/issues/2",
                "labels": [{"name": "agent:in-flight"}],
                "createdAt": "2026-05-09T10:00:00Z",
                "body": "",
            },
        ]

    monkeypatch.setenv("BATMAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setattr(runner, "gh_json", fake_gh_json)

    rows = runner._list_large_features()

    assert [row["number"] for row in rows] == [1]
    cmd = calls[0]
    assert "--owner" not in cmd
    assert cmd.count("--repo") == 2
    assert "myorg/myorg-backend" in cmd
    assert "myorg/frontend" in cmd


def test_bundle_for_issue_keeps_siblings_inside_scan_scope(monkeypatch):
    runner = _load_runner()
    seen_allowed: list[list[str]] = []

    issue = {
        "number": 1,
        "title": "bundle trigger",
        "url": "https://github.com/myorg/backend/issues/1",
        "labels": [{"name": "agent:bundle:checkout"}],
        "createdAt": "2026-05-09T10:00:00Z",
        "body": "",
    }

    def fake_list_issues_by_bundle_label(label, *, allowed_repos=None):
        seen_allowed.append(list(allowed_repos or []))
        return [
            issue,
            {
                "number": 2,
                "title": "frontend sibling",
                "url": "https://github.com/myorg/frontend/issues/2",
                "labels": [{"name": label}],
                "createdAt": "2026-05-09T10:01:00Z",
                "body": "",
            },
        ]

    monkeypatch.setenv("BATMAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setattr(runner, "list_issues_by_bundle_label", fake_list_issues_by_bundle_label)

    bundle = runner._bundle_for_issue(issue)

    assert bundle.bundle_label == "agent:bundle:checkout"
    assert seen_allowed == [["myorg/backend", "myorg/frontend"]]
    assert {row["number"] for row in bundle.issues} == {1, 2}


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
    re-draft, not resume — so an abandoned plan post doesn't hold a
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
    """``gh_issue_edit`` failures must not crash the firing — label
    management is operator-visible but secondary to the firing's
    primary path (post + poll + execute)."""
    runner = _load_runner()

    def boom(*a, **kw):
        raise RuntimeError("gh down")

    monkeypatch.setattr(runner, "gh_issue_edit", boom)
    runner._set_pending_approval_label("myorg/p", 1)  # must not raise
    runner._unset_pending_approval_label("myorg/p", 1)  # must not raise
