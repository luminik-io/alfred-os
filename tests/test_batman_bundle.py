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


def test_parse_plan_preserves_duplicate_explicit_repo_tails():
    import batman as bm

    body = "## Affected Repos\n- acme/backend\n- beta/backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["acme/backend", "beta/backend"]
    assert plan.repo_slugs == {
        "acme/backend": "acme/backend",
        "beta/backend": "beta/backend",
    }


def test_parse_plan_preserves_explicit_slug_with_repo_mapping(monkeypatch):
    import batman as bm

    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"other/service": "service"})

    body = "## Affected Repos\n- acme/backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["acme/backend"]
    assert plan.repo_slugs == {"acme/backend": "acme/backend"}


def test_parse_plan_bare_rollout_uses_explicit_affected_slug():
    import batman as bm

    body = "## Affected Repos\n- acme/backend\n\n## Rollout order\n- backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["acme/backend"]
    assert plan.repo_slugs == {"acme/backend": "acme/backend"}


def test_parse_plan_bare_rollout_uses_mapped_explicit_affected_slug(monkeypatch):
    import batman as bm

    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"acme/acme-backend": "backend"})

    body = "## Affected Repos\n- acme/acme-backend\n\n## Rollout order\n- backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["acme/acme-backend"]
    assert plan.repo_slugs == {"acme/acme-backend": "acme/acme-backend"}


def test_parse_plan_explicit_rollout_does_not_promote_bare_affected_repo():
    import batman as bm

    body = "## Affected Repos\n- backend\n\n## Rollout order\n- acme/backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["backend"]


def test_parse_plan_default_rollout_orders_explicit_affected_slugs(monkeypatch):
    import batman as bm

    monkeypatch.setattr(bm, "DEFAULT_ROLLOUT_ORDER", ["backend", "frontend", "mobile"])

    body = "## Affected Repos\n- acme/frontend\n- acme/backend\n"
    plan = bm.parse_plan_from_issue(body)

    assert plan.affected_repos == ["acme/backend", "acme/frontend"]
    assert plan.repo_slugs == {
        "acme/frontend": "acme/frontend",
        "acme/backend": "acme/backend",
    }


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
    assert plan.needs_scope_resolution is True
    assert plan.parse_notes


# ---------------------------------------------------------------------------
# parse_plan_from_bundle
# ---------------------------------------------------------------------------


def test_parse_plan_from_bundle_solo_delegates_to_issue_parser():
    import batman as bm

    body = "## Affected Repos\n- backend\n- frontend\n"
    bundle = bm.Bundle(issues=[_issue(10, body=body)], bundle_label=None)
    plan = bm.parse_plan_from_bundle(bundle)
    assert plan.affected_repos == ["backend", "frontend"]


def test_parse_plan_from_bundle_multi_uses_per_issue_repo_with_default_rollout():
    """Multi-issue bundle: each issue's repo IS its affected repo."""
    import batman as bm

    bundle = bm.Bundle(
        issues=[
            _issue(2, "frontend", body="Do the frontend thing"),
            _issue(1, "backend", body="Do the backend thing"),
            _issue(3, "mobile", body="Do the mobile thing"),
        ],
        bundle_label="agent:bundle:auth-rework",
    )
    plan = bm.parse_plan_from_bundle(bundle)
    assert plan.affected_repos == ["backend", "frontend", "mobile"]
    assert plan.repo_criteria["backend"] == "Do the backend thing"
    assert plan.repo_criteria["frontend"] == "Do the frontend thing"


def test_parse_plan_from_bundle_preserves_dependency_sorted_issue_order():
    import batman as bm

    bundle = bm.Bundle(
        issues=[
            _issue(3, "mobile", body="Do the mobile thing"),
            _issue(1, "backend", body="Do the backend thing"),
            _issue(
                2,
                "frontend",
                body="Depends on: mobile#3\n\nDo the frontend thing",
            ),
        ],
        bundle_label="agent:bundle:auth-rework",
    )
    plan = bm.parse_plan_from_bundle(bundle)
    assert plan.affected_repos == ["mobile", "backend", "frontend"]


# ---------------------------------------------------------------------------
# list_issues_by_bundle_label
# ---------------------------------------------------------------------------


