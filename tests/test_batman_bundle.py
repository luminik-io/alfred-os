"""Tests for ``lib/batman.py``, bundle primitives + plan parsing.

The pure-data helpers (Bundle, PlanShape, parse_plan_from_issue,
parse_plan_from_bundle) are deterministic and tested directly. The
network-touching helpers (claim_bundle / release_bundle) are tested
via monkeypatched claim_issue / release_issue so the suite stays
offline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod == "batman":
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    yield


def _issue(num, repo="backend", body="", title="t", created="2026-05-09T10:00:00Z"):
    return {
        "number": num,
        "title": title,
        "url": f"https://github.com/myorg/{repo}/issues/{num}",
        "labels": [],
        "createdAt": created,
        "body": body,
    }


# ---------------------------------------------------------------------------
# Bundle dataclass
# ---------------------------------------------------------------------------


def test_bundle_primary_issue_is_oldest_by_created_at():
    import batman as bm

    b = bm.Bundle(
        issues=[
            _issue(2, "frontend", created="2026-05-09T11:00:00Z"),
            _issue(1, "backend", created="2026-05-09T10:00:00Z"),
            _issue(3, "mobile", created="2026-05-09T12:00:00Z"),
        ],
        bundle_label="agent:bundle:auth-rework",
    )
    assert b.primary_issue["number"] == 1


def test_bundle_slug_uses_label_when_present():
    import batman as bm

    b = bm.Bundle(
        issues=[_issue(1)],
        bundle_label="agent:bundle:auth-rework",
    )
    assert b.slug == "auth-rework"


def test_bundle_slug_falls_back_to_repo_number_for_solo_bundle():
    import batman as bm

    b = bm.Bundle(issues=[_issue(275, "backend")], bundle_label=None)
    assert b.slug == "backend-275"


# ---------------------------------------------------------------------------
# parse_plan_from_issue
# ---------------------------------------------------------------------------


def test_parse_plan_inline_repos_line():
    import batman as bm

    plan = bm.parse_plan_from_issue("Repos: backend, frontend\nBlah blah")
    assert plan.affected_repos == ["backend", "frontend"]


def test_parse_plan_h2_block_with_bullets():
    import batman as bm

    body = "## Affected Repos\n- backend\n- frontend\n\n## Other\nstuff"
    plan = bm.parse_plan_from_issue(body)
    assert plan.affected_repos == ["backend", "frontend"]


def test_parse_plan_h2_block_with_comma_separated_payload():
    """PR #121 fix: bare comma-separated payload after the H2 header
    must parse, not silently fall back to the default rollout."""
    import batman as bm

    body = "## Affected Repos\nbackend, frontend\n"
    plan = bm.parse_plan_from_issue(body)
    assert "backend" in plan.affected_repos
    assert "frontend" in plan.affected_repos
    assert "mobile" not in plan.affected_repos


def test_parse_plan_h2_rollout_order_block():
    import batman as bm

    body = "## Affected Repos\n- backend\n- frontend\n## Rollout Order\n- frontend\n- backend\n"
    plan = bm.parse_plan_from_issue(body)
    assert plan.affected_repos == ["frontend", "backend"]


def test_parse_plan_rollout_does_not_split_hyphenated_names():
    """Repo names like ``data-acquisition`` contain hyphens; the
    splitter must not treat ``-`` as a separator, otherwise the name
    silently disappears."""
    import batman as bm

    body = "Rollout order: backend > data-acquisition > frontend"
    plan = bm.parse_plan_from_issue(body)
    assert "data-acquisition" in plan.affected_repos


def test_parse_plan_acceptance_criteria_per_repo():
    import batman as bm

    body = (
        "## Affected Repos\n- backend\n- frontend\n"
        "## Acceptance Criteria\n"
        "### backend\nDo backend thing\n"
        "### frontend\nDo frontend thing\n"
    )
    plan = bm.parse_plan_from_issue(body)
    assert plan.repo_criteria["backend"].startswith("Do backend")
    assert plan.repo_criteria["frontend"].startswith("Do frontend")


def test_parse_plan_explicit_list_wins_over_stray_h3():
    """PR #121 scope-widening guard: when an explicit Affected Repos
    list is present, a stray H3 in the criteria block must NOT be
    appended to the affected set."""
    import batman as bm

    body = (
        "## Affected Repos\n- backend\n"
        "## Acceptance Criteria\n"
        "### backend\nDo backend\n"
        "### frontend\nLeftover from a previous draft\n"
    )
    plan = bm.parse_plan_from_issue(body)
    assert plan.affected_repos == ["backend"]
    # The stray H3 is still parsed into the criteria map (so the
    # caller can spot it), but it does NOT widen the affected set.
    assert "frontend" in plan.repo_criteria


def test_parse_plan_backfills_affected_when_only_h3_present():
    import batman as bm

    body = "## Acceptance Criteria\n### backend\nDo backend\n### frontend\nDo frontend\n"
    plan = bm.parse_plan_from_issue(body)
    assert "backend" in plan.affected_repos
    assert "frontend" in plan.affected_repos


def test_parse_plan_falls_back_to_default_rollout_on_empty_body():
    import batman as bm

    plan = bm.parse_plan_from_issue("")
    # Empty body → first three from default rollout order.
    assert plan.affected_repos == bm.DEFAULT_ROLLOUT_ORDER[:3]


# ---------------------------------------------------------------------------
# parse_plan_from_bundle
# ---------------------------------------------------------------------------


def test_parse_plan_from_bundle_solo_delegates_to_issue_parser():
    import batman as bm

    body = "## Affected Repos\n- backend\n- frontend\n"
    bundle = bm.Bundle(issues=[_issue(10, body=body)], bundle_label=None)
    plan = bm.parse_plan_from_bundle(bundle)
    assert plan.affected_repos == ["backend", "frontend"]


def test_parse_plan_from_bundle_multi_uses_per_issue_repo():
    """Multi-issue bundle: each issue's repo IS its affected repo."""
    import batman as bm

    bundle = bm.Bundle(
        issues=[
            _issue(1, "backend", body="Do the backend thing"),
            _issue(2, "frontend", body="Do the frontend thing"),
            _issue(3, "mobile", body="Do the mobile thing"),
        ],
        bundle_label="agent:bundle:auth-rework",
    )
    plan = bm.parse_plan_from_bundle(bundle)
    # Default rollout order respected: backend before frontend before mobile.
    assert plan.affected_repos == ["backend", "frontend", "mobile"]
    assert plan.repo_criteria["backend"] == "Do the backend thing"
    assert plan.repo_criteria["frontend"] == "Do the frontend thing"


