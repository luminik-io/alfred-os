"""Guards for the aggregate-only product proof site refresh."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
SCRIPT = ROOT / "site" / "scripts" / "build-product-proof.mjs"


def test_product_proof_progress_log_does_not_include_repo_slug() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "console.error(`${repo}" not in script
    assert "repo ${repoIndex + 1}/${REPOS.length}" in script


def test_product_proof_filters_rows_to_exact_timestamp_window() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "mergedAt" in script
    assert "createdAt" in script
    assert "closedAt" in script
    assert "isWithinWindow(item.mergedAt)" in script
    assert "isWithinWindow(item.createdAt)" in script
    assert "isWithinWindow(item.closedAt)" in script
    assert "timestamp >= from.getTime()" in script
    assert "timestamp <= now.getTime()" in script


def test_dependabot_does_not_use_agent_authored_label() -> None:
    config = DEPENDABOT.read_text(encoding="utf-8")

    assert "agent:authored" not in config