def test_list_issues_by_bundle_label_filters_to_allowed_repos(monkeypatch):
    import batman as bm

    def fake_gh_json(_cmd, *, default):
        return [
            _issue(1, "backend"),
            _issue(2, "frontend"),
            _issue(3, "private-lab"),
        ]

    monkeypatch.setattr(bm, "gh_json", fake_gh_json)

    rows = bm.list_issues_by_bundle_label(
        "agent:bundle:checkout", allowed_repos=["myorg/backend", "myorg/frontend"]
    )

    assert [row["number"] for row in rows] == [1, 2]


def test_list_issues_by_bundle_label_accepts_local_repo_allowlist(monkeypatch):
    import batman as bm

    bm.GH_REPO_TO_LOCAL.update({"myorg-backend": "backend"})

    def fake_gh_json(_cmd, *, default):
        return [
            _issue(1, "myorg-backend"),
            _issue(2, "frontend"),
        ]

    monkeypatch.setattr(bm, "gh_json", fake_gh_json)

    rows = bm.list_issues_by_bundle_label("agent:bundle:checkout", allowed_repos=["backend"])

    assert [row["number"] for row in rows] == [1]


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
# Issue #107: parse_parent_issue diagnostic + auto-fallback to loose shape.
# ---------------------------------------------------------------------------


def _parse_parent(body: str, title: str = "Bundle: billing-v2 rollout"):
    import batman

    return batman.parse_parent_issue(
        body=body,
        title=title,
        parent_repo="myorg/backend",
        parent_issue_number=42,
    )


def test_parse_parent_issue_warns_when_no_shape_matches(caplog):
    """The lifecycle parser must surface a single warning when both the
    canonical (`Repos:`/`Children:`) and the loose
    (`## Affected Repos`/`## Acceptance Criteria`) shapes come up
    empty, so operators notice the body-format miss on the FIRST firing
    instead of after wasted cycles."""
    import logging

    with caplog.at_level(logging.WARNING, logger="alfred.batman.lifecycle"):
        plan = _parse_parent("This is just a free-form description, no markers.")
    assert plan.children == ()
    assert plan.affected_repos == ()
    matched = [
        rec
        for rec in caplog.records
        if "parse_parent_issue" in rec.getMessage() and "EXEC_NO_CHILDREN" in rec.getMessage()
    ]
    assert matched, (
        f"expected an EXEC_NO_CHILDREN warning, got: {[r.getMessage() for r in caplog.records]}"
    )


def test_parse_parent_issue_falls_back_to_loose_shape(caplog):
    """When the canonical `Repos:`/`Children:` blocks are absent but the
    loose `## Affected Repos`/`## Acceptance Criteria` shape is present,
    the parser must synthesize one child per affected repo so the plan
    post lands with real work the operator can approve, AND must log a
    warning so the operator knows to tighten the body next time."""
    import logging

    body = """
We want a billing-v2 rollout.

## Affected Repos
- backend
- frontend
- mobile

## Acceptance Criteria

### backend
- New `/billing/...` endpoints behind the `billing-v2` feature flag.

### frontend
- Billing settings page wired to the v2 endpoints.

### mobile
- Subscription paywall reads from the v2 schema.
"""
    with caplog.at_level(logging.WARNING, logger="alfred.batman.lifecycle"):
        plan = _parse_parent(body)
    assert len(plan.children) == 3, [c.repo for c in plan.children]
    child_repos = {c.repo for c in plan.children}
    # _resolve_child_repo prefers GH_REPO_TO_LOCAL when present; with no
    # mapping the fallback uses the parent org so the slugs become
    # `myorg/backend` / `myorg/frontend` / `myorg/mobile`. Either form
    # is acceptable because tests don't pin the inflection - they pin
    # the count and the local-name presence.
    for local in ("backend", "frontend", "mobile"):
        assert any(local in r for r in child_repos), child_repos
    assert any("auto-fell-back" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_parse_parent_issue_loose_shape_preserves_cross_org_slug():
    body = """
We want a cross-org billing worker rollout.

## Affected Repos
- acme/backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend"]
    assert plan.affected_repos == ("acme/backend",)


def test_parse_parent_issue_loose_shape_preserves_cross_org_slug_with_bare_rollout():
    body = """
We want a cross-org billing worker rollout.

## Affected Repos
- acme/backend

## Rollout order
- backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend"]
    assert plan.affected_repos == ("acme/backend",)


def test_parse_parent_issue_loose_shape_orders_explicit_slugs_by_default_rollout(
    monkeypatch,
):
    import batman as bm

    monkeypatch.setattr(bm, "DEFAULT_ROLLOUT_ORDER", ["backend", "frontend", "mobile"])

    body = """
We want a cross-org app rollout.

## Affected Repos
- acme/frontend
- acme/backend

## Acceptance Criteria

### frontend
- Wire the UI to the new API.

### backend
- Add the API.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend", "acme/frontend"]
    assert plan.affected_repos == ("acme/backend", "acme/frontend")


def test_parse_parent_issue_loose_shape_preserves_mapped_slug_with_bare_rollout(
    monkeypatch,
):
    import batman as bm

    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"acme/acme-backend": "backend"})

    body = """
We want a cross-org billing worker rollout.

## Affected Repos
- acme/acme-backend

## Rollout order
- backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/acme-backend"]
    assert plan.affected_repos == ("acme/acme-backend",)
    assert "Add the billing worker behind the `billing-v2` flag." in plan.children[0].body
    assert "see acceptance criteria" not in plan.children[0].body


def test_parse_parent_issue_loose_shape_qualifies_bare_mapped_slug(
    monkeypatch,
):
    import batman as bm

    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"acme-backend": "backend"})

    body = """
We want a mapped backend rollout.

## Affected Repos
- backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["myorg/acme-backend"]
    assert plan.affected_repos == ("myorg/acme-backend",)


def test_parse_parent_issue_loose_shape_uses_gh_org_for_bare_mapped_slug(
    monkeypatch,
):
    import batman as bm

    monkeypatch.setattr(bm, "GH_ORG", "acme")
    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"acme-backend": "backend"})

    body = """
