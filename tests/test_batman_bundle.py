"""Tests for ``lib/batman.py`` — bundle primitives + plan parsing.

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
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
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

    body = (
        "## Affected Repos\n- backend\n- frontend\n"
        "## Rollout Order\n- frontend\n- backend\n"
    )
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

    body = (
        "## Acceptance Criteria\n"
        "### backend\nDo backend\n"
        "### frontend\nDo frontend\n"
    )
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
    # batman.py imported its own references at import time — patch them
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
    monkeypatch.setattr(
        bm, "release_issue", lambda *a, **kw: released.append((a, kw)) or True
    )

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
