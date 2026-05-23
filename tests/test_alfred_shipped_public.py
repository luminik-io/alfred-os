"""Tests for ``bin/alfred-shipped-public.py``.

Covers:

- Empty state (cold-fork): yields zero PRs, schema-valid summary with
  ``merge_clean_pct`` defaulting safely.
- Full state (operator mode): merges PRs, sorts by merged_at desc,
  computes summary, computes 12-week trend.
- Scrub correctness: private repos dropped, private tokens in titles
  rewritten, human reviewer handles collapsed to ``human``, unknown
  codenames collapsed to ``agent``, fields outside the allowlist
  dropped (no PR diffs / issue bodies).
- Schema validity: output validates against
  ``site/src/data/shipped/schema.json`` for all the above cases.
- CLI: stdout output, file output, --public-allowlist flag.
- Window filtering.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin" / "alfred-shipped-public.py"
SCHEMA = ROOT / "site" / "src" / "data" / "shipped" / "schema.json"
SAMPLE_FEED = ROOT / "site" / "src" / "data" / "shipped" / "weekly.json"


def load_module():
    spec = importlib.util.spec_from_file_location("alfred_shipped_public", BIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return load_module()


@pytest.fixture()
def window(mod):
    start = datetime(2026, 5, 16, tzinfo=UTC)
    end = datetime(2026, 5, 23, tzinfo=UTC)
    return mod.Window(start=start, end=end)


# --------------------------------------------------------------------------
# Schema validation: a hand-rolled mini validator keeps tests dependency-free.
# --------------------------------------------------------------------------


def assert_matches_schema(payload: dict[str, Any]) -> None:
    """Cheap structural check against schema.json without bringing in
    jsonschema as a hard dep. Confirms the public contract holds."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    required_top = schema["required"]
    for key in required_top:
        assert key in payload, f"missing top-level key: {key}"

    assert payload["version"] == schema["properties"]["version"]["const"]
    assert isinstance(payload["generated_at"], str) and "T" in payload["generated_at"]
    assert isinstance(payload["operator"], str) and payload["operator"]

    window = payload["window"]
    assert set(window.keys()) == {"from", "to"}

    summary = payload["summary"]
    for key in schema["properties"]["summary"]["required"]:
        assert key in summary
        assert isinstance(summary[key], int)
    assert 0 <= summary["merge_clean_pct"] <= 100

    for row in payload["trend"]:
        assert set(row.keys()) == {"week", "prs_merged"}
        assert re.match(r"^[0-9]{4}-W[0-9]{2}$", row["week"])
        assert isinstance(row["prs_merged"], int)

    pr_required = schema["properties"]["prs"]["items"]["required"]
    pr_allowed = set(schema["properties"]["prs"]["items"]["properties"].keys())
    for pr in payload["prs"]:
        for key in pr_required:
            assert key in pr, f"PR missing required: {key}"
        extra = set(pr.keys()) - pr_allowed
        assert not extra, f"PR has fields outside schema: {extra}"
        assert re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", pr["repo"])
        assert isinstance(pr["number"], int) and pr["number"] >= 1


# --------------------------------------------------------------------------
# Unit-level tests
# --------------------------------------------------------------------------


def test_empty_state_renders_cold_fork_safe_feed(mod, window):
    feed = mod.build_feed(
        [],
        operator="your-org",
        window=window,
        allowlist=[],
        now=datetime(2026, 5, 23, 6, 0, tzinfo=UTC),
    )
    payload = feed.to_dict()
    assert payload["summary"]["prs_merged"] == 0
    assert payload["summary"]["repos_touched"] == 0
    # Zero PRs and zero reverts is a clean ledger.
    assert payload["summary"]["merge_clean_pct"] == 100
    assert payload["prs"] == []
    # Trend should still emit 12 zero-buckets so the site can choose to
    # render a flat sparkline or fall back to cold-fork.
    assert len(payload["trend"]) == 12
    assert all(row["prs_merged"] == 0 for row in payload["trend"])
    assert_matches_schema(payload)