We want a mapped backend rollout from a planning repo.

## Affected Repos
- backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = bm.parse_parent_issue(
        body=body,
        title="Bundle: billing-v2 rollout",
        parent_repo="platform/specs",
        parent_issue_number=42,
    )

    assert [child.repo for child in plan.children] == ["acme/acme-backend"]
    assert plan.affected_repos == ("acme/acme-backend",)


def test_parse_parent_issue_loose_shape_blocks_bare_mapped_slug_without_gh_org(
    monkeypatch,
):
    import batman as bm

    monkeypatch.setattr(bm, "GH_ORG", "")
    monkeypatch.setattr(bm, "GH_REPO_TO_LOCAL", {"acme-backend": "backend"})

    body = """
We want a mapped backend rollout from a planning repo.

## Affected Repos
- backend

## Acceptance Criteria

### backend
- Add the billing worker behind the `billing-v2` flag.
"""

    plan = bm.parse_parent_issue(
        body=body,
        title="Bundle: billing-v2 rollout",
        parent_repo="platform/specs",
        parent_issue_number=42,
    )

    assert plan.children == ()
    assert any(f.code == "ambiguous_repo_mapping" for f in plan.readiness_findings)


def test_parse_parent_issue_loose_shape_preserves_duplicate_cross_org_tails():
    body = """
We want the same worker in two orgs.

## Affected Repos
- acme/backend
- beta/backend

## Acceptance Criteria

### backend
- Add the worker behind the rollout flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend", "beta/backend"]
    assert plan.affected_repos == ("acme/backend", "beta/backend")


def test_parse_parent_issue_loose_shape_rollout_subset_keeps_remaining_repos():
    body = """
We want the same worker in two orgs, starting with acme.

## Affected Repos
- acme/backend
- beta/backend

## Rollout order
- acme/backend

## Acceptance Criteria

### backend
- Add the worker behind the rollout flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend", "beta/backend"]
    assert plan.affected_repos == ("acme/backend", "beta/backend")


def test_parse_parent_issue_loose_shape_rollout_does_not_retarget_bare_repo():
    body = """
We want backend changes in the parent org.

## Affected Repos
- backend

## Rollout order
- acme/backend

## Acceptance Criteria

### backend
- Add the shared worker behind the rollout flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["myorg/backend"]
    assert plan.affected_repos == ("myorg/backend",)


def test_parse_parent_issue_loose_shape_keeps_bare_repo_after_explicit_same_tail():
    body = """
We want backend changes in the external app and the local app.

## Affected Repos
- acme/backend
- backend

## Acceptance Criteria

### backend
- Add the shared worker behind the rollout flag.
"""

    plan = _parse_parent(body)

    assert {child.repo for child in plan.children} == {"acme/backend", "myorg/backend"}
    assert set(plan.affected_repos) == {"acme/backend", "myorg/backend"}


def test_parse_parent_issue_explicit_rollout_keeps_bare_same_tail_repo():
    body = """
