from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from dependencies import (  # noqa: E402
    IssueRef,
    issue_dependencies,
    issue_ref,
    parse_dependency_refs,
    sort_issues_by_dependencies,
)


def _issue(num, repo, body=""):
    return {
        "number": num,
        "url": f"https://github.com/acme/{repo}/issues/{num}",
        "body": body,
    }


def test_parse_dependency_refs_accepts_local_qualified_and_url_refs():
    body = """
Depends on: #12, acme/frontend#34
Blocked by: https://github.com/acme/agents/issues/56
"""

    assert parse_dependency_refs(body, default_repo="backend") == (
        IssueRef("acme/agents", 56),
        IssueRef("acme/frontend", 34),
        IssueRef("backend", 12),
    )


def test_parse_dependency_refs_qualifies_bare_refs_with_default_owner():
    body = """
Depends on: #12, frontend#34
"""

    assert parse_dependency_refs(body, default_repo="acme/backend") == (
        IssueRef("acme/backend", 12),
        IssueRef("acme/frontend", 34),
    )


def test_parse_dependency_refs_ignores_ambiguous_bare_words():
    body = """
Depends on: fix#12, pr#13, frontend#34
"""

    assert parse_dependency_refs(body, default_repo="acme/backend") == (
        IssueRef("acme/frontend", 34),
    )


def test_issue_ref_ignores_payloads_without_issue_url():
    issue = {"number": 12, "body": "Depends on: #12, #13"}

    assert issue_ref(issue) is None
    assert issue_dependencies(issue) == ()
    assert issue_ref(issue, default_repo="acme/backend") == IssueRef("acme/backend", 12)
    assert issue_dependencies(issue, default_repo="acme/backend") == (IssueRef("acme/backend", 13),)


def test_sort_issues_by_dependencies_orders_bundle_siblings():
    frontend = _issue(2, "frontend", "Depends on: acme/backend#1")
    backend = _issue(1, "backend")
    mobile = _issue(3, "mobile", "Blocked by: acme/frontend#2")

    sorted_issues = sort_issues_by_dependencies([mobile, frontend, backend])

    assert [(issue["url"], issue["number"]) for issue in sorted_issues] == [
        (backend["url"], 1),
        (frontend["url"], 2),
        (mobile["url"], 3),
    ]


def test_sort_issues_by_dependencies_keeps_original_order_on_cycle():
    one = _issue(1, "backend", "Depends on: acme/frontend#2")
    two = _issue(2, "frontend", "Depends on: acme/backend#1")

    assert sort_issues_by_dependencies([one, two]) == [one, two]
