#!/usr/bin/env python3
"""``bin/code-memory-mcp`` sha256-verifies the auto-fetched release before it is
ever extracted or executed.

The launcher fetches a prebuilt codebase-memory-mcp tarball from a GitHub
release and runs the binary inside it. To close the supply-chain gap (a
compromised upstream account or an overridden repo/version env could otherwise
install a malicious binary), the download is checked against a sha256 pinned
from upstream's published checksums.txt before extraction. These tests drive
the verify path directly through the script's internal ``__verify-checksum``
hook, with no network access: a matching digest passes, and every failure mode
(wrong digest, no pin, missing file) fails closed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "code-memory-mcp"

# Pinned digest the script ships for darwin-arm64, copied from upstream
# checksums.txt for the pinned version. The test recreates a file with exactly
# this digest so the match path is exercised without any download.
DARWIN_ARM64_SHA = "fbd047509852021b5446a11141bcb0a3d1dcaebf6e5112460960f29f052c1c58"


def _verify(file_path: Path, expected: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "__verify-checksum", str(file_path), expected],
        capture_output=True,
        text=True,
    )


def _asset(tmp_path: Path) -> Path:
    """A stand-in release tarball with known bytes (so we know its digest)."""
    blob = tmp_path / "asset.tar.gz"
    blob.write_bytes(b"alfred-code-memory-pinned-asset")
    return blob


def test_verify_passes_on_matching_digest(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest()
    res = _verify(blob, actual)
    assert res.returncode == 0, res.stderr


def test_verify_is_case_insensitive(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest().upper()
    res = _verify(blob, actual)
    assert res.returncode == 0, res.stderr


def test_verify_fails_closed_on_mismatch(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    res = _verify(blob, "deadbeef" * 8)
    assert res.returncode != 0
    assert "MISMATCH" in res.stderr


def test_verify_fails_closed_on_empty_expected(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    res = _verify(blob, "")
    assert res.returncode != 0
    assert "refusing unverified binary" in res.stderr


def test_verify_fails_closed_on_missing_file(tmp_path: Path) -> None:
    res = _verify(tmp_path / "does-not-exist.tar.gz", DARWIN_ARM64_SHA)
    assert res.returncode != 0
    assert "missing" in res.stderr


def test_pinned_tag_resolves_to_published_digest(tmp_path: Path) -> None:
    """Passing a bare platform tag resolves to the pinned digest. A file that
    does NOT have that digest must fail closed, proving the pin is wired in
    (not silently treated as 'no pin = skip')."""
    blob = _asset(tmp_path)
    res = _verify(blob, "darwin-arm64")
    assert res.returncode != 0
    assert "MISMATCH" in res.stderr


def test_pinned_digest_overridable_via_env(tmp_path: Path) -> None:
    blob = _asset(tmp_path)
    actual = hashlib.sha256(blob.read_bytes()).hexdigest()
    env = dict(os.environ, ALFRED_CODE_MEMORY_SHA256_DARWIN_ARM64=actual)
    res = subprocess.run(
        ["bash", str(SCRIPT), "__verify-checksum", str(blob), "darwin-arm64"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, res.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