We want backend changes in the external app and the local app.

## Affected Repos
- acme/backend
- backend

## Rollout order
- acme/backend

## Acceptance Criteria

### backend
- Add the shared worker behind the rollout flag.
"""

    plan = _parse_parent(body)

    assert [child.repo for child in plan.children] == ["acme/backend", "myorg/backend"]
    assert plan.affected_repos == ("acme/backend", "myorg/backend")


def test_parse_parent_issue_blocks_ambiguous_short_child_repo_key():
    body = """
Bundle: shared backend rollout

Repos:
- acme/backend
- beta/backend

Children:
- backend: add the shared worker

Done when:
- Both backend repos have the shared worker.
"""

    plan = _parse_parent(body)

    assert plan.children == ()
    assert any(f.code == "ambiguous_child_repo" for f in plan.readiness_findings)
    assert plan.readiness_blockers


def test_parse_parent_issue_blocks_loose_shape_that_would_guess_default_rollout(
    caplog,
):
    """A loose marker without actual repos must not synthesize default children."""
    import logging

    body = """
We want something better.

## Acceptance Criteria

- Improve the overall experience.
"""
    with caplog.at_level(logging.WARNING, logger="alfred.batman.lifecycle"):
        plan = _parse_parent(body)
    assert plan.children == ()
    assert plan.affected_repos == ()
    assert [(finding.code, finding.severity) for finding in plan.readiness_findings] == [
        ("guessed_default_rollout", "error")
    ]
    assert any("default rollout guess" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_parse_parent_issue_canonical_shape_does_not_trigger_fallback(caplog):
    """Sanity: when the canonical shape is present, no warning should
    fire and no auto-fallback should run."""
    import logging

    body = """
Bundle: billing-v2 rollout

Repos:
- myorg/backend
- myorg/frontend

Children:
- backend: introduce BillingV2Service
- frontend: pricing page rewrite

Done when:
- All children merged to main
"""
    with caplog.at_level(logging.WARNING, logger="alfred.batman.lifecycle"):
        plan = _parse_parent(body)
    assert len(plan.children) == 2
    assert all("parse_parent_issue" not in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_parse_parent_issue_canonical_bare_repos_use_parent_owner():
    import batman as bm

    body = """
Bundle: billing-v2 rollout

Repos:
- backend

Children:
- backend: introduce BillingV2Service

Done when:
- Child merged to main
"""
    plan = bm.parse_parent_issue(
        body=body,
        title="Bundle: billing-v2 rollout",
        parent_repo="other-org/specs",
        parent_issue_number=42,
    )

    assert plan.affected_repos == ("other-org/backend",)
    assert [child.repo for child in plan.children] == ["other-org/backend"]


def test_parse_parent_issue_does_not_suffix_match_child_repo_key():
    import batman as bm

    body = """
Bundle: core backend rollout

Repos:
- acme/core-backend

Children:
- backend: introduce BillingV2Service