# ---------------------------------------------------------------------------
# claim_bundle / release_bundle (monkeypatched, no network)
# ---------------------------------------------------------------------------


def test_claim_bundle_all_or_nothing_releases_on_failure(monkeypatch):
    import agent_runner as ar
    import batman as bm

    issues = [
        _issue(1, "backend"),
        _issue(2, "frontend"),
        _issue(3, "mobile"),
    ]
    bundle = bm.Bundle(issues=issues, bundle_label="agent:bundle:auth-rework")

    claim_calls: list[tuple[str, int]] = []
    release_calls: list[tuple[str, int, str]] = []

    def fake_claim(repo_slug, num, *, codename, firing_id):
        claim_calls.append((repo_slug, num))
        # Fail on the third issue so the rollback path runs.
        return num != 3

    def fake_release(repo_slug, num, *, codename, firing_id, outcome, transition_to=None):
        release_calls.append((repo_slug, num, outcome))
        return True

    monkeypatch.setattr(ar, "claim_issue", fake_claim)
    monkeypatch.setattr(ar, "release_issue", fake_release)
    # batman.py imported its own references at import time, patch them
    # too so the monkeypatch takes effect inside claim_bundle.
    monkeypatch.setattr(bm, "claim_issue", fake_claim)
    monkeypatch.setattr(bm, "release_issue", fake_release)

    ok = bm.claim_bundle(bundle, codename="batman", firing_id="f-1")
    assert ok is False
    # Tried to claim all three.
    assert {(r, n) for r, n in claim_calls} == {("backend", 1), ("frontend", 2), ("mobile", 3)}
    # Rolled back the two that succeeded.
    rolled_back = {(r, n) for r, n, _ in release_calls}
    assert rolled_back == {("backend", 1), ("frontend", 2)}
    # Outcome string identifies the rollback path.
    assert all(o == "bundle-claim-rolled-back" for _, _, o in release_calls)


def test_claim_bundle_succeeds_when_all_claims_succeed(monkeypatch):
    import batman as bm

    bundle = bm.Bundle(
        issues=[_issue(1, "backend"), _issue(2, "frontend")],
        bundle_label="agent:bundle:auth-rework",
    )

    monkeypatch.setattr(bm, "claim_issue", lambda *a, **kw: True)
    released: list = []
    monkeypatch.setattr(bm, "release_issue", lambda *a, **kw: released.append((a, kw)) or True)

    ok = bm.claim_bundle(bundle, codename="batman", firing_id="f-1")
    assert ok is True
    # No release_issue calls when every claim succeeded.
    assert released == []


def test_release_bundle_continues_past_per_issue_failures(monkeypatch):
    import batman as bm

    bundle = bm.Bundle(
        issues=[
            _issue(1, "backend"),
            _issue(2, "frontend"),
            _issue(3, "mobile"),
        ],
        bundle_label="agent:bundle:auth-rework",
    )

    calls: list = []

    def flaky_release(repo_slug, num, **kw):
        calls.append((repo_slug, num))
        if num == 2:
            raise RuntimeError("network blip")
        return True

    monkeypatch.setattr(bm, "release_issue", flaky_release)

    # Must not raise; must hit all three issues despite the flake.
    bm.release_bundle(bundle, codename="batman", firing_id="f-1", outcome="ok")
    assert {(r, n) for r, n in calls} == {("backend", 1), ("frontend", 2), ("mobile", 3)}


