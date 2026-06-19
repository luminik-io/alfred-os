"""Guards for the aggregate-only product proof site refresh."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "site" / "scripts" / "build-product-proof.mjs"


def test_product_proof_progress_log_does_not_include_repo_slug() -> None:
    script = SCRIPT.read_text(encoding="utf-8")

    assert "console.error(`${repo}" not in script
    assert "repo ${repoIndex + 1}/${REPOS.length}" in script
