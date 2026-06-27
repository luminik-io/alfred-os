from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "alfred-shipped-summary.py"


def load_module(monkeypatch, tmp_path, *, gh_org: str = "myorg"):
    monkeypatch.setenv("GH_ORG", gh_org)
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    for name in list(sys.modules):
        if name == "agent_runner" or name.startswith("agent_runner"):
            del sys.modules[name]
    sys.path.insert(0, str(ROOT / "lib"))
    try:
        spec = importlib.util.spec_from_file_location("alfred_shipped_summary_test", BIN)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def test_configured_repos_from_env(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_REPOS", "backend, myorg/frontend, ,mobile")
    assert mod.configured_repos() == ["backend", "myorg/frontend", "mobile"]


def test_configured_repos_prefers_period_specific_env(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_REPOS", "shared")
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_DAILY_REPOS", "daily-api,daily-web")
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_WEEKLY_REPOS", "weekly-app")

    assert mod.configured_repos("daily") == ["daily-api", "daily-web"]
    assert mod.configured_repos("weekly") == ["weekly-app"]
    assert mod.configured_repos() == ["shared"]


def test_collect_filters_prs_issues_and_detects_model_changes(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    conf = tmp_path / "agents.conf"
    conf.write_text(
        "my.fleet.lucius\tlucius.py\tinterval:600\tyes\t\tclaude-sonnet-4-5\tFeature dev\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALFRED_AGENTS_CONF", str(conf))
    period = mod.Period(
        label="test",
        start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
    )

    def fake_gh_json(cmd, default=None):
        joined = " ".join(cmd)
        if "pr list" in joined:
            return [
                {
                    "number": 10,
                    "title": "fix(models): tune Lucius default",
                    "url": "https://github.com/myorg/alfred/pull/10",
                    "mergedAt": "2026-05-09T12:00:00Z",
                    "additions": 12,
                    "deletions": 3,
                    "changedFiles": 2,
                    "author": {"login": "alice"},
                    "labels": [{"name": "agent:authored"}],
                    "closingIssuesReferences": [
                        {
                            "number": 7,
                            "url": "https://github.com/myorg/alfred/issues/7",
                            "repository": {"name": "alfred"},
                        }
                    ],
                },
                {
                    "number": 9,
                    "title": "old",
                    "url": "https://github.com/myorg/alfred/pull/9",
                    "mergedAt": "2026-05-08T12:00:00Z",
                    "additions": 1,
                    "deletions": 0,
                    "changedFiles": 1,
                    "author": {"login": "alice"},
                    "labels": [{"name": "agent:authored"}],
                    "closingIssuesReferences": [],
                },
            ]
        if "pr view 10" in joined:
            return {"files": [{"path": "launchd/agents.conf"}]}
        if "issue list" in joined and "created:" in joined:
            return [
                {
                    "number": 20,
                    "title": "new issue",
                    "url": "https://github.com/myorg/alfred/issues/20",
                    "createdAt": "2026-05-09T09:00:00Z",
                    "closedAt": None,
                    "state": "OPEN",
                    "labels": [{"name": "agent:authored"}],
                }
            ]
        if "issue list" in joined and "closed:" in joined:
            return [
                {
                    "number": 7,
                    "title": "closed issue",
                    "url": "https://github.com/myorg/alfred/issues/7",
                    "createdAt": "2026-05-08T09:00:00Z",
                    "closedAt": "2026-05-09T12:01:00Z",
                    "state": "CLOSED",
                    "labels": [{"name": "agent:done"}],
                }
            ]
        return default

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)

    data = mod.collect(period, ["alfred"])

    assert [pr["number"] for pr in data["prs"]] == [10]
    assert [issue["number"] for issue in data["issues_opened"]] == [20]
    assert [issue["number"] for issue in data["issues_closed"]] == [7]
    assert data["model_related_prs"][0]["model_paths"] == ["launchd/agents.conf"]
    assert data["model_defaults"] == ["lucius=claude-sonnet-4-5"]
    assert data["query_warnings"] == []


def test_fetch_uses_configurable_query_limit_and_warns(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    period = mod.Period(
        label="test",
        start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
    )
    calls: list[list[str]] = []
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT", "2")

    def fake_gh_json(cmd, default=None):
        calls.append(cmd)
        return [
            {
                "number": 1,
                "title": "one",
                "url": "https://github.com/myorg/alfred/pull/1",
                "mergedAt": "2026-05-09T12:00:00Z",
                "labels": [{"name": "agent:authored"}],
            },
            {
                "number": 2,
                "title": "two",
                "url": "https://github.com/myorg/alfred/pull/2",
                "mergedAt": "2026-05-09T13:00:00Z",
                "labels": [{"name": "agent:authored"}],
            },
        ]

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)
    warnings: list[str] = []

    prs = mod.fetch_merged_prs("alfred", period, warnings)

    assert [pr["number"] for pr in prs] == [1, 2]
    assert calls[0][calls[0].index("--limit") + 1] == "2"
    assert warnings == [
        "alfred: merged PR query hit limit 2; "
        "increase ALFRED_SHIPPED_SUMMARY_QUERY_LIMIT if totals look capped"
    ]


def test_fetch_splits_multi_day_periods_into_daily_query_windows(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    period = mod.Period(
        label="test",
        start=datetime(2026, 5, 8, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
    )
    calls: list[list[str]] = []

    def fake_gh_json(cmd, default=None):
        calls.append(cmd)
        search = cmd[cmd.index("--search") + 1]
        if search == "merged:>=2026-05-08 merged:<2026-05-09":
            return [
                {
                    "number": 8,
                    "title": "day one",
                    "url": "https://github.com/myorg/alfred/pull/8",
                    "mergedAt": "2026-05-08T12:00:00Z",
                    "labels": [{"name": "agent:authored"}],
                }
            ]
        if search == "merged:>=2026-05-09 merged:<2026-05-10":
            return [
                {
                    "number": 9,
                    "title": "day two",
                    "url": "https://github.com/myorg/alfred/pull/9",
                    "mergedAt": "2026-05-09T12:00:00Z",
                    "labels": [{"name": "agent:authored"}],
                }
            ]
        return default

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)

    prs = mod.fetch_merged_prs("alfred", period, [])

    assert [pr["number"] for pr in prs] == [8, 9]
    assert [cmd[cmd.index("--search") + 1] for cmd in calls] == [
        "merged:>=2026-05-08 merged:<2026-05-09",
        "merged:>=2026-05-09 merged:<2026-05-10",
    ]


def test_render_slack_includes_shipping_totals_and_model_section(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    data = {
        "period": {"label": "2026-05-09"},
        "prs": [
            {
                "repo": "alfred",
                "number": 10,
                "title": "fix(models): tune Lucius default",
                "url": "https://github.com/myorg/alfred/pull/10",
                "additions": 12,
                "deletions": 3,
                "changedFiles": 2,
                "closingIssuesReferences": [],
            }
        ],
        "issues_opened": [{"number": 20}],
        "issues_closed": [{"number": 7}],
        "query_warnings": ["alfred: merged PR query hit limit 2"],
        "model_related_prs": [
            {
                "repo": "alfred",
                "number": 10,
                "title": "fix(models): tune Lucius default",
                "model_paths": ["launchd/agents.conf"],
            }
        ],
        "model_defaults": ["lucius=claude-sonnet-4-5"],
        "engine_overrides": ["rasalghul=codex"],
    }

    text = mod.render_slack(data)

    assert "*Alfred shipped - 2026-05-09*" in text
    assert "1 PRs merged | 1 issues opened | 1 issues closed | +12/-3 LOC | 2 files" in text
    assert "*Query warnings*" in text
    assert "alfred: merged PR query hit limit 2" in text
    assert "`alfred#10` fix(models): tune Lucius default" in text
    assert "*Model/config changes*" in text
    assert "`lucius=claude-sonnet-4-5`" in text
    assert "`rasalghul=codex`" in text


def _period(mod):
    return mod.Period(
        label="test",
        start=datetime(2026, 5, 9, 0, 0, tzinfo=UTC),
        end=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
    )


def test_fetch_merged_prs_excludes_unlabelled_human_pr(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)

    def fake_gh_json(cmd, default=None):
        return [
            {
                "number": 1,
                "title": "agent work",
                "url": "https://github.com/myorg/alfred/pull/1",
                "mergedAt": "2026-05-09T12:00:00Z",
                "labels": [{"name": "agent:authored"}],
            },
            {
                "number": 2,
                "title": "operator hand fix",
                "url": "https://github.com/myorg/alfred/pull/2",
                "mergedAt": "2026-05-09T13:00:00Z",
                "labels": [{"name": "bug"}],
            },
            {
                "number": 3,
                "title": "operator untagged fix",
                "url": "https://github.com/myorg/alfred/pull/3",
                "mergedAt": "2026-05-09T14:00:00Z",
                "labels": [],
            },
        ]

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)

    prs = mod.fetch_merged_prs("alfred", _period(mod), [])

    # Only the agent:authored PR is counted; human PR #2 and untagged PR #3 drop.
    assert [pr["number"] for pr in prs] == [1]


def test_fetch_issues_filters_to_agent_labelled(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)

    def fake_gh_json(cmd, default=None):
        return [
            {
                "number": 11,
                "title": "agent issue",
                "url": "https://github.com/myorg/alfred/issues/11",
                "createdAt": "2026-05-09T09:00:00Z",
                "closedAt": None,
                "state": "OPEN",
                "labels": [{"name": "agent:done"}],
            },
            {
                "number": 12,
                "title": "human-filed issue",
                "url": "https://github.com/myorg/alfred/issues/12",
                "createdAt": "2026-05-09T10:00:00Z",
                "closedAt": None,
                "state": "OPEN",
                "labels": [{"name": "question"}],
            },
        ]

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)

    issues = mod.fetch_issues("alfred", _period(mod), "created", [])

    assert [issue["number"] for issue in issues] == [11]


def test_agent_labels_override_and_wildcard(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)

    # Default set includes the canonical provenance label.
    assert "agent:authored" in mod.agent_labels()

    # Custom override replaces the default set entirely.
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_AGENT_LABELS", "team:bot, Shipped-By-Alfred")
    assert mod.agent_labels() == frozenset({"team:bot", "shipped-by-alfred"})

    # Wildcard disables filtering: every item counts.
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_AGENT_LABELS", "*")
    assert mod.agent_labels() == frozenset()
    assert mod.is_agent_authored({"labels": []}, mod.agent_labels()) is True

    # A non-wildcard value that parses to nothing (just commas or whitespace) is
    # a misconfiguration, not an opt-out: fall back to defaults instead of
    # silently counting every item like the wildcard.
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_AGENT_LABELS", " , ,")
    assert "agent:authored" in mod.agent_labels()
    assert mod.is_agent_authored({"labels": []}, mod.agent_labels()) is False


def test_wildcard_counts_unlabelled_prs(monkeypatch, tmp_path):
    mod = load_module(monkeypatch, tmp_path)
    monkeypatch.setenv("ALFRED_SHIPPED_SUMMARY_AGENT_LABELS", "*")

    def fake_gh_json(cmd, default=None):
        return [
            {
                "number": 1,
                "title": "untagged",
                "url": "https://github.com/myorg/alfred/pull/1",
                "mergedAt": "2026-05-09T12:00:00Z",
                "labels": [],
            }
        ]

    monkeypatch.setattr(mod, "gh_json", fake_gh_json)

    prs = mod.fetch_merged_prs("alfred", _period(mod), [])

    assert [pr["number"] for pr in prs] == [1]
