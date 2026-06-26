#!/usr/bin/env python3
"""Tests for lib/shipped_board.py: the kanban feed aggregator.

All GitHub access is stubbed via ``_gh_json``, so these run offline and
deterministically. ``conftest.py`` puts ``lib/`` on ``sys.path``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import shipped_board as sb

# A fixed "now" so age/cutoff math is deterministic.
NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _hermetic_alfred_home(monkeypatch, tmp_path):
    """Point ALFRED_HOME at a tmp dir for every test.

    ``shipped_board`` resolves the demo store and the ``.env`` repo fallback
    from ``$ALFRED_HOME`` (defaulting to ``~/.alfred``). Without
    this, a seeded host leaks demo cards into ``build_board`` (and the demo merge
    is now opt-in, but defense-in-depth keeps the whole suite independent of the
    operator's real home). Tests that want their own home can still override.
    """
    home = tmp_path / "_alfred_home"
    (home / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ALFRED_HOME", str(home))
    return home


def _iso(day: int) -> str:
    return datetime(2026, 6, day, 12, 0, 0, tzinfo=UTC).isoformat()


def _fake_gh(pr_rows, issue_rows):
    def _impl(args, **kwargs):
        if args[0] == "pr":
            return pr_rows
        if args[0] == "issue":
            return issue_rows
        if args[0] == "repo":
            return [{"nameWithOwner": "acme/api"}, {"nameWithOwner": "acme/web"}]
        return None

    return _impl


def test_board_splits_into_three_columns(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "open pr",
            "url": "u1",
            "state": "OPEN",
            "author": {"login": "alice"},
            "createdAt": _iso(1),
            "mergedAt": None,
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
            "headRefName": "lucius/1-1780550000",
        },
        {
            "number": 2,
            "title": "merged recent",
            "url": "u2",
            "state": "MERGED",
            "author": {"login": "bob"},
            "createdAt": _iso(1),
            "mergedAt": _iso(1),
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
        },
        {
            "number": 3,
            "title": "merged old",
            "url": "u3",
            "state": "MERGED",
            "author": {"login": "bob"},
            "createdAt": "2026-04-01T12:00:00+00:00",
            "mergedAt": "2026-04-01T12:00:00+00:00",
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
        },
    ]
    issues = [
        {
            "number": 9,
            "title": "do a thing",
            "url": "u9",
            "author": {"login": "carol"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, issues))
    board = sb.build_board(["acme/api"], days=14, now=NOW)

    cols = board["columns"]
    assert [c["number"] for c in cols["in_progress"]] == [1]
    assert [c["number"] for c in cols["shipped"]] == [2]  # old merge (April) excluded
    assert [c["number"] for c in cols["queued"]] == [9]
    assert board["counts"] == {"queued": 1, "in_progress": 1, "shipped": 1}
    assert board["errors"] == []


def test_cards_have_human_context(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "ship it",
            "url": "u1",
            "state": "OPEN",
            "author": {"login": "alice"},
            "createdAt": _iso(2),
            "mergedAt": None,
            "isDraft": True,
            "labels": [{"name": "agent:authored"}],
            "headRefName": "lucius/ship-it",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    card = sb.build_board(["acme/api"], now=NOW)["columns"]["in_progress"][0]
    assert card["repo"] == "acme/api"
    assert card["title"] == "ship it"
    assert card["author"] == "alice"
    assert card["age_days"] == 0
    assert card["is_draft"] is True


def test_parked_issues_excluded_from_queued(monkeypatch):
    # Open issues that are PR-backed or parked must not show as queued (Codex P2).
    issues = [
        {
            "number": 1,
            "title": "real work",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}],
        },
        {
            "number": 2,
            "title": "has a pr",
            "url": "u2",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:pr-open"}],
        },
        {
            "number": 3,
            "title": "parked",
            "url": "u3",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "do-not-pickup"}],
        },
        {
            "number": 4,
            "title": "needs human",
            "url": "u4",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "needs:human-scope"}],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [1]  # only the genuinely-queued issue


def test_approval_gated_issue_excluded_from_queued(monkeypatch):
    # A gated single-repo plan carries BOTH agent:implement AND the
    # agent:plan-pending-approval gate. The gate blocks pickup, so the board
    # must not show it in the pickable "Ready" lane while it waits on the
    # operator (Codex P2 on #218).
    issues = [
        {
            "number": 1,
            "title": "approved work",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}],
        },
        {
            "number": 2,
            "title": "gated plan awaiting operator go-ahead",
            "url": "u2",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [
                {"name": "agent:implement"},
                {"name": "agent:plan-pending-approval"},
            ],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [1]  # gated issue must not read as Ready


def test_queue_include_label_required_when_set(monkeypatch):
    # With an include label configured, only pickup-ready issues are queued;
    # roadmap / needs-info backlog is excluded (Codex review on #253).
    monkeypatch.setenv("ALFRED_SHIPPED_QUEUE_INCLUDE_LABELS", "agent:implement")
    issues = [
        {
            "number": 1,
            "title": "ready",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}],
        },
        {
            "number": 2,
            "title": "roadmap",
            "url": "u2",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "roadmap"}],
        },
        {
            "number": 3,
            "title": "needs info",
            "url": "u3",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "needs-info"}],
        },
        {
            "number": 4,
            "title": "no labels",
            "url": "u4",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [1]  # only the pickup-ready issue


def test_queue_include_label_required_by_default(monkeypatch):
    # The board answers "what Alfred can pick up", so generic backlog issues do
    # not count unless the operator opts out with the wildcard override.
    issues = [
        {
            "number": 1,
            "title": "ready",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}],
        },
        {
            "number": 2,
            "title": "large feature",
            "url": "u2",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:large-feature"}],
        },
        {
            "number": 3,
            "title": "roadmap",
            "url": "u3",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "roadmap"}],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [1, 2]


def test_queue_include_wildcard_allows_generic_backlog(monkeypatch):
    monkeypatch.setenv("ALFRED_SHIPPED_QUEUE_INCLUDE_LABELS", "*")
    issues = [
        {
            "number": 1,
            "title": "roadmap",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "roadmap"}],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [1]


def test_queue_exclude_labels_configurable(monkeypatch):
    monkeypatch.setenv("ALFRED_SHIPPED_QUEUE_EXCLUDE_LABELS", "triage")
    issues = [
        {
            "number": 1,
            "title": "t",
            "url": "u1",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "triage"}],
        },
        {
            "number": 2,
            "title": "p",
            "url": "u2",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "labels": [{"name": "agent:implement"}, {"name": "do-not-pickup"}],
        },  # not excluded now
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], issues))
    queued = sb.build_board(["acme/api"], now=NOW)["columns"]["queued"]
    assert [c["number"] for c in queued] == [2]


def test_failed_repo_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(sb, "_gh_json", lambda *a, **k: None)  # gh always fails
    board = sb.build_board(["acme/api", "acme/web"], now=NOW)
    assert board["counts"] == {"queued": 0, "in_progress": 0, "shipped": 0}
    assert set(board["errors"]) == {"acme/api", "acme/web"}
    assert board["error"].startswith("GitHub data unavailable")


def test_partial_repo_failure_with_no_cards_stays_soft(monkeypatch):
    def fake_gh(args, **kwargs):
        repo = args[args.index("--repo") + 1] if "--repo" in args else ""
        if repo == "acme/api":
            return None
        if repo == "acme/web":
            return []
        return None

    monkeypatch.setattr(sb, "_gh_json", fake_gh)
    board = sb.build_board(["acme/api", "acme/web"], now=NOW)
    assert board["counts"] == {"queued": 0, "in_progress": 0, "shipped": 0}
    assert board["errors"] == ["acme/api"]
    assert "error" not in board


def test_gh_json_uses_resolved_gh_binary(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout='[{"nameWithOwner": "acme/api"}]')

    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.delenv("ALFRED_GH_BIN", raising=False)
    monkeypatch.delenv("GH_BIN", raising=False)
    monkeypatch.setattr(sb.shutil, "which", lambda name, path=None: "/opt/homebrew/bin/gh")
    monkeypatch.setattr(sb.subprocess, "run", fake_run)

    assert sb._gh_json(["repo", "list", "acme", "--json", "nameWithOwner"]) == [
        {"nameWithOwner": "acme/api"}
    ]
    assert calls[0][0] == "/opt/homebrew/bin/gh"


def test_resolve_repos_precedence(monkeypatch):
    # Pin ALFRED_HOME away from the real ~/.alfred so the .env fallback is inert.
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "a/b, c/d")
    assert sb.resolve_repos() == ["a/b", "c/d"]
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.setenv("ALFRED_BRIDGE_REPOS", "e/f")
    assert sb.resolve_repos() == ["e/f"]
    # explicit arg wins
    assert sb.resolve_repos(["x/y"]) == ["x/y"]


def test_resolve_repos_falls_back_to_gh_org(monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.setenv("GH_ORG", "acme")
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], []))
    assert sb.resolve_repos() == ["acme/api", "acme/web"]


def test_resolve_repos_reads_env_file(monkeypatch, tmp_path):
    # The launchd server may not inherit the operator's .env into its process
    # environment, so resolve_repos reads $ALFRED_HOME/.env directly as a
    # fallback. With no env vars set, the file value must win.
    monkeypatch.delenv("ALFRED_SHIPPED_REPOS", raising=False)
    monkeypatch.delenv("ALFRED_BRIDGE_REPOS", raising=False)
    monkeypatch.delenv("GH_ORG", raising=False)
    (tmp_path / ".env").write_text(
        '# operator config\nALFRED_SHIPPED_REPOS="acme/api, acme/web"\nOTHER=ignored\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    assert sb.resolve_repos() == ["acme/api", "acme/web"]
    # An explicit process-env value still takes precedence over the file.
    monkeypatch.setenv("ALFRED_SHIPPED_REPOS", "z/z")
    assert sb.resolve_repos() == ["z/z"]


def test_gh_subprocess_env_augments_path(monkeypatch):
    # A bare-PATH host (launchd default) must still reach the Homebrew gh/git.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    path = sb._gh_subprocess_env()["PATH"].split(":")
    assert "/opt/homebrew/bin" in path
    assert "/usr/bin" in path  # original entries preserved


def test_gh_bin_resolves_against_augmented_path(monkeypatch, tmp_path):
    # Simulate gh installed only under a Homebrew-like dir absent from PATH.
    fake_bin = tmp_path / "brew" / "gh"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(sb, "_GH_EXTRA_PATH", (str(fake_bin.parent),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert sb._gh_bin() == str(fake_bin)


def test_gh_bin_honors_configured_binary(monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", "/nonexistent-alfred-home")
    monkeypatch.setenv("ALFRED_GH_BIN", "/custom/gh")
    monkeypatch.setenv("GH_BIN", "/fallback/gh")
    monkeypatch.setattr(sb.shutil, "which", lambda name, path=None: "/ignored/gh")
    assert sb._gh_bin() == "/custom/gh"


def test_shipped_sorted_newest_first(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "older",
            "url": "u1",
            "state": "MERGED",
            "author": {"login": "a"},
            "createdAt": _iso(1),
            "mergedAt": _iso(1),
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
        },
        {
            "number": 2,
            "title": "newer",
            "url": "u2",
            "state": "MERGED",
            "author": {"login": "a"},
            "createdAt": _iso(2),
            "mergedAt": _iso(2),
            "isDraft": False,
            "labels": [{"name": "agent:done"}],
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    shipped = sb.build_board(["acme/api"], now=NOW)["columns"]["shipped"]
    assert [c["number"] for c in shipped] == [2, 1]


def test_in_progress_excludes_generic_human_prs_by_default(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "human pr",
            "url": "u1",
            "state": "OPEN",
            "author": {"login": "human"},
            "createdAt": _iso(2),
            "mergedAt": None,
            "isDraft": False,
            "labels": [],
            "headRefName": "main-fix",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    assert sb.build_board(["acme/api"], now=NOW)["columns"]["in_progress"] == []


def test_in_progress_requires_an_agent_label(monkeypatch):
    prs = [
        {
            # An agent label qualifies the PR even on a human-named branch.
            "number": 1,
            "title": "agent pr",
            "url": "u1",
            "state": "OPEN",
            "author": {"login": "prasadus92"},
            "createdAt": _iso(2),
            "mergedAt": None,
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
            "headRefName": "human-named-branch",
        },
        {
            # A codename-style branch with no agent label must NOT count: it is
            # the over-matching that miscounted human PRs as agent-shipped.
            "number": 2,
            "title": "lookalike branch, no label",
            "url": "u2",
            "state": "OPEN",
            "author": {"login": "prasadus92"},
            "createdAt": _iso(1),
            "mergedAt": None,
            "isDraft": False,
            "labels": [],
            "headRefName": "batman/plan-approval-fix",
        },
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    in_progress = sb.build_board(["acme/api"], now=NOW)["columns"]["in_progress"]
    assert [c["number"] for c in in_progress] == [1]
    assert in_progress[0]["agent_evidence"] == ["label:agent:authored"]


def test_in_progress_can_include_every_pr_when_evidence_gate_disabled(monkeypatch):
    monkeypatch.setenv("ALFRED_IN_PROGRESS_REQUIRE_AGENT_EVIDENCE", "0")
    prs = [
        {
            "number": 1,
            "title": "generic pr",
            "url": "u1",
            "state": "OPEN",
            "author": {"login": "human"},
            "createdAt": _iso(2),
            "mergedAt": None,
            "isDraft": False,
            "labels": [],
            "headRefName": "main-fix",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    in_progress = sb.build_board(["acme/api"], now=NOW)["columns"]["in_progress"]
    assert [c["number"] for c in in_progress] == [1]


def test_shipped_excludes_generic_human_merges_by_default(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "human merge",
            "url": "u1",
            "state": "MERGED",
            "author": {"login": "prasadus92"},
            "createdAt": _iso(1),
            "mergedAt": _iso(2),
            "isDraft": False,
            "labels": [],
            "headRefName": "main-fix",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    shipped = sb.build_board(["acme/api"], now=NOW)["columns"]["shipped"]
    assert shipped == []


def test_shipped_counts_agent_label_evidence(monkeypatch):
    prs = [
        {
            "number": 1,
            "title": "agent merge",
            "url": "u1",
            "state": "MERGED",
            "author": {"login": "prasadus92"},
            "createdAt": _iso(1),
            "mergedAt": _iso(2),
            "isDraft": False,
            "labels": [{"name": "agent:authored"}],
            "headRefName": "human-named-branch",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    shipped = sb.build_board(["acme/api"], now=NOW)["columns"]["shipped"]
    assert [c["number"] for c in shipped] == [1]
    assert shipped[0]["agent_evidence"] == ["label:agent:authored"]


def test_shipped_excludes_branch_evidence_without_a_label(monkeypatch):
    # A merged PR on a codename-style branch but carrying no agent label must
    # NOT count as shipped: the agent label is the authoritative signal, and a
    # branch prefix alone is exactly the lookalike that was being miscounted.
    prs = [
        {
            "number": 1,
            "title": "branch merge, no label",
            "url": "u1",
            "state": "MERGED",
            "author": {"login": "prasadus92"},
            "createdAt": _iso(1),
            "mergedAt": _iso(2),
            "isDraft": False,
            "labels": [],
            "headRefName": "batman/plan-approval-fix",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    shipped = sb.build_board(["acme/api"], now=NOW)["columns"]["shipped"]
    assert [c["number"] for c in shipped] == []


def test_shipped_ignores_disabled_evidence_gate(monkeypatch):
    monkeypatch.setenv("ALFRED_SHIPPED_REQUIRE_AGENT_EVIDENCE", "0")
    prs = [
        {
            "number": 1,
            "title": "generic merge",
            "url": "u1",
            "state": "MERGED",
            "author": {"login": "human"},
            "createdAt": _iso(1),
            "mergedAt": _iso(2),
            "isDraft": False,
            "labels": [],
            "headRefName": "main-fix",
        }
    ]
    monkeypatch.setattr(sb, "_gh_json", _fake_gh(prs, []))
    shipped = sb.build_board(["acme/api"], now=NOW)["columns"]["shipped"]
    assert shipped == []


def _seed_demo(home, monkeypatch):
    """Write one demo card per column under the (tmp) ALFRED_HOME state."""
    import json
    from pathlib import Path

    state = Path(str(home)) / "state"
    state.mkdir(parents=True, exist_ok=True)
    columns = {
        "queued": [{"number": 9001, "title": "demo queued", "demo": True}],
        "in_progress": [{"number": 9002, "title": "demo wip", "demo": True}],
        "shipped": [{"number": 9003, "title": "demo shipped", "demo": True}],
    }
    (state / "setup-demo-cards.json").write_text(json.dumps({"columns": columns}), encoding="utf-8")


def test_demo_cards_excluded_by_default(_hermetic_alfred_home, monkeypatch):
    # Even with a seeded demo store, the default board contains only real work,
    # so a seeded host cannot contaminate the live board (or the test suite).
    _seed_demo(_hermetic_alfred_home, monkeypatch)
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], []))
    board = sb.build_board(["acme/api"], now=NOW)
    titles = [c["title"] for col in board["columns"].values() for c in col]
    assert all("demo" not in t for t in titles)
    assert board["counts"] == {"queued": 0, "in_progress": 0, "shipped": 0}


def test_demo_cards_merged_only_on_opt_in(_hermetic_alfred_home, monkeypatch):
    # With include_demo=True the seeded sample cards are merged into the board.
    _seed_demo(_hermetic_alfred_home, monkeypatch)
    monkeypatch.setattr(sb, "_gh_json", _fake_gh([], []))
    board = sb.build_board(["acme/api"], now=NOW, include_demo=True)
    assert [c["number"] for c in board["columns"]["queued"]] == [9001]
    assert [c["number"] for c in board["columns"]["in_progress"]] == [9002]
    assert [c["number"] for c in board["columns"]["shipped"]] == [9003]


if __name__ == "__main__":  # pragma: no cover
    import subprocess

    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-v"]))
