"""Tests for lib/slop_detector.py and bin/slop-detector.py.

Coverage:
- Empty dir is reported clean
- Single banned word produces a finding with the correct line
- Multiple findings across rule types (word / phrase / regex)
- Code fences, inline backticks, HTML comments, JSX comments excluded
- skip_dirs (.git, node_modules, .cache) are pruned
- include_globs filter is respected
- Rule loader rejects malformed rules
- CLI exit code: 0 clean, 1 with --fail-on-match, 2 on bad input
- --min-severity gating
- JSON output is well-formed and deterministic
- Default bundled rule pack loads cleanly and catches the canonical
  "seamless / transform / unlock" example
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "lib"
BIN_DIR = ROOT / "bin"
EXAMPLES_DIR = ROOT / "examples"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from slop_detector import (  # noqa: E402
    RuleLoadError,
    RulePack,
    default_rule_pack_path,
    iter_target_files,
    load_rule_pack,
    render_json,
    render_markdown,
    rule_pack_from_dict,
    scan_file,
    scan_path,
    strip_code_regions,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _basic_pack_dict() -> dict:
    return {
        "name": "test-pack",
        "version": "0.0.1",
        "description": "in-memory test pack",
        "severities": ["DRIFT", "CAUTION", "TYPO"],
        "skip_dirs": [".git", "node_modules", ".cache"],
        "include_globs": ["*.md", "*.mdx", "*.html", "*.txt"],
        "rules": [
            {
                "id": "word.seamless",
                "type": "word",
                "severity": "CAUTION",
                "value": "seamless",
                "reason": "LLM cliche",
            },
            {
                "id": "word.transform",
                "type": "word",
                "severity": "CAUTION",
                "value": "transform",
                "reason": "LLM cliche",
            },
            {
                "id": "phrase.your-stack",
                "type": "phrase",
                "severity": "CAUTION",
                "value": "your stack",
                "reason": "filler",
            },
            {
                "id": "regex.em-dash",
                "type": "regex",
                "severity": "CAUTION",
                "value": "[\u2014\u2013]",  # em-dash, en-dash
                "reason": "em/en dash",
            },
        ],
    }


@pytest.fixture
def basic_pack() -> RulePack:
    return rule_pack_from_dict(_basic_pack_dict())


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    """Build a small site-tree with intentional slop + clean baselines."""
    (tmp_path / "clean.md").write_text(
        "# Clean copy\n\nThe scanner walks every file under the path.\n",
        encoding="utf-8",
    )
    (tmp_path / "slop.md").write_text(
        "# Pitch\n\n"
        "This solution will seamlessly transform your workflow.\n"
        "Plug into your stack and ship.\n",
        encoding="utf-8",
    )
    # Code-fenced slop should NOT trigger.
    (tmp_path / "doc.md").write_text(
        "# Voice rules\n\n"
        "Banned words:\n\n"
        "```\n"
        "seamless\n"
        "transform\n"
        "```\n\n"
        "Inline `seamless` is also fine to mention.\n",
        encoding="utf-8",
    )
    # Skip-dirs should be pruned.
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendor.md").write_text(
        "vendor seamless garbage\n", encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.md").write_text("seamless seamless seamless\n", encoding="utf-8")
    # Glob filter: .py is not in include_globs.
    (tmp_path / "ignored.py").write_text("seamless = True\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# Library tests
# --------------------------------------------------------------------------


def test_empty_directory_is_clean(tmp_path: Path, basic_pack: RulePack) -> None:
    report = scan_path(tmp_path, basic_pack)
    assert report.total_findings == 0
    assert report.scanned_files == 0
    assert report.by_severity == {}


def test_single_match_reports_correct_line(tmp_path: Path, basic_pack: RulePack) -> None:
    f = tmp_path / "one.md"
    f.write_text(
        "# Title\n"
        "\n"
        "Some prose.\n"
        "We will seamlessly do the thing.\n"  # line 4: NOT a match (no \bseamless\b)
        "Actually seamless integration here.\n",  # line 5: match
        encoding="utf-8",
    )
    report = scan_path(tmp_path, basic_pack)
    matches = [f for f in report.findings if f.rule_id == "word.seamless"]
    assert len(matches) == 1
    assert matches[0].line == 5
    assert matches[0].match == "seamless"
    assert "Actually seamless integration" in matches[0].snippet


def test_multiple_matches_across_rule_types(fixture_root: Path, basic_pack: RulePack) -> None:
    report = scan_path(fixture_root, basic_pack)
    rule_ids = {f.rule_id for f in report.findings}
    assert "word.seamless" not in rule_ids  # "seamlessly" doesn't match word.seamless
    # The slop.md file:
    #   "seamlessly transform your workflow"  -> word.transform
    #   "Plug into your stack"                -> phrase.your-stack
    assert "word.transform" in rule_ids
    assert "phrase.your-stack" in rule_ids


def test_code_fence_is_excluded(fixture_root: Path, basic_pack: RulePack) -> None:
    report = scan_path(fixture_root, basic_pack)
    doc_findings = [f for f in report.findings if f.path == "doc.md"]
    # The only "seamless"/"transform" occurrences in doc.md are inside
    # a fenced block or inline backticks. Nothing should trip.
    assert doc_findings == [], f"unexpected findings in doc.md: {doc_findings}"


def test_inline_backticks_excluded(tmp_path: Path, basic_pack: RulePack) -> None:
    (tmp_path / "x.md").write_text(
        "Reference: `seamless` is on the banlist; we never ship it.\n",
        encoding="utf-8",
    )
    report = scan_path(tmp_path, basic_pack)
    assert report.total_findings == 0


def test_html_and_jsx_comments_excluded(tmp_path: Path, basic_pack: RulePack) -> None:
    (tmp_path / "x.mdx").write_text(
        "<!-- seamless transform -->\n{/* your stack */}\nReal prose with seamless in it.\n",
        encoding="utf-8",
    )
    report = scan_path(tmp_path, basic_pack)
    matches = [f.rule_id for f in report.findings]
    assert matches == ["word.seamless"]
    assert report.findings[0].line == 3


def test_skip_dirs_are_pruned(fixture_root: Path, basic_pack: RulePack) -> None:
    report = scan_path(fixture_root, basic_pack)
    paths = {f.path for f in report.findings}
    assert not any(p.startswith("node_modules") for p in paths)
    assert not any(p.startswith(".git") for p in paths)


def test_include_globs_filter(fixture_root: Path, basic_pack: RulePack) -> None:
    files = iter_target_files(fixture_root, basic_pack)
    suffixes = {Path(f).suffix for f in files}
    assert ".py" not in suffixes
    assert ".md" in suffixes


def test_line_offsets_preserved_through_stripping() -> None:
    raw = (
        "line 1\n"
        "```\n"
        "seamless\n"  # would-be line 3 inside fence
        "```\n"
        "real seamless on line 5\n"
    )
    cleaned = strip_code_regions(raw)
    # newlines preserved
    assert cleaned.count("\n") == raw.count("\n")
    # length preserved
    assert len(cleaned) == len(raw)
    # in-fence "seamless" is blanked
    assert "seamless" in cleaned  # the line-5 occurrence survives
    assert cleaned.splitlines()[2].strip() == ""  # the fenced one is whitespace


def test_phrase_rule_matches_whitespace_run(tmp_path: Path, basic_pack: RulePack) -> None:
    (tmp_path / "p.md").write_text("Plug into your   stack and go.\n", encoding="utf-8")
    report = scan_path(tmp_path, basic_pack)
    ids = [f.rule_id for f in report.findings]
    assert "phrase.your-stack" in ids


def test_regex_rule_matches_em_dash(tmp_path: Path, basic_pack: RulePack) -> None:
    (tmp_path / "d.md").write_text("This - that.\n", encoding="utf-8")
    report = scan_path(tmp_path, basic_pack)
    ids = [f.rule_id for f in report.findings]
    assert "regex.em-dash" in ids


def test_findings_are_sorted_deterministically(tmp_path: Path, basic_pack: RulePack) -> None:
    (tmp_path / "b.md").write_text("seamless\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("transform\n", encoding="utf-8")
    report = scan_path(tmp_path, basic_pack)
    paths = [f.path for f in report.findings]
    assert paths == sorted(paths)


# --------------------------------------------------------------------------
# Rule loader tests
# --------------------------------------------------------------------------


def test_default_rule_pack_loads() -> None:
    pack = load_rule_pack(default_rule_pack_path())
    assert pack.name == "alfred-default"
    assert len(pack.rules) > 0
    # The advertised opinionated defaults must be present.
    ids = {r.id for r in pack.rules}
    for expected in ("word.seamless", "word.unlock", "word.leverage", "word.transform"):
        assert expected in ids, f"default pack missing {expected}"


def test_default_rule_pack_catches_advertised_examples(tmp_path: Path) -> None:
    """Confirms the advertised default-pack behavior shipped in docs."""
    (tmp_path / "ad.md").write_text(
        "This solution will seamlessly transform your workflow.\n",
        encoding="utf-8",
    )
    pack = load_rule_pack(default_rule_pack_path())
    report = scan_path(tmp_path, pack)
    matched = {f.rule_id for f in report.findings}
    assert "word.seamlessly" in matched
    assert "word.transform" in matched


def test_loader_rejects_unknown_rule_type() -> None:
    bad = _basic_pack_dict()
    bad["rules"].append({"id": "x", "type": "nope", "severity": "CAUTION", "value": "x"})
    with pytest.raises(RuleLoadError):
        rule_pack_from_dict(bad)


def test_loader_rejects_invalid_regex() -> None:
    bad = _basic_pack_dict()
    bad["rules"] = [{"id": "x", "type": "regex", "severity": "CAUTION", "value": "([)"}]
    with pytest.raises(RuleLoadError):
        rule_pack_from_dict(bad)


def test_loader_rejects_missing_value() -> None:
    bad = _basic_pack_dict()
    bad["rules"] = [{"id": "x", "type": "word", "severity": "CAUTION"}]
    with pytest.raises(RuleLoadError):
        rule_pack_from_dict(bad)


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(RuleLoadError):
        load_rule_pack(tmp_path / "does-not-exist.json")


def test_loader_rejects_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RuleLoadError):
        load_rule_pack(bad)


# --------------------------------------------------------------------------
# Rendering tests
# --------------------------------------------------------------------------


def test_markdown_clean_path(tmp_path: Path, basic_pack: RulePack) -> None:
    report = scan_path(tmp_path, basic_pack)
    md = render_markdown(report)
    assert "Clean. No slop detected." in md


def test_markdown_with_findings(fixture_root: Path, basic_pack: RulePack) -> None:
    report = scan_path(fixture_root, basic_pack)
    md = render_markdown(report)
    assert "Findings" in md
    assert "By severity" in md


def test_json_output_is_well_formed(fixture_root: Path, basic_pack: RulePack) -> None:
    report = scan_path(fixture_root, basic_pack)
    payload = json.loads(render_json(report))
    assert payload["rule_pack"] == "test-pack"
    assert payload["total_findings"] == report.total_findings
    assert isinstance(payload["findings"], list)


def test_scan_file_handles_unreadable_file(tmp_path: Path, basic_pack: RulePack) -> None:
    p = tmp_path / "ghost.md"  # never created
    result = scan_file(p, basic_pack, tmp_path)
    assert result == []


# --------------------------------------------------------------------------
# CLI tests (subprocess)
# --------------------------------------------------------------------------


CLI = BIN_DIR / "slop-detector.py"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_clean_exits_zero(tmp_path: Path) -> None:
    out = _run_cli(["--path", str(tmp_path), "--rules", str(EXAMPLES_DIR / "slop-rules.json")])
    assert out.returncode == 0, out.stderr
    assert "Clean" in out.stdout


def test_cli_fail_on_match_exits_one(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("seamlessly transform your workflow\n", encoding="utf-8")
    out = _run_cli(
        [
            "--path",
            str(tmp_path),
            "--rules",
            str(EXAMPLES_DIR / "slop-rules.json"),
            "--fail-on-match",
        ]
    )
    assert out.returncode == 1, out.stdout


def test_cli_without_fail_flag_exits_zero_with_findings(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("seamlessly transform your workflow\n", encoding="utf-8")
    out = _run_cli(["--path", str(tmp_path), "--rules", str(EXAMPLES_DIR / "slop-rules.json")])
    assert out.returncode == 0, out.stdout


def test_cli_bad_path_exits_two() -> None:
    out = _run_cli(["--path", "/definitely/not/a/real/path/here"])
    assert out.returncode == 2


def test_cli_bad_rules_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    out = _run_cli(["--path", str(tmp_path), "--rules", str(bad)])
    assert out.returncode == 2


def test_cli_json_output(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("seamlessly transform\n", encoding="utf-8")
    out = _run_cli(
        [
            "--path",
            str(tmp_path),
            "--rules",
            str(EXAMPLES_DIR / "slop-rules.json"),
            "--report",
            "json",
        ]
    )
    assert out.returncode == 0
    payload = json.loads(out.stdout)
    assert payload["total_findings"] >= 1


def test_cli_min_severity_filter(tmp_path: Path) -> None:
    """--min-severity CAUTION should still flag CAUTION-only matches.

    The default pack lists DRIFT first (most severe), CAUTION second.
    Asking for CAUTION-or-higher with only CAUTION findings present must
    still fail the run under --fail-on-match.
    """
    (tmp_path / "x.md").write_text("seamlessly\n", encoding="utf-8")
    out = _run_cli(
        [
            "--path",
            str(tmp_path),
            "--rules",
            str(EXAMPLES_DIR / "slop-rules.json"),
            "--fail-on-match",
            "--min-severity",
            "CAUTION",
        ]
    )
    assert out.returncode == 1


def test_cli_min_severity_drift_only_passes_caution(tmp_path: Path) -> None:
    """Only CAUTION findings + --min-severity DRIFT should exit 0."""
    (tmp_path / "x.md").write_text("seamlessly\n", encoding="utf-8")
    out = _run_cli(
        [
            "--path",
            str(tmp_path),
            "--rules",
            str(EXAMPLES_DIR / "slop-rules.json"),
            "--fail-on-match",
            "--min-severity",
            "DRIFT",
        ]
    )
    assert out.returncode == 0, out.stdout