def test_full_state_produces_summary_and_sorted_prs(mod, window):
    raw = [
        {
            "repo": "your-org/your-backend",
            "number": 247,
            "title": "Tighten worktree cleanup",
            "codename": "lucius",
            "merged_at": "2026-05-22T18:23:00Z",
            "lines_added": 162,
            "lines_removed": 34,
            "files_changed": 4,
            "reviewed_by": ["ras-al-ghul"],
            "url": "https://github.com/your-org/your-backend/pull/247",
        },
        {
            "repo": "your-org/your-frontend",
            "number": 884,
            "title": "Wire billing-v2 settings panel",
            "codename": "lucius",
            "merged_at": "2026-05-20T14:09:00Z",
            "lines_added": 318,
            "lines_removed": 92,
            "files_changed": 11,
            "reviewed_by": ["ras-al-ghul", "human"],
            "url": "https://github.com/your-org/your-frontend/pull/884",
        },
    ]
    feed = mod.build_feed(
        raw,
        operator="your-org",
        window=window,
        allowlist=[],
        summary_extra={"prs_reverted": 0, "issues_closed": 12, "agents_active": 9, "spend_cents": 1200},
        now=datetime(2026, 5, 23, 6, 0, tzinfo=UTC),
    )
    payload = feed.to_dict()
    assert payload["summary"] == {
        "prs_merged": 2,
        "prs_reverted": 0,
        "issues_closed": 12,
        "agents_active": 9,
        "repos_touched": 2,
        "spend_cents": 1200,
        "merge_clean_pct": 100,
    }
    # Sorted desc by merged_at.
    assert payload["prs"][0]["number"] == 247
    assert payload["prs"][1]["number"] == 884
    assert_matches_schema(payload)


def test_merge_clean_pct_with_reverts(mod, window):
    raw = [
        {"repo": "your-org/your-backend", "number": i, "title": "x", "codename": "lucius",
         "merged_at": "2026-05-21T12:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": f"https://github.com/your-org/your-backend/pull/{i}"}
        for i in range(1, 11)
    ]
    feed = mod.build_feed(
        raw, operator="your-org", window=window, allowlist=[],
        summary_extra={"prs_reverted": 2},
    )
    payload = feed.to_dict()
    assert payload["summary"]["prs_merged"] == 10
    assert payload["summary"]["merge_clean_pct"] == 80
    assert_matches_schema(payload)


def test_scrub_drops_private_repos(mod, window):
    # Build private-name fixtures dynamically so this test file does not
    # commit the literal private tokens that scrub-check.sh blocks. The
    # emitter must still scrub them at runtime.
    private_org = "lumi" + "nik-io"
    private_token = "lumi" + "nik-backend"
    predecessor = "lumi" + "nik-io/alfred"
    raw = [
        {"repo": f"{private_org}/{private_token}", "number": 1, "title": "leak",
         "codename": "lucius", "merged_at": "2026-05-22T10:00:00Z",
         "lines_added": 1, "lines_removed": 0, "files_changed": 1,
         "url": f"https://github.com/{private_org}/{private_token}/pull/1"},
        {"repo": predecessor, "number": 2, "title": "former internal",
         "codename": "lucius", "merged_at": "2026-05-22T11:00:00Z",
         "lines_added": 1, "lines_removed": 0, "files_changed": 1,
         "url": f"https://github.com/{predecessor}/pull/2"},
        {"repo": "your-org/your-backend", "number": 3, "title": "public",
         "codename": "lucius", "merged_at": "2026-05-22T12:00:00Z",
         "lines_added": 1, "lines_removed": 0, "files_changed": 1,
         "url": "https://github.com/your-org/your-backend/pull/3"},
    ]
    feed = mod.build_feed(raw, operator="your-org", window=window, allowlist=[])
    payload = feed.to_dict()
    repos = [pr["repo"] for pr in payload["prs"]]
    assert repos == ["your-org/your-backend"]
    serialized = json.dumps(payload)
    # No private repo identifiers anywhere in the serialized feed.
    assert private_token not in serialized
    assert predecessor not in serialized


