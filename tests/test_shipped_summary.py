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
                    "labels": [],
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
                    "labels": [],
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
            },
            {
                "number": 2,
                "title": "two",
                "url": "https://github.com/myorg/alfred/pull/2",
                "mergedAt": "2026-05-09T13:00:00Z",
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
                }
            ]
        if search == "merged:>=2026-05-09 merged:<2026-05-10":
            return [
                {
                    "number": 9,
                    "title": "day two",
                    "url": "https://github.com/myorg/alfred/pull/9",
                    "mergedAt": "2026-05-09T12:00:00Z",
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
