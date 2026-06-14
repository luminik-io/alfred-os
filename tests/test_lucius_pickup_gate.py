"""Lucius must keep operator-approval-gated plans out of its pickup window.

A gated single-repo plan carries BOTH ``agent:implement`` AND
``agent:plan-pending-approval``. The gate label is the pickup blocker, cleared
on operator approval. Lucius fetches only the first page of open
``agent:implement`` issues, so if gated issues consumed that window an approved
issue could be starved out. These tests prove the gate is excluded at the query
source (so it never consumes the window) and that the in-loop backstop still
skips any gated issue that slips through between query and pick.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_bin_module(name: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALFRED_HOME", str(ROOT))
    sys.path.insert(0, str(ROOT / "lib"))
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "bin" / name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop(spec.name, None)
    spec.loader.exec_module(module)
    return module


def _issue(number: int, labels: list[str], created: str) -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "url": f"https://example.com/{number}",
        "labels": [{"name": name} for name in labels],
        "createdAt": created,
        "body": "Add a retry banner so failed checkout calls surface to the user.",
        "author": {"login": "user"},
    }


def test_pickup_query_excludes_the_approval_gate_at_the_source(monkeypatch):
    """The gh query must carry a ``-label:`` search qualifier for the gate, so
    gated issues never consume the ``--limit`` window."""
    monkeypatch.setenv("GH_ORG", "myorg")
    lucius = load_bin_module("lucius.py", monkeypatch)
    monkeypatch.setattr(lucius, "LUCIUS_REPOS", ["backend"])
    monkeypatch.setattr(lucius, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(lucius, "is_dry_run", lambda: False)
    monkeypatch.setattr(lucius, "issue_has_open_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr(lucius, "find_open_authored_pr_for_issue", lambda *a, **kw: None)

    seen_cmds: list[list[str]] = []

    def fake_gh_json(cmd, default=None):
        seen_cmds.append(cmd)
        # Mimic GitHub honoring the search qualifier: gated issue is filtered out.
        return [_issue(7, [lucius.label_constants.IMPLEMENT], "2026-06-06T10:00:00Z")]

    monkeypatch.setattr(lucius, "gh_json", fake_gh_json)

    repo, issue = lucius.pick_issue()

    assert repo == "backend"
    assert issue is not None and issue["number"] == 7
    # The list query must exclude the gate label via a search qualifier.
    list_cmd = next(c for c in seen_cmds if c[:3] == ["gh", "issue", "list"])
    assert "--search" in list_cmd
    search_arg = list_cmd[list_cmd.index("--search") + 1]
    assert search_arg == f"-label:{lucius.label_constants.PLAN_PENDING_APPROVAL}"


def test_pickup_loop_skips_a_gated_issue_that_slips_through(monkeypatch):
    """Backstop: if a gated issue is returned anyway (acquired the gate between
    query and pick), the in-loop blocker check still skips it and falls through
    to the next ungated approved issue instead of returning the blocked one."""
    monkeypatch.setenv("GH_ORG", "myorg")
    lucius = load_bin_module("lucius.py", monkeypatch)
    monkeypatch.setattr(lucius, "LUCIUS_REPOS", ["backend"])
    monkeypatch.setattr(lucius, "is_repo_paused", lambda repo: False)
    monkeypatch.setattr(lucius, "is_dry_run", lambda: False)
    monkeypatch.setattr(lucius, "issue_has_open_dependencies", lambda *a, **kw: False)
    monkeypatch.setattr(lucius, "find_open_authored_pr_for_issue", lambda *a, **kw: None)

    gated = _issue(
        1,
        [lucius.label_constants.IMPLEMENT, lucius.label_constants.PLAN_PENDING_APPROVAL],
        "2026-06-06T09:00:00Z",
    )
    approved = _issue(2, [lucius.label_constants.IMPLEMENT], "2026-06-06T11:00:00Z")

    monkeypatch.setattr(lucius, "gh_json", lambda cmd, default=None: [gated, approved])

    repo, issue = lucius.pick_issue()

    # The older gated issue must be skipped; Lucius reaches the approved one.
    assert repo == "backend"
    assert issue is not None and issue["number"] == 2