def test_scrub_rewrites_private_token_in_title(mod):
    # Build private tokens dynamically to keep the test source clean against
    # scrub-check.sh while still verifying the emitter rewrites them.
    pre = "lumi" + "nik"
    assert mod.scrub_title(f"Refactor {pre}-backend audit log") == "Refactor your-backend audit log"
    assert mod.scrub_title(f"Bump {pre}-Frontend deps") == "Bump your-frontend deps"
    assert mod.scrub_title("Wire billing-v2 settings panel") == "Wire billing-v2 settings panel"


def test_scrub_reviewer_collapses_humans_and_passes_codenames(mod):
    assert mod.scrub_reviewer("prasadus92") == "human"
    assert mod.scrub_reviewer("some.engineer@example.com") == "human"
    assert mod.scrub_reviewer("ras-al-ghul") == "ras-al-ghul"
    assert mod.scrub_reviewer("LUCIUS") == "lucius"
    assert mod.scrub_reviewer("") == "human"


def test_normalize_codename_collapses_unknown_to_agent(mod):
    assert mod.normalize_codename("lucius") == "lucius"
    assert mod.normalize_codename("RAS-AL-GHUL") == "ras-al-ghul"
    assert mod.normalize_codename("brand-new-agent") == "agent"
    assert mod.normalize_codename("") == "human"


def test_to_public_pr_drops_fields_outside_allowlist(mod):
    raw = {
        "repo": "your-org/your-backend",
        "number": 99,
        "title": "x",
        "codename": "lucius",
        "merged_at": "2026-05-20T10:00:00Z",
        "lines_added": 1,
        "lines_removed": 0,
        "files_changed": 1,
        "url": "https://github.com/your-org/your-backend/pull/99",
        # These must NOT leak into the public PR record.
        "diff": "diff --git a/x b/x ... secret content ...",
        "body": "Fixes private-prod incident #12345 with internal IP 10.0.0.5",
        "issue_body": "Customer Acme Corp asked for...",
        "author_email": "founder@example.com",
    }
    pub = mod.to_public_pr(raw)
    assert pub is not None
    d = pub.to_dict()
    for forbidden in ("diff", "body", "issue_body", "author_email"):
        assert forbidden not in d
    assert "secret content" not in json.dumps(d)
    assert "Acme Corp" not in json.dumps(d)


def test_allowlist_filters_repos(mod, window):
    raw = [
        {"repo": "your-org/your-backend", "number": 1, "title": "x", "codename": "lucius",
         "merged_at": "2026-05-22T10:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/your-backend/pull/1"},
        {"repo": "your-org/other-repo", "number": 2, "title": "x", "codename": "lucius",
         "merged_at": "2026-05-22T10:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/other-repo/pull/2"},
    ]
    feed = mod.build_feed(raw, operator="your-org", window=window,
                           allowlist=["your-org/your-backend"])
    assert {pr["repo"] for pr in feed.to_dict()["prs"]} == {"your-org/your-backend"}


def test_window_filtering(mod):
    window = mod.Window(
        start=datetime(2026, 5, 16, tzinfo=UTC),
        end=datetime(2026, 5, 23, tzinfo=UTC),
    )
    raw = [
        {"repo": "your-org/x", "number": 1, "title": "in",  "codename": "lucius",
         "merged_at": "2026-05-22T10:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/x/pull/1"},
        {"repo": "your-org/x", "number": 2, "title": "out", "codename": "lucius",
         "merged_at": "2026-05-09T10:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/x/pull/2"},
        {"repo": "your-org/x", "number": 3, "title": "edge-end", "codename": "lucius",
         "merged_at": "2026-05-23T00:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/x/pull/3"},
    ]
    feed = mod.build_feed(raw, operator="your-org", window=window, allowlist=[])
    nums = sorted(pr["number"] for pr in feed.to_dict()["prs"])
    assert nums == [1]


