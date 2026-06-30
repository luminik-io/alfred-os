"""Unit tests for ``bin/alfred-setup-token.py``.

The script wraps ``claude setup-token`` to mint a long-lived OAuth token
for scheduled (launchd / systemd) firings. We verify the parts the
operator interacts with directly:

* token-presence detection (env vs $ALFRED_HOME/.env vs unset),
* the rotate-in-place semantics of ``write_token`` (no duplicates,
  unrelated lines preserved, 0600 perms applied),
* ``--check-only`` exit-code contract,
* upstream-output parsing.

Spawning real ``claude setup-token`` is intentionally not covered - that requires a browser flow and a live Anthropic account.
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
def env_file(tmp_path, monkeypatch):
    """Isolate the canonical ``$ALFRED_HOME/.env`` token store."""
    env = tmp_path / ".env"
    monkeypatch.setenv("ALFRED_HOME", str(tmp_path))
    monkeypatch.delenv("ALFREDRC", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    if "alfred_setup_token" in sys.modules:
        del sys.modules["alfred_setup_token"]
    yield env


def test_existing_token_source_returns_none_when_unset(env_file):
    mod = _load_module()
    assert mod.existing_token_source() is None


def test_existing_token_source_reports_env(env_file, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxxxx")
    mod = _load_module()
    src = mod.existing_token_source()
    assert src is not None
    assert "env var" in src


def test_existing_token_source_reports_env_file(env_file):
    """A token in the canonical $ALFRED_HOME/.env is detected (dotenv form,
    no `export`)."""
    env_file.write_text(
        "GH_ORG=acme\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-existing\n", encoding="utf-8"
    )
    mod = _load_module()
    src = mod.existing_token_source()
    assert src == str(env_file)


def test_existing_token_source_ignores_legacy_alfredrc(env_file):
    """Legacy ~/.alfredrc is intentionally ignored after the .env cutover."""
    (env_file.parent / ".alfredrc").write_text(
        "export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-existing\n"
    )
    mod = _load_module()
    assert mod.existing_token_source() is None


def test_existing_token_source_ignores_comment_lines(env_file):
    env_file.write_text("# example: CLAUDE_CODE_OAUTH_TOKEN=fake\nGH_ORG=acme\n")
    mod = _load_module()
    assert mod.existing_token_source() is None


def test_write_token_creates_env_file_with_0600(env_file):
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEWTOKEN")
    assert env_file.is_file()
    contents = env_file.read_text()
    # Dotenv line: bare KEY=value, no `export`. shlex.quote leaves a
    # metachar-free token bare.
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NEWTOKEN" in contents
    assert "export CLAUDE_CODE_OAUTH_TOKEN" not in contents
    perms = stat.S_IMODE(os.stat(env_file).st_mode)
    assert perms == 0o600, f"expected 0600, got {oct(perms)}"


def test_write_token_rotates_in_place_preserving_unrelated_lines(env_file):
    """Re-runs must replace the existing line, not duplicate it, and must
    not touch other keys the operator (or the Set up surface) added."""
    env_file.write_text(
        "# operator preamble\n"
        "GH_ORG=acme\n"
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\n"
        "OTHER_VAR=preserved\n"
    )
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEW")

    text = env_file.read_text()
    # New token present, old absent, exactly one line.
    assert "sk-ant-oat01-NEW" in text
    assert "sk-ant-oat01-OLD" not in text
    assert text.count("CLAUDE_CODE_OAUTH_TOKEN=") == 1
    # Other lines preserved, in place.
    assert "GH_ORG=acme" in text
    assert "OTHER_VAR=preserved" in text
    assert "# operator preamble" in text


def test_write_token_quotes_value_against_shell_metachars(env_file):
    """A literal ``$`` in a token must not be expanded when .env is sourced
    by a shell-style loader. ``shlex.quote`` handles this."""
    mod = _load_module()
    mod.write_token("sk-ant-oat01-$DANGEROUS")
    contents = env_file.read_text()
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


def test_main_check_only_exits_zero_when_set(env_file, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    mod = _load_module()
    rc = mod.main(["--check-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "is set" in out


def test_main_check_only_exits_one_when_unset(env_file, capsys):
    mod = _load_module()
    rc = mod.main(["--check-only"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "is NOT set" in out


def test_main_no_args_exits_zero_when_already_set(env_file, monkeypatch, capsys):
    """Without ``--force``, the default path should not re-spawn ``claude
    setup-token`` when a token is already configured."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-xxx")
    mod = _load_module()
    rc = mod.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already set" in out
    assert "rotate" in out.lower()


