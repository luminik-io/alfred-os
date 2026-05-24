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


def test_write_token_rotates_block_with_crlf_line_endings(alfredrc):
    """Operators who saved ``~/.alfredrc`` from a CRLF editor (Notepad,
    some VSCode-on-Windows-shared checkouts) must still get the rotate-
    in-place behaviour. Without the ``\\r?\\n`` allowance on ``BLOCK_RE``
    the regex misses the prior block and the file grows duplicate
    exports on every re-run."""
    alfredrc.write_bytes(
        b"export GH_ORG=acme\r\n"
        b"# alfred setup-token, do not edit by hand (re-run to rotate)\r\n"
        b"export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\r\n"
    )
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEW")
    text = alfredrc.read_text()
    assert text.count("export CLAUDE_CODE_OAUTH_TOKEN=") == 1
    assert "sk-ant-oat01-NEW" in text
    assert "sk-ant-oat01-OLD" not in text


def test_write_token_uses_narrow_umask(alfredrc, monkeypatch):
    """The token file must never exist at world-readable perms, even for
    a fraction of a second between create and chmod. Verify the wrapper
    narrows umask around the write."""
    seen: dict[str, int] = {}
    real_write = type(alfredrc).write_text
    original_umask = os.umask

    def spying_umask(mask: int) -> int:
        seen["umask_during_call"] = mask
        return original_umask(mask)

    def spying_write_text(self, *args, **kwargs):
        # umask should be narrow at the moment of write.
        seen["umask_at_write"] = os.umask(0o077)
        os.umask(seen["umask_at_write"])
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(os, "umask", spying_umask)
    monkeypatch.setattr(type(alfredrc), "write_text", spying_write_text)
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEWTOKEN")
    # At the time of the actual write, umask must mask off group + other.
    assert seen["umask_at_write"] & 0o077 == 0o077
    final_perms = stat.S_IMODE(os.stat(alfredrc).st_mode)
    assert final_perms == 0o600


def test_run_setup_token_rejects_truncated_token(alfredrc, monkeypatch):
    """If the upstream output is malformed (e.g. truncated by an ANSI
    escape), the parser must fail loud rather than silently write a
    partial credential."""
    import subprocess

    class FakeProc:
        def __init__(self) -> None:
            # Token-shaped string but only 30 chars after prefix - too short.
            self.stdout = iter(["sk-ant-oat01-tooshort_abc123\n"])

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    import shutil as _shutil_module

    monkeypatch.setattr(_shutil_module, "which", lambda _name: "/usr/local/bin/claude")
    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.run_setup_token()
    # Should exit nonzero with a clear failure message.
    assert exc.value.code != 0


def test_main_no_args_with_unset_token_invokes_setup_token(alfredrc, monkeypatch, capsys):
    """The default code path (no flag, no env) must actually call
    ``claude setup-token`` (we patch it out) and then write the parsed
    token to ~/.alfredrc."""
    fake_token = "sk-ant-oat01-fakeABC123DEFghi456JKLmno789PQRstu012VWXyzA1B2C3D4-_E5"
    mod = _load_module()
    monkeypatch.setattr(mod, "run_setup_token", lambda: fake_token)
    # Pretend the script is running from a real TTY so the non-TTY guard
    # doesn't short-circuit before run_setup_token is reached.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    rc = mod.main([])
    assert rc == 0
    text = alfredrc.read_text()
    assert fake_token in text


# ----------- issue #110: --token paste-back + non-TTY guard -----------------


def test_main_token_flag_writes_directly_without_spawning(alfredrc, monkeypatch):
    """`alfred setup-token --token <value>` must skip the Ink spawn and
    write the token straight to ~/.alfredrc. AI-assisted installs use
    this when the operator pastes the token back into the chat."""
    fake_token = "sk-ant-oat01-pastedXYZ987abc654DEFghi321JKLmno098PQRstu765VWXyzA1B2"
    mod = _load_module()
    # If anyone tried to spawn `claude setup-token` here it would crash;
    # patch run_setup_token to a sentinel so the test fails loudly if
    # the --token path doesn't short-circuit.
    sentinel = object()
    monkeypatch.setattr(mod, "run_setup_token", lambda: sentinel)
    rc = mod.main(["--token", fake_token])
    assert rc == 0
    assert fake_token in alfredrc.read_text()


def test_main_token_flag_rejects_obvious_garbage(alfredrc):
    """--token must validate the shape before persisting; otherwise an
    AI assistant could paste a wrong/truncated value and the next firing
    fails with a 401 instead of with a clear error here."""
    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.main(["--token", "definitely-not-a-real-token"])
    assert exc.value.code != 0
    assert not alfredrc.is_file()


def test_main_non_tty_exits_clean_instead_of_ink_crash(alfredrc, monkeypatch):
    """Without --token, when stdin isn't a TTY (AI-assisted install,
    CI, automation), the script must fail with a clear message instead
    of spawning `claude setup-token` and surfacing Ink's
    `Raw mode is not supported` stack trace."""
    mod = _load_module()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    # If the guard doesn't fire we'd spawn for real; stub it to a
    # sentinel that would explode the test if reached.
    monkeypatch.setattr(
        mod,
        "run_setup_token",
        lambda: (_ for _ in ()).throw(AssertionError("non-TTY guard did not fire")),
    )
    with pytest.raises(SystemExit) as exc:
        mod.main([])
    assert exc.value.code != 0
    assert not alfredrc.is_file()