Done when:
- Child merged to main
"""
    plan = bm.parse_parent_issue(
        body=body,
        title="Bundle: core backend rollout",
        parent_repo="acme/specs",
        parent_issue_number=42,
    )

    assert plan.children == ()
    assert any(f.code == "missing_children" for f in plan.readiness_findings)


# ---------------------------------------------------------------------------
# Issue #116: lifecycle parser silently skips bare-name repos
# ---------------------------------------------------------------------------


def test_parse_repo_lines_keeps_owner_repo_slugs():
    """Canonical shape: full ``owner/repo`` slugs round-trip unchanged."""
    import batman as bm

    out = bm._parse_repo_lines("- acme/backend\n- acme/frontend\n")
    assert out == ["acme/backend", "acme/frontend"]


def test_parse_repo_lines_qualifies_bare_names_with_gh_org(monkeypatch, capsys):
    """Issue #116: bare repo names get qualified with GH_ORG when set,
    instead of being silently dropped. Operator's natural shorthand
    (`palette`, `palette-web`) just works for single-org fleets."""
    monkeypatch.setenv("GH_ORG", "acme")
    import batman as bm

    # Re-import to pick up the new GH_ORG since the fixture clears
    # sys.modules per-test.
    out = bm._parse_repo_lines("- palette\n- palette-web\n")
    assert out == ["acme/palette", "acme/palette-web"]
    captured = capsys.readouterr()
    assert "BATMAN-PARSE-INFO" in captured.err
    assert "qualified bare repo name" in captured.err


def test_parse_repo_lines_warns_when_bare_and_no_gh_org(monkeypatch, capsys):
    """Without GH_ORG and without an `owner/` prefix the parser can't
    construct a usable slug - warn loudly so the operator sees the
    cause on the first firing instead of after a wasted approval cycle."""
    monkeypatch.delenv("GH_ORG", raising=False)
    import batman as bm

    out = bm._parse_repo_lines("- palette\n- backend\n")
    assert out == []
    captured = capsys.readouterr()
    assert "BATMAN-PARSE-WARN" in captured.err
    # Both lines should warn so the operator can fix all of them in one pass.
    assert captured.err.count("BATMAN-PARSE-WARN") == 2


def test_parse_repo_lines_mixes_slugs_and_bare_names(monkeypatch):
    """Half-canonical, half-bare is a realistic operator pattern when
    they paste a list of repos with one cross-org reference. Each line
    is handled on its own merits."""
    monkeypatch.setenv("GH_ORG", "acme")
    import batman as bm

    out = bm._parse_repo_lines("- acme/backend\n- mobile\n- other-org/lib\n")
    assert out == ["acme/backend", "acme/mobile", "other-org/lib"]


# ---------------------------------------------------------------------------
# Issue #117: Batman execute fails to file children when bundle label
# doesn't exist on target repos.
# ---------------------------------------------------------------------------


def test_create_issue_pre_creates_bundle_label(monkeypatch):
    """``SubprocessGitHubChildIssueClient.create_issue`` must opportunistically
    call ``gh label create`` for ``agent:bundle:<slug>`` before invoking
    ``gh issue create``, mirroring the ``gh_pr_create`` pattern. Without
    this, the first cross-repo execute fails with ``could not add label``
    and operator is left with an approved plan and zero filed children."""
    import batman as bm

    calls: list[list[str]] = []

    class FakeProc:
        def __init__(self, stdout="", returncode=0, stderr=""):
            self.stdout = stdout
            self.returncode = returncode
            self.stderr = stderr

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        # `gh label create` returns 0 (created) or non-zero (exists) -         # either is fine. `gh issue create` returns 0 + the URL.
        if cmd[1] == "issue" and cmd[2] == "create":
            return FakeProc(stdout="https://github.com/acme/backend/issues/42")
        return FakeProc()

    monkeypatch.setattr(bm.subprocess, "run", fake_run)

    client = bm.SubprocessGitHubChildIssueClient()
    url = client.create_issue(
        "acme/backend",
        title="backend: implement billing-v2",
        body="scope",
        labels=["agent:bundle:billing-v2", "agent:implement"],
    )
    assert url == "https://github.com/acme/backend/issues/42"

    # Both labels should have been pre-created, then `gh issue create`
    # invoked with both --label flags.
    label_creates = [c for c in calls if c[1] == "label" and c[2] == "create"]
    assert any("agent:bundle:billing-v2" in c for c in label_creates), label_creates
    assert any("agent:implement" in c for c in label_creates), label_creates

    issue_create = next(c for c in calls if c[1] == "issue" and c[2] == "create")
    # The label create calls happen BEFORE the issue create.
    issue_idx = calls.index(issue_create)
    bundle_label_idx = next(
        i for i, c in enumerate(calls) if "agent:bundle:billing-v2" in c and c[1] == "label"
    )
    assert bundle_label_idx < issue_idx, calls


def test_create_issue_continues_when_label_create_fails(monkeypatch):
    """Label creation is best-effort: if `gh label create` blows up
    (rate limit, transient network), the issue creation must still try
    and likely succeed - gh will accept --label for existing labels."""
    import batman as bm

    def fake_run(cmd, **_kw):
        if cmd[1] == "label":
            raise RuntimeError("transient network failure")
        if cmd[1] == "issue":

            class FakeProc:
                stdout = "https://github.com/acme/backend/issues/9"
                returncode = 0
                stderr = ""

            return FakeProc()
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr(bm.subprocess, "run", fake_run)
    client = bm.SubprocessGitHubChildIssueClient()
    url = client.create_issue(
        "acme/backend",
        title="x",
        body="y",
        labels=["agent:bundle:foo"],
    )
    assert url == "https://github.com/acme/backend/issues/9"