def test_write_token_rotates_in_place_with_crlf_line_endings(env_file):
    """A ``.env`` saved from a CRLF editor (Notepad, a Windows-shared
    checkout) must still get rotate-in-place behaviour rather than growing
    a duplicate token line on every re-run."""
    env_file.write_bytes(
        b"GH_ORG=acme\r\nCLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-OLD\r\nOTHER=keep\r\n"
    )
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEW")
    text = env_file.read_text()
    assert text.count("CLAUDE_CODE_OAUTH_TOKEN=") == 1
    assert "sk-ant-oat01-NEW" in text
    assert "sk-ant-oat01-OLD" not in text
    assert "GH_ORG=acme" in text
    assert "OTHER=keep" in text


def test_write_token_uses_narrow_umask(env_file, monkeypatch):
    """The token file must never exist at world-readable perms, even for
    a fraction of a second between create and chmod. Verify the wrapper
    narrows umask around the write."""
    seen: dict[str, int] = {}
    real_write = type(env_file).write_text

    def spying_write_text(self, *args, **kwargs):
        # umask should be narrow at the moment of write.
        seen["umask_at_write"] = os.umask(0o077)
        os.umask(seen["umask_at_write"])
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(type(env_file), "write_text", spying_write_text)
    mod = _load_module()
    mod.write_token("sk-ant-oat01-NEWTOKEN")
    # At the time of the actual write, umask must mask off group + other.
    assert seen["umask_at_write"] & 0o077 == 0o077
    final_perms = stat.S_IMODE(os.stat(env_file).st_mode)
    assert final_perms == 0o600


def test_run_setup_token_rejects_truncated_token(env_file, monkeypatch):
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


def test_main_no_args_with_unset_token_invokes_setup_token(env_file, monkeypatch, capsys):
    """The default code path (no flag, no env) must actually call
    ``claude setup-token`` (we patch it out) and then write the parsed
    token to $ALFRED_HOME/.env."""
    fake_token = "sk-ant-oat01-fakeABC123DEFghi456JKLmno789PQRstu012VWXyzA1B2C3D4-_E5"
    mod = _load_module()
    monkeypatch.setattr(mod, "run_setup_token", lambda: fake_token)
    # Pretend the script is running from a real TTY so the non-TTY guard
    # doesn't short-circuit before run_setup_token is reached.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    rc = mod.main([])
    assert rc == 0
    text = env_file.read_text()
    assert fake_token in text


# ----------- issue #110: --token paste-back + non-TTY guard -----------------


def test_main_token_flag_writes_directly_without_spawning(env_file, monkeypatch):
    """`alfred setup-token --token <value>` must skip the Ink spawn and
    write the token straight to $ALFRED_HOME/.env. AI-assisted installs use
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
    assert fake_token in env_file.read_text()


def test_main_token_flag_rejects_obvious_garbage(env_file):
    """--token must validate the shape before persisting; otherwise an
    AI assistant could paste a wrong/truncated value and the next firing
    fails with a 401 instead of with a clear error here."""
    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.main(["--token", "definitely-not-a-real-token"])
    assert exc.value.code != 0
    assert not env_file.is_file()


def test_main_non_tty_exits_clean_instead_of_ink_crash(env_file, monkeypatch):
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
    assert not env_file.is_file()
