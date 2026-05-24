"""Unit tests for ``bin/alfred-setup-token.py``.

The script wraps ``claude setup-token`` to mint a long-lived OAuth token
for scheduled (launchd / systemd) firings. We verify the parts the
operator interacts with directly:

* token-presence detection (env vs ~/.alfredrc vs unset),
* the rotate-in-place semantics of ``write_token`` (no duplicates,
  unrelated lines preserved, 0600 perms applied),
* ``--check-only`` exit-code contract,
* upstream-output parsing.

Spawning real ``claude setup-token`` is intentionally not covered —
that requires a browser flow and a live Anthropic account.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_module():
    """Import bin/alfred-setup-token.py without giving it a fixed package name.

    The script is invoked as a standalone executable in production; we
    pull it in by file path so the test never has to know about its
    private deps.
    """
    spec = importlib.util.spec_from_file_location(
        "alfred_setup_token", REPO / "bin" / "alfred-setup-token.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def alfredrc(tmp_path, monkeypatch):
    """Point the module at a clean tmp ~/.alfredrc and reset env."""
    rc = tmp_path / ".alfredrc"
    monkeypatch.setenv("ALFREDRC", str(rc))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reload so module-level ALFREDRC picks up the env override.
    if "alfred_setup_token" in sys.modules:
        del sys.modules["alfred_setup_token"]
    yield rc


def test_existing_token_source_returns_none_when_unset(alfredrc):
    mod = _load_module()
    assert mod.existing_token_source() is None


def test_existing_token_source_reports_env(alfredrc, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxxxx")
    mod = _load_module()
    src = mod.existing_token_source()
    assert src is not None
    assert "env var" in src


def test_existing_token_source_reports_alfredrc(alfredrc):
    alfredrc.write_text(
        "export GH_ORG=acme\nexport CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-existing\n"
    )
    mod = _load_module()
    src = mod.existing_token_source()
    assert src == str(alfredrc)


def test_existing_token_source_ignores_comment_lines(alfredrc):
    alfredrc.write_text("# example: export CLAUDE_CODE_OAUTH_TOKEN=fake\nexport GH_ORG=acme\n")
    mod = _load_module()
    assert mod.existing_token_source() is None


def test_write_token_creates_file_with_0600(alfredrc):
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEWTOKEN")
    assert alfredrc.is_file()
    contents = alfredrc.read_text()
    # shlex.quote leaves a metachar-free token bare; we just need the
    # export line to be present, quoted or not.
    assert "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEWTOKEN" in contents
    perms = stat.S_IMODE(os.stat(alfredrc).st_mode)
    assert perms == 0o600, f"expected 0600, got {oct(perms)}"


def test_write_token_rotates_in_place_preserving_unrelated_lines(alfredrc):
    """Re-runs must replace the existing block, not duplicate it, and
    must not touch other env vars the operator added."""
    alfredrc.write_text(
        "# operator preamble\n"
        "export GH_ORG=acme\n"
        "# alfred setup-token, do not edit by hand (re-run to rotate)\n"
        "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"
        "export OTHER_VAR=preserved\n"
    )
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEW")

    text = alfredrc.read_text()
    # New token present, old absent, exactly one block.
    assert "sk-ant-oat01-NEW" in text
    assert "sk-ant-oat01-OLD" not in text
    assert text.count("export CLAUDE_CODE_OAUTH_TOKEN=") == 1
    # Operator's other lines preserved.
    assert "export GH_ORG=acme" in text
    assert "export OTHER_VAR=preserved" in text


def test_write_token_quotes_value_against_shell_metachars(alfredrc):
    """A literal ``$`` in a token must not be expanded when ~/.alfredrc
    is sourced. ``shlex.quote`` handles this."""
    mod = _load_module()
    mod.write_token("sk-ant-oat01-$DANGEROUS")
    contents = alfredrc.read_text()
    # shlex.quote wraps in single quotes when special chars present.
    assert "'sk-ant-oat01-$DANGEROUS'" in contents


def test_token_line_regex_matches_canonical_format():
    mod = _load_module()
    sample = (
        "Long-lived authentication token created successfully!\n"
        "\n"
        "sk-ant-oat01-fakeABC123DEFghi456JKLmno789PQRstu012VWXyzA1B2C3D4-_E5F6G7H8I9J0KaLbMc\n"
        "\n"
        "Store this token securely.\n"
    )
    match = mod.TOKEN_LINE_RE.search(sample)
    assert match is not None
    assert match.group(1).startswith("sk-ant-oat01-")
    assert len(match.group(1)) > 50


def test_token_line_regex_rejects_short_or_malformed():
    mod = _load_module()
    assert mod.TOKEN_LINE_RE.search("sk-ant-oat01-tooshort") is None
    assert mod.TOKEN_LINE_RE.search("sk-ant-api01-not-an-oauth-token") is None


def test_main_check_only_exits_zero_when_set(alfredrc, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    mod = _load_module()
    rc = mod.main(["--check-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "is set" in out


def test_main_check_only_exits_one_when_unset(alfredrc, capsys):
    mod = _load_module()
    rc = mod.main(["--check-only"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "is NOT set" in out


def test_main_no_args_exits_zero_when_already_set(alfredrc, monkeypatch, capsys):
    """Without ``--force``, the default path should not re-spawn ``claude
    setup-token`` when a token is already configured."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    mod = _load_module()
    rc = mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already set" in out
    assert "rotate" in out.lower()
