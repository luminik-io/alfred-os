"""Tests for ``lib/damian_planner.py`` and the ``bin/damian.py`` runner.

The planner is the deterministic, offline core: spec discovery, multi-repo
detection, candidate-list construction. Tests build a tmp_path spec directory
plus a fake gh client so nothing touches the network or shells out to ``gh``.
The runner shell is exercised via ``importlib`` with the same fake harness.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "bin" / "damian.py"


@pytest.fixture(autouse=True)
def _isolated_alfred_home(tmp_path, monkeypatch):
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path / "alfred"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("GH_ORG", "myorg")
    monkeypatch.delenv("DAMIAN_SCAN_REPOS", raising=False)
    monkeypatch.delenv("DAMIAN_SPEC_DIR", raising=False)
    monkeypatch.delenv("DAMIAN_DAILY_BUNDLE_CAP", raising=False)
    for mod in list(sys.modules):
        if mod.startswith("agent_runner") or mod in (
            "batman",
            "damian_planner",
            "damian_runner",
            "labels",
            "slack_format",
        ):
            del sys.modules[mod]
    sys.path.insert(0, str(REPO / "lib"))
    yield


def _write_spec(spec_dir: Path, name: str, body: str) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / name
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# PlannerConfig
# ---------------------------------------------------------------------------


def test_planner_config_reads_scan_repos_and_spec_dir(monkeypatch):
    monkeypatch.setenv("DAMIAN_SCAN_REPOS", "your-org/your-backend, your-org/your-frontend")
    monkeypatch.setenv("DAMIAN_SPEC_DIR", "/abs/specs")
    monkeypatch.setenv("DAMIAN_DAILY_BUNDLE_CAP", "5")

    import damian_planner as dp

    cfg = dp.PlannerConfig.from_env()
    assert cfg.scan_repos == ("your-org/your-backend", "your-org/your-frontend")
    assert cfg.spec_dir == Path("/abs/specs")
    assert cfg.daily_bundle_cap == 5


def test_planner_config_defaults_to_empty_scope():
    import damian_planner as dp

    cfg = dp.PlannerConfig.from_env({})
    assert cfg.scan_repos == ()
    assert cfg.spec_dir is None
    assert cfg.daily_bundle_cap == 3


def test_planner_config_floors_cap_to_one(monkeypatch):
    monkeypatch.setenv("DAMIAN_DAILY_BUNDLE_CAP", "0")
    import damian_planner as dp

    cfg = dp.PlannerConfig.from_env()
    assert cfg.daily_bundle_cap == 1


# ---------------------------------------------------------------------------
# MarkdownSpecParser
# ---------------------------------------------------------------------------


def test_parser_returns_none_for_empty_dir(tmp_path):
    import damian_planner as dp

    parser = dp.MarkdownSpecParser()
    assert parser.discover(tmp_path / "nope") == []


def test_parser_extracts_inline_repos_and_title(tmp_path):
    import damian_planner as dp

    spec = _write_spec(
        tmp_path,
        "001-auth.md",
        """# Feature: Auth Rework

Repos: backend, frontend

## Acceptance Criteria

- [ ] Backend exposes /v1/auth/login
- [ ] Frontend wires the new login form
""",
    )
    bundle = dp.MarkdownSpecParser().parse(spec)
    assert bundle is not None
    assert bundle.summary == "Auth Rework"
    assert bundle.slug == "auth-rework"
    assert [c.repo for c in bundle.children] == ["backend", "frontend"]


def test_parser_extracts_per_repo_h3_sections(tmp_path):
    import damian_planner as dp

    spec = _write_spec(
        tmp_path,
        "002-search.md",
        """# Feature: Search v2

## Acceptance Criteria

### backend
- [ ] expose POST /v1/search

### frontend
- [ ] new results page

### mobile
- [ ] sync query cache
""",
    )
    bundle = dp.MarkdownSpecParser().parse(spec)
    assert bundle is not None
    assert [c.repo for c in bundle.children] == ["backend", "frontend", "mobile"]
    backend = next(c for c in bundle.children if c.repo == "backend")
    assert "POST /v1/search" in backend.criteria


def test_parser_returns_none_for_malformed_spec(tmp_path):
    import damian_planner as dp

    spec = _write_spec(tmp_path, "junk.md", "Just a blob with no headers and no repos.\n")
    assert dp.MarkdownSpecParser().parse(spec) is None


def test_parser_picks_up_severity_marker(tmp_path):
    import damian_planner as dp

    spec = _write_spec(
        tmp_path,
        "003-p1.md",
        """# Feature: Critical Sync