def test_gh_repo_from_url_filters_cross_org():
    import batman as bm

    assert bm._gh_repo_from_url("https://github.com/myorg/backend/issues/1") == "backend"
    # Cross-org URL → None (Batman never claims issues outside the configured org).
    assert bm._gh_repo_from_url("https://github.com/otherorg/backend/issues/1") is None
    assert bm._gh_repo_from_url("") is None
    assert bm._gh_repo_from_url("not a url") is None


# ---------------------------------------------------------------------------
# Issue picker: BATMAN_SCAN_REPOS gate (Codex P1 on alfred-os PR #15)
# ---------------------------------------------------------------------------


def _import_batman_runner():
    """Re-import bin/batman.py with the current env so module-level
    constants (GH_ORG, etc.) re-evaluate against the test fixtures.

    bin/batman.py does ``from batman import …`` referring to
    lib/batman.py; pre-import the lib module under that name so the
    runner's import resolves correctly even when we exec_module it
    under a different sys.modules key.
    """
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent
    lib_dir = repo_root / "lib"
    bin_dir = repo_root / "bin"
    for path in (str(lib_dir), str(bin_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod in {"batman", "batman_runner"}:
            del sys.modules[mod]
    lib_spec = importlib.util.spec_from_file_location("batman", lib_dir / "batman.py")
    lib_module = importlib.util.module_from_spec(lib_spec)
    sys.modules["batman"] = lib_module
    lib_spec.loader.exec_module(lib_module)
    bin_spec = importlib.util.spec_from_file_location("batman_runner", bin_dir / "batman.py")
    bin_module = importlib.util.module_from_spec(bin_spec)
    sys.modules["batman_runner"] = bin_module
    bin_spec.loader.exec_module(bin_module)
    return bin_module


def test_list_large_features_returns_empty_when_scan_repos_unset(monkeypatch):
    """No BATMAN_SCAN_REPOS = no work. The previous implementation
    would have searched org-wide once batman was enabled, drafting
    plans for repos outside the operator-configured scope."""
    monkeypatch.delenv("BATMAN_SCAN_REPOS", raising=False)
    monkeypatch.setenv("GH_ORG", "myorg")
    runner = _import_batman_runner()
    # Even if gh search would have returned issues, the early return
    # short-circuits before any subprocess call.
    monkeypatch.setattr(
        runner,
        "gh_json",
        lambda *_a, **_k: [
            {"number": 1, "url": "https://github.com/myorg/x/issues/1", "labels": []}
        ],
    )
    assert runner._list_large_features() == []


def test_list_large_features_filters_to_scan_repos(monkeypatch):
    """Org-wide gh search results get post-filtered against the URL
    prefix derived from BATMAN_SCAN_REPOS, issues in repos outside
    the scan list are dropped silently."""
    monkeypatch.setenv("BATMAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setenv("GH_ORG", "myorg")
    runner = _import_batman_runner()
    fake_rows = [
        {"number": 10, "url": "https://github.com/myorg/backend/issues/10", "labels": []},
        {"number": 20, "url": "https://github.com/myorg/frontend/issues/20", "labels": []},
        {"number": 30, "url": "https://github.com/myorg/random-repo/issues/30", "labels": []},
        {"number": 40, "url": "https://github.com/myorg/mobile/issues/40", "labels": []},
    ]
    monkeypatch.setattr(runner, "gh_json", lambda *_a, **_k: fake_rows)
    out = runner._list_large_features()
    nums = sorted(r["number"] for r in out)
    assert nums == [10, 20], f"only backend + frontend should pass; got {nums}"


def test_list_large_features_skip_labels_still_apply(monkeypatch):
    """Skip-labels filter is applied AFTER the scan-repo gate, so a
    blocked issue inside a scan repo still gets dropped."""
    monkeypatch.setenv("BATMAN_SCAN_REPOS", "backend")
    monkeypatch.setenv("GH_ORG", "myorg")
    runner = _import_batman_runner()
    fake_rows = [
        {
            "number": 1,
            "url": "https://github.com/myorg/backend/issues/1",
            "labels": [{"name": "agent:large-feature"}],
        },
        {
            "number": 2,
            "url": "https://github.com/myorg/backend/issues/2",
            "labels": [{"name": "agent:large-feature"}, {"name": "do-not-pickup"}],
        },
        {
            "number": 3,
            "url": "https://github.com/myorg/backend/issues/3",
            "labels": [{"name": "agent:large-feature"}, {"name": "agent:in-flight"}],
        },
    ]
    monkeypatch.setattr(runner, "gh_json", lambda *_a, **_k: fake_rows)
    out = runner._list_large_features()
    assert [r["number"] for r in out] == [1]
