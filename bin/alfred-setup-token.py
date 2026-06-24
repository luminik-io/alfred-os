#!/usr/bin/env python3
"""Bootstrap ``CLAUDE_CODE_OAUTH_TOKEN`` for scheduler-spawned agents.

Interactive auth (``claude``) stores the OAuth token in the host
credential store (macOS Keychain on Darwin, libsecret on Linux). That
works from your shell because the shell session can read the store, but
launchd / ``systemd --user`` processes run in a different security
context and cannot. Every ``claude -p`` call from a scheduled agent
returns 401 even though the same token is on disk.

The supported fix is a long-lived OAuth token that ``claude`` reads from
the ``CLAUDE_CODE_OAUTH_TOKEN`` env var, bypassing the credential store.
This script wraps the ``claude setup-token`` flow:

  1. Detect whether a token is already set (env var, ``$ALFRED_HOME/.env``,
     or the legacy ``~/.alfredrc``). Exit early when already configured,
     unless ``--force`` is given.
  2. Spawn ``claude setup-token`` so the operator can approve the
     browser flow once.
  3. Parse the long-lived token from the resulting output.
  4. Upsert ``CLAUDE_CODE_OAUTH_TOKEN=<value>`` into ``$ALFRED_HOME/.env``
     (idempotently: re-runs overwrite the line, not duplicate it) and
     chmod the file 0600.

``$ALFRED_HOME/.env`` is the single source of truth for runtime config:
the same file the Set up surface, ``config_value()``, ``ams-launch.sh``,
and ``bin/agent-launch`` read. Writing the token anywhere else (a shell
rc file the scheduler loader never sources) is exactly how a token can
be "set" yet still 401 every scheduled firing. The dotenv format is bare
``KEY=value`` with no ``export``, matching the rest of ``.env``.

The token is tied to the operator's existing subscription. There is no
extra cost, no new account, no API-key billing. Rotate by re-running
with ``--force`` and overwriting the line; revoke at
https://console.anthropic.com/settings/keys if the file is exposed.

Usage:
  alfred setup-token              # interactive (recommended)
  alfred setup-token --force      # rotate even if already set
  alfred setup-token --check-only # report status without touching auth
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

# Legacy shell rc. Older installs (and a hand-edited operator machine)
# may still carry the token here. We READ it so ``--check-only`` and the
# already-configured guard see it, and so ``--force`` can migrate it, but
# we no longer WRITE here: the scheduler loader does not source rc files,
# so a token parked in ~/.alfredrc silently 401s every firing.
ALFREDRC = Path(os.environ.get("ALFREDRC", str(Path.home() / ".alfredrc")))

# Accepts ``\r?\n`` line endings because an operator could have saved
# ~/.alfredrc from a CRLF editor (Notepad, a Windows checkout).
LEGACY_BANNER = "# alfred setup-token, do not edit by hand (re-run to rotate)"
LEGACY_BLOCK_RE = re.compile(
    rf"\r?\n?{re.escape(LEGACY_BANNER)}\r?\nexport {TOKEN_ENV}=[^\r\n]*\r?\n",
    re.MULTILINE,
)


def _alfred_home() -> Path:
    return Path(os.environ.get("ALFRED_HOME") or str(Path.home() / ".alfred"))


def env_path() -> Path:
    """Canonical runtime store: ``$ALFRED_HOME/.env`` (dotenv KEY=value).

    Resolved at call time, not import time, so a test (or an operator who
    exports ALFRED_HOME between runs) sees the right file.
    """
    return _alfred_home() / ".env"


# claude setup-token prints the token on a line by itself between two
# sets of human prose. We match the canonical prefix (``sk-ant-oat01-``)
# and grab the longest token-shaped string on that line.
TOKEN_LINE_RE = re.compile(r"(sk-ant-oat[0-9]{2}-[A-Za-z0-9_\-]{40,})")

# Sanity bounds on a parsed token. Loose by design so a future longer
# token format still flies, but tight enough to reject obvious garbage
# (truncated by an ANSI escape, mangled by locale-decoder, etc.).
_MIN_TOKEN_LEN = 50
_MAX_TOKEN_LEN = 4096


def info(msg: str) -> None:
    print(f"[alfred setup-token] {msg}")


def warn(msg: str) -> None:
    print(f"[alfred setup-token] WARN: {msg}", file=sys.stderr)


def fail(msg: str, code: int = 1) -> None:
    print(f"[alfred setup-token] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _file_defines_token(path: Path) -> bool:
    """True if ``path`` defines a non-empty ``TOKEN_ENV`` line.

    Tolerates both dotenv (``KEY=value``) and shell (``export KEY=value``)
    forms so it reads ``.env`` and the legacy ``~/.alfredrc`` alike.
    """
    if not path.is_file():
        return False
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read {path}: {exc}")
        return False
    for line in contents.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        key = name.removeprefix("export").strip()
        if key == TOKEN_ENV and value.strip():
            return True
    return False


def existing_token_source() -> str | None:
    """Return a human-readable description of where the token is already set,
    or ``None`` if it is unset.

    Checks process env first (covers shell exports), then the canonical
    ``$ALFRED_HOME/.env``, then the legacy ``~/.alfredrc``. Reporting the
    legacy path lets ``--force`` migrate an old install to ``.env``.
    Does not validate the value, only reports presence.
    """
    if os.environ.get(TOKEN_ENV, "").strip():
        return f"env var {TOKEN_ENV}"
    env_file = env_path()
    if _file_defines_token(env_file):
        return str(env_file)
    if _file_defines_token(ALFREDRC):
        return f"{ALFREDRC} (legacy; re-run with --force to migrate to {env_file})"
    return None


def _strip_legacy_block(text: str) -> str:
    """Remove the token line from legacy ``~/.alfredrc`` content.

    Drops both the old marker block and any bare ``export TOKEN=...`` line
    so migrating to ``.env`` does not leave a stale duplicate behind.
    """
    cleaned = LEGACY_BLOCK_RE.sub("\n", text)
    kept = [
        line
        for line in cleaned.splitlines()
        if line.strip().removeprefix("export").strip().split("=", 1)[0] != TOKEN_ENV
    ]
    return "\n".join(kept).rstrip()


def _migrate_legacy_token() -> None:
    """Remove a token line left in ``~/.alfredrc`` by an older install.

    Best-effort: if the file is unreadable or unwritable we warn and move
    on. The newly written ``.env`` is authoritative regardless.
    """
    if not _file_defines_token(ALFREDRC):
        return
    try:
        existing = ALFREDRC.read_text(encoding="utf-8")
    except OSError as exc:
        warn(f"could not read legacy {ALFREDRC} to migrate token: {exc}")
        return
    cleaned = _strip_legacy_block(existing)
    new_contents = (cleaned + "\n") if cleaned else ""
    prior_umask = os.umask(0o077)
    try:
        ALFREDRC.write_text(new_contents, encoding="utf-8")
    except OSError as exc:
        warn(f"could not rewrite legacy {ALFREDRC}: {exc}")
        return
    finally:
        os.umask(prior_umask)
    info(f"removed legacy {TOKEN_ENV} from {ALFREDRC} (now lives in {env_path()}).")


def write_token(token: str) -> None:
    """Idempotently upsert ``CLAUDE_CODE_OAUTH_TOKEN`` into ``$ALFRED_HOME/.env``
    and tighten perms to 0600.

    Re-runs replace the existing line in place rather than duplicating it,
    and every other line in ``.env`` is preserved untouched. The file is
    created if missing. On a shared host the file is created with 0600
    perms from the start (umask narrowed during the write) so there is no
    readable window between create and chmod holding a year-long
    subscription credential. A token left in the legacy ``~/.alfredrc`` is
    migrated out so the two stores cannot disagree.
    """
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Dotenv line: bare KEY=value, no `export`, shlex-quoted so a stray
    # shell metachar in the token cannot break a downstream sourcing.
    new_line = f"{TOKEN_ENV}={shlex.quote(token)}"

    try:
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        existing_lines = []

    out_lines: list[str] = []
    replaced = False
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            name = stripped.partition("=")[0].removeprefix("export").strip()
            if name == TOKEN_ENV:
                if not replaced:
                    out_lines.append(new_line)
                    replaced = True
                continue
        out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)
    new_contents = "\n".join(out_lines).rstrip("\n") + "\n"

    # Narrow umask so the create-then-chmod window cannot leave a
    # world-readable file holding a year-long subscription credential.
    prior_umask = os.umask(0o077)
    try:
        path.write_text(new_contents, encoding="utf-8")
    finally:
        os.umask(prior_umask)
    try:
        path.chmod(0o600)
    except OSError as exc:
        warn(f"could not chmod 0600 {path}: {exc}")

    _migrate_legacy_token()


def run_setup_token() -> str:
    """Spawn ``claude setup-token``, return the parsed token.

    Stdout is teed to the operator's terminal in real time so they see
    the browser prompt and any errors. We also capture a copy to parse
    the token line out of afterwards.
    """
    if shutil.which("claude") is None:
        fail("`claude` is not on PATH. Install it first: npm install -g @anthropic-ai/claude-code")

    info("running `claude setup-token` (approve in browser when prompted) ...")
    print("=" * 60)
    try:
        # Force utf-8 with replace so a non-UTF-8 host locale (LANG=C
        # under launchd, POSIX on minimal Linux) cannot mangle the token
        # before our regex sees it.
        proc = subprocess.Popen(
            ["claude", "setup-token"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        fail(f"could not launch `claude setup-token`: {exc}")

    assert proc.stdout is not None
    captured: list[str] = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    rc = proc.wait()
    print("=" * 60)

    if rc != 0:
        fail(
            f"`claude setup-token` exited {rc}. See output above for details.",
            code=rc,
        )

    output = "".join(captured)
    match = TOKEN_LINE_RE.search(output)
    if not match:
        fail(
            "could not parse a long-lived token from `claude setup-token` output. "
            "Run the command yourself, copy the printed token, and add this line "
            f"to {env_path()} manually:\n\n    {TOKEN_ENV}=<your-token>\n\n"
            "Then re-run this script with --check-only to confirm."
        )
    token = match.group(1)
    # Defensive bounds: the canonical regex above already gates on prefix
    # and minimum length, but if upstream ever emits the token with an
    # embedded ANSI escape or similar, the match could silently truncate.
    # Better to fail loud than to write half a credential to disk.
    if not (_MIN_TOKEN_LEN <= len(token) <= _MAX_TOKEN_LEN) or not token.isascii():
        fail(
            f"parsed token failed sanity check (length={len(token)}, ascii={token.isascii()}). "
            "The `claude setup-token` output format may have changed. "
            "File a bug and pass --check-only after setting CLAUDE_CODE_OAUTH_TOKEN manually."
        )
    return token


def _validate_token_shape(token: str) -> None:
    """Reject obviously broken tokens before writing them to ``.env``."""
    if not TOKEN_LINE_RE.fullmatch(token):
        fail(
            f"--token value does not look like a Claude long-lived token "
            f"(expected prefix `sk-ant-oat<NN>-`, got {token[:16]!r}...). "
            "Copy the exact value `claude setup-token` printed."
        )
    if not (_MIN_TOKEN_LEN <= len(token) <= _MAX_TOKEN_LEN) or not token.isascii():
        fail(f"--token failed sanity check (length={len(token)}, ascii={token.isascii()}).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="alfred setup-token",
        description=(
            "Mint a long-lived Claude OAuth token so scheduled (launchd / "
            "systemd --user) agents can authenticate without the host "
            "credential store. Token goes to $ALFRED_HOME/.env."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run even if a token is already configured (rotate)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report status without spawning `claude setup-token`",
    )
    parser.add_argument(
        "--token",
        metavar="VALUE",
        default=None,
        help=(
            "skip the interactive `claude setup-token` spawn and write the "
            "given token to $ALFRED_HOME/.env directly. Use this from "
            "AI-assisted installs where the operator runs `claude setup-token` "
            "in their own terminal and pastes the value back."
        ),
    )
    args = parser.parse_args(argv)

    source = existing_token_source()
    if args.check_only:
        if source:
            info(f"{TOKEN_ENV} is set in {source}.")
            return 0
        info(f"{TOKEN_ENV} is NOT set. Run `alfred setup-token` to configure.")
        return 1

    if source and not args.force:
        info(f"{TOKEN_ENV} already set in {source}. Pass --force to rotate.")
        return 0

    if source and args.force:
        info(f"rotating existing token in {source} ...")

    # Paste-back path: skip the Ink-based spawn entirely. Unblocks
    # AI-assisted installs (Claude Code, Codex, automation) where the
    # script can't spawn a TUI but the assistant CAN ask the operator
    # to run `claude setup-token` in their own terminal and paste the
    # result back. See issue #110 and docs/AI_ASSISTED_INSTALL.md.
    if args.token:
        token = args.token.strip()
        _validate_token_shape(token)
        write_token(token)
        info(f"wrote {TOKEN_ENV} from --token to {env_path()} (chmod 0600).")
        info("scheduled agents will pick it up on their next firing.")
        return 0

    # Interactive spawn requires a real TTY because `claude setup-token`
    # uses Ink (React-for-CLI) and Ink needs raw-mode stdin. Subprocess
    # / non-TTY contexts (AI-assisted installs, CI, automation) crash
    # with an opaque Ink stack trace. Bail clean with a path forward.
    if not sys.stdin.isatty():
        fail(
            "`claude setup-token` needs an interactive terminal (the underlying "
            "Ink TUI cannot read raw-mode stdin from a non-TTY context). Three "
            "supported paths:\n"
            "  1. Run `alfred setup-token` in your own shell, OR\n"
            "  2. Run `claude setup-token` in your shell, copy the printed token, "
            "then run: alfred setup-token --token <value>, OR\n"
            "  3. Set CLAUDE_CODE_OAUTH_TOKEN in $ALFRED_HOME/.env by hand "
            "(CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...) and re-run with --check-only."
        )

    token = run_setup_token()
    write_token(token)

    info(f"wrote {TOKEN_ENV} to {env_path()} (chmod 0600).")
    info("scheduled agents will pick it up on their next firing.")
    info("rotate later with `alfred setup-token --force`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