severity: p1

Repos: backend, mobile
""",
    )
    bundle = dp.MarkdownSpecParser().parse(spec)
    assert bundle is not None
    assert bundle.severity == "p1"


# ---------------------------------------------------------------------------
# SpecBundlePlanner.build_plan
# ---------------------------------------------------------------------------


def _fake_gh(_cmd: list[str]) -> list[dict]:
    return []


def test_build_plan_is_empty_when_no_spec_dir():
    import damian_planner as dp

    planner = dp.SpecBundlePlanner(scan_repos=["backend", "frontend"], gh_client=_fake_gh)
    plan = planner.build_plan(None)
    assert plan.is_empty
    assert plan.bundles == []


def test_build_plan_is_empty_when_no_scan_repos(tmp_path):
    import damian_planner as dp

    spec = _write_spec(
        tmp_path,
        "001.md",
        "# Feature: A\n\nRepos: backend, frontend\n",
    )
    planner = dp.SpecBundlePlanner(scan_repos=[], gh_client=_fake_gh)
    plan = planner.build_plan(spec.parent)
    assert plan.is_empty


def test_build_plan_skips_single_repo_specs(tmp_path):
    import damian_planner as dp

    _write_spec(
        tmp_path,
        "single.md",
        "# Feature: Solo\n\nRepos: backend\n",
    )
    planner = dp.SpecBundlePlanner(scan_repos=["backend", "frontend"], gh_client=_fake_gh)
    plan = planner.build_plan(tmp_path)
    assert plan.is_empty
    assert plan.rejected_single_repo == 1


def test_build_plan_counts_unparseable_specs(tmp_path):
    import damian_planner as dp

    _write_spec(tmp_path, "noise.md", "nothing useful here\n")
    planner = dp.SpecBundlePlanner(scan_repos=["backend", "frontend"], gh_client=_fake_gh)
    plan = planner.build_plan(tmp_path)
    assert plan.is_empty
    assert plan.skipped_unparseable == 1


def test_build_plan_returns_multi_repo_bundle(tmp_path):
    import damian_planner as dp

    _write_spec(
        tmp_path,
        "multi.md",
        """# Feature: Captures

Repos: backend, frontend, mobile

## Acceptance Criteria

### backend
- [ ] expose /v1/captures
### frontend
- [ ] render captures table
### mobile
- [ ] upload audio captures
""",
    )
    planner = dp.SpecBundlePlanner(scan_repos=["backend", "frontend", "mobile"], gh_client=_fake_gh)
    plan = planner.build_plan(tmp_path)
    assert len(plan.bundles) == 1
    bundle = plan.bundles[0]
    assert bundle.is_multi_repo
    assert bundle.slug == "captures"
    assert set(bundle.affected_repos) == {"backend", "frontend", "mobile"}


def test_build_plan_filters_out_repos_outside_scan_list(tmp_path):
    import damian_planner as dp

    _write_spec(
        tmp_path,
        "mixed.md",
        """# Feature: Mixed

Repos: backend, frontend, mobile, infra

## Acceptance Criteria