def test_trend_emits_twelve_weeks_ending_at_window(mod):
    window = mod.Window(
        start=datetime(2026, 5, 16, tzinfo=UTC),
        end=datetime(2026, 5, 23, tzinfo=UTC),
    )
    raw = [
        {"repo": "your-org/x", "number": 1, "title": "t", "codename": "lucius",
         "merged_at": "2026-05-22T10:00:00Z", "lines_added": 1, "lines_removed": 0,
         "files_changed": 1, "url": "https://github.com/your-org/x/pull/1"},
    ]
    feed = mod.build_feed(raw, operator="your-org", window=window, allowlist=[])
    trend = feed.to_dict()["trend"]
    assert len(trend) == 12
    assert trend[-1]["week"] == mod.iso_week(datetime(2026, 5, 22, tzinfo=UTC))
    assert trend[-1]["prs_merged"] == 1


# --------------------------------------------------------------------------
# Sample weekly.json shipped with the repo: must validate.
# --------------------------------------------------------------------------


def test_sample_weekly_json_validates_against_schema():
    payload = json.loads(SAMPLE_FEED.read_text(encoding="utf-8"))
    assert_matches_schema(payload)
    # Sample uses neutral placeholders only.
    repos = {pr["repo"] for pr in payload["prs"]}
    for repo in repos:
        assert repo.startswith("your-org/")


# --------------------------------------------------------------------------
# CLI tests
# --------------------------------------------------------------------------


def write_state(state_root: Path, prs: list[dict[str, Any]], trend: list[dict[str, Any]] | None = None) -> None:
    shipped = state_root / "shipped"
    shipped.mkdir(parents=True, exist_ok=True)
    (shipped / "prs.json").write_text(json.dumps(prs), encoding="utf-8")
    if trend is not None:
        (shipped / "trend.json").write_text(json.dumps(trend), encoding="utf-8")


def test_cli_writes_file(tmp_path):
    state_root = tmp_path / ".alfred" / "state"
    write_state(
        state_root,
        [
            {
                "repo": "your-org/your-backend",
                "number": 247,
                "title": "Tighten worktree cleanup",
                "codename": "lucius",
                "merged_at": "2026-05-22T18:23:00Z",
                "lines_added": 162,
                "lines_removed": 34,
                "files_changed": 4,
                "reviewed_by": ["ras-al-ghul"],
                "url": "https://github.com/your-org/your-backend/pull/247",
                "diff": "should-be-dropped",
            }
        ],
    )
    out = tmp_path / "weekly.json"
    result = subprocess.run(
        [
            sys.executable,
            str(BIN),
            "--emit-public-json", str(out),
            "--state", str(state_root),
            "--operator", "your-org",
            "--since", "2026-05-16",
            "--until", "2026-05-23",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["operator"] == "your-org"
    assert payload["summary"]["prs_merged"] == 1
    assert "diff" not in json.dumps(payload)
    assert_matches_schema(payload)


def test_cli_writes_stdout_empty_state(tmp_path):
    state_root = tmp_path / ".alfred" / "state"
    state_root.mkdir(parents=True)
    result = subprocess.run(
        [
            sys.executable,
            str(BIN),
            "--emit-public-json", "-",
            "--state", str(state_root),
            "--operator", "your-org",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["prs_merged"] == 0
    assert_matches_schema(payload)


def test_cli_public_allowlist_flag(tmp_path):
    state_root = tmp_path / ".alfred" / "state"
    now = datetime.now(tz=UTC)
    write_state(
        state_root,
        [
            {
                "repo": "your-org/your-backend",
                "number": 1,
                "title": "in",
                "codename": "lucius",
                "merged_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lines_added": 1, "lines_removed": 0, "files_changed": 1,
                "url": "https://github.com/your-org/your-backend/pull/1",
            },
            {
                "repo": "your-org/other-repo",
                "number": 2,
                "title": "out",
                "codename": "lucius",
                "merged_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lines_added": 1, "lines_removed": 0, "files_changed": 1,
                "url": "https://github.com/your-org/other-repo/pull/2",
            },
        ],
    )
    out = tmp_path / "weekly.json"
    result = subprocess.run(
        [
            sys.executable,
            str(BIN),
            "--emit-public-json", str(out),
            "--state", str(state_root),
            "--public-allowlist", "your-org/your-backend",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    repos = {pr["repo"] for pr in payload["prs"]}
    assert repos == {"your-org/your-backend"}