### backend
- [ ] thing
### frontend
- [ ] thing
### mobile
- [ ] thing
### infra
- [ ] thing
""",
    )
    planner = dp.SpecBundlePlanner(scan_repos=["backend", "frontend"], gh_client=_fake_gh)
    plan = planner.build_plan(tmp_path)
    assert len(plan.bundles) == 1
    assert set(plan.bundles[0].affected_repos) == {"backend", "frontend"}


def test_build_plan_respects_daily_cap(tmp_path):
    import damian_planner as dp

    for idx in range(5):
        _write_spec(
            tmp_path,
            f"{idx:03d}-feature-{idx}.md",
            f"# Feature: Feature {idx}\n\nRepos: backend, frontend\n",
        )
    planner = dp.SpecBundlePlanner(
        scan_repos=["backend", "frontend"], gh_client=_fake_gh, daily_bundle_cap=2
    )
    plan = planner.build_plan(tmp_path)
    assert len(plan.bundles) == 2


def test_build_plan_dedups_against_open_bundle_slugs(tmp_path):
    import damian_planner as dp

    _write_spec(
        tmp_path,
        "captures.md",
        "# Feature: Captures\n\nRepos: backend, frontend\n",
    )

    def fake_gh(_cmd: list[str]) -> list[dict]:
        return [
            {"labels": [{"name": "agent:large-feature"}, {"name": "agent:bundle:captures"}]},
        ]

    planner = dp.SpecBundlePlanner(
        scan_repos=["backend", "frontend"],
        gh_client=fake_gh,
        gh_org="myorg",
    )
    plan = planner.build_plan(tmp_path)
    assert plan.is_empty, "open bundle with matching slug should dedup"


# ---------------------------------------------------------------------------
# render_plan_for_prompt
# ---------------------------------------------------------------------------


def test_render_plan_returns_none_marker_when_empty():
    import damian_planner as dp

    rendered = dp.render_plan_for_prompt(dp.Plan())
    assert "(none — empty plan)" in rendered


def test_render_plan_shows_bundles_and_counters(tmp_path):
    import damian_planner as dp

    bundle = dp.SpecBundle(
        slug="captures",
        spec_path=tmp_path / "captures.md",
        summary="Captures",
        children=[
            dp.BundleChild(repo="backend", criteria="- [ ] expose /v1/captures"),
            dp.BundleChild(repo="frontend", criteria="- [ ] render table"),
        ],
        severity="p1",
    )
    plan = dp.Plan(bundles=[bundle], rejected_single_repo=2, skipped_unparseable=1)
    rendered = dp.render_plan_for_prompt(plan)
    assert "### captures (p1)" in rendered
    assert "backend" in rendered
    assert "frontend" in rendered
    assert "Single-repo specs deferred to drake: 2" in rendered
    assert "Unparseable specs skipped: 1" in rendered


# ---------------------------------------------------------------------------
# Runner shell wiring (bin/damian.py)
# ---------------------------------------------------------------------------


def _load_runner():
    spec = importlib.util.spec_from_file_location("damian_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["damian_runner"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_runner_exits_quietly_when_disabled(monkeypatch, capsys):
    runner = _load_runner()
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_k: False)

    rc = runner.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "DAMIAN-SKIP" in err


def test_runner_reports_idle_when_no_scan_repos(monkeypatch, capsys, tmp_path):
    runner = _load_runner()
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_k: None)
    monkeypatch.delenv("DAMIAN_SCAN_REPOS", raising=False)

    rc = runner.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "[DAMIAN-IDLE]" in out


def test_runner_emits_noop_when_no_candidates(monkeypatch, capsys, tmp_path):
    spec_dir = tmp_path / "specs"
    _write_spec(spec_dir, "single.md", "# Feature: Solo\n\nRepos: backend\n")

    monkeypatch.setenv("DAMIAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setenv("DAMIAN_SPEC_DIR", str(spec_dir))

    runner = _load_runner()
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "gh_json", lambda *_a, **_k: [])

    rc = runner.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "[DAMIAN-NOOP]" in out


def test_runner_drafts_plan_when_prompt_missing(monkeypatch, capsys, tmp_path):
    spec_dir = tmp_path / "specs"
    _write_spec(
        spec_dir,
        "multi.md",
        "# Feature: Cross\n\nRepos: backend, frontend\n",
    )

    monkeypatch.setenv("DAMIAN_SCAN_REPOS", "backend,frontend")
    monkeypatch.setenv("DAMIAN_SPEC_DIR", str(spec_dir))

    runner = _load_runner()
    monkeypatch.setattr(runner, "doctor_mode", lambda: False)
    monkeypatch.setattr(runner, "is_agent_enabled", lambda *_a, **_k: True)
    monkeypatch.setattr(runner, "preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "with_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "gh_json", lambda *_a, **_k: [])
    posted: list[str] = []
    monkeypatch.setattr(runner, "slack_post", lambda msg, **_k: posted.append(msg))

    rc = runner.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "[DAMIAN-PLAN-DRAFTED]" in out
    assert posted, "runner should slack-post the draft when no prompt is seeded"
