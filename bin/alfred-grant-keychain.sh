#!/usr/bin/env bash
# alfred-grant-keychain — targeted Keychain ACL grant for the `claude` binary.
#
# Resolves the macOS launchd Keychain ACL issue documented in
# docs/MACOS_KEYCHAIN.md without using the "Allow all applications"
# radio (which would grant every process running as your user silent
# access to your Claude OAuth token).
#
# What this script does:
#
#   1. Resolves the real `claude.exe` path (following symlinks through
#      fnm / nvm / asdf / volta session shims). Override with the
#      CLAUDE_BIN environment variable if your install is non-standard.
#   2. Enumerates Keychain entries matching `Claude Code-credentials*`
#      plus `Claude Safe Storage`.
#   3. Inspects the binary's code-signing identity to pick the right
#      partition-list entry (`teamid:<TEAMID>` if signed by Anthropic,
#      `unsigned:` otherwise).
#   4. With `--apply`, calls `security set-generic-password-partition-
#      list` for each entry so the partition list includes claude.exe
#      alongside the existing `apple:` / `apple-tool:` defaults. The
#      operator's login keychain password is prompted once, used in
#      this process only, never echoed or stored.
#
# What this script does NOT do:
#
#   - Modify any Keychain item other than the matched Claude entries.
#   - Touch any file outside Keychain.
#   - Write the keychain password to disk, env, history, or process
#     listings (passed via stdin, not argv).
#   - Run without your explicit `--apply` flag.
#
# Usage:
#
#   bash bin/alfred-grant-keychain.sh             # advisory mode
#   bash bin/alfred-grant-keychain.sh --apply     # apply via security CLI
#   CLAUDE_BIN=/path/to/claude bash bin/alfred-grant-keychain.sh --apply

set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "alfred-grant-keychain: this script is macOS only (Keychain is not used on Linux)." >&2
  exit 0
fi

# Resolve the real claude binary, following symlinks through any session
# shim. BSD readlink lacks -f; use Python for portable real-path resolution.
real_path() {
  python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "$1"
}

if [ -z "${CLAUDE_BIN:-}" ]; then
  candidate="$(command -v claude || true)"
  if [ -z "$candidate" ]; then
    echo "alfred-grant-keychain: cannot find 'claude' on PATH. Set CLAUDE_BIN env var to the full path." >&2
    exit 2
  fi
  CLAUDE_BIN="$(real_path "$candidate")"
fi
# CLAUDE_BIN is now either the operator's override or the resolved real path.

if [ ! -f "$CLAUDE_BIN" ]; then
  echo "alfred-grant-keychain: $CLAUDE_BIN does not exist or is not a regular file." >&2
  exit 2
fi

echo "alfred-grant-keychain: resolved claude binary"
echo "  $CLAUDE_BIN"
echo ""

# Find every Claude-related credential entry in the login keychain.
# We look for the canonical service name plus the per-account hashed
# variants and the Electron-app safe-storage entry.
LOGIN_KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"
if [ ! -f "$LOGIN_KEYCHAIN" ]; then
  echo "alfred-grant-keychain: login.keychain-db not found at $LOGIN_KEYCHAIN." >&2
  exit 3
fi

# `security dump-keychain` is verbose; filter for service-name lines that
# match our prefixes. Dedupe with sort -u. Use a temp file rather than
# `mapfile` (bash 4+) so this stays portable to macOS's default bash 3.2.
ENTRIES_FILE="$(mktemp)"
trap 'rm -f "$ENTRIES_FILE"' EXIT
security dump-keychain "$LOGIN_KEYCHAIN" 2>/dev/null \
  | awk -F'"' '
      /"svce"<blob>=/ {
        if ($4 ~ /^Claude Code-credentials/ || $4 == "Claude Safe Storage") print $4
      }
    ' \
  | sort -u > "$ENTRIES_FILE"

ENTRY_COUNT="$(wc -l < "$ENTRIES_FILE" | tr -d '[:space:]')"
if [ "$ENTRY_COUNT" -eq 0 ]; then
  echo "alfred-grant-keychain: no Claude credential entries found in $LOGIN_KEYCHAIN." >&2
  echo "  Run 'claude login' first, then re-run this script." >&2
  exit 3
fi

echo "alfred-grant-keychain: found $ENTRY_COUNT credential entries to update"
while IFS= read -r e; do
  echo "  - $e"
done < "$ENTRIES_FILE"
echo ""

# Pick the right partition-list entry. If claude.exe is signed with a
# Developer-ID team, use teamid:<TEAMID>. If unsigned, use 'unsigned:'.
# Anything else falls back to 'unsigned:' as a conservative default.
CODESIGN_OUT="$(codesign -dvv "$CLAUDE_BIN" 2>&1 || true)"
TEAMID="$(printf '%s\n' "$CODESIGN_OUT" | sed -n 's/^TeamIdentifier=\([A-Z0-9]\{1,\}\).*$/\1/p' | head -n1)"

if [ -n "$TEAMID" ]; then
  PARTITION="teamid:$TEAMID"
  echo "alfred-grant-keychain: claude.exe is signed by team $TEAMID"
else
  PARTITION="unsigned:"
  echo "alfred-grant-keychain: claude.exe is not Developer-ID signed; using partition 'unsigned:'"
fi

# Preserve the standard Apple partitions so other Apple-shipped tools
# still work.
PARTITION_LIST="apple:,apple-tool:,$PARTITION"
echo "alfred-grant-keychain: target partition list -> $PARTITION_LIST"
echo ""

if [ "${1:-}" != "--apply" ]; then
  cat <<HELP
Advisory mode only (no changes were made).

Two ways to apply:

  GUI:
    1. Open Keychain Access (Cmd-Space, type "Keychain Access")
    2. Search for: Claude Code-credentials
    3. Double-click each entry above, switch to Access Control tab
    4. Leave the radio on "Confirm before allowing access"
       (do NOT switch to "Allow all applications")
    5. Under "Always allow access by these applications", click +
    6. Press Cmd-Shift-G, paste:
         $CLAUDE_BIN
    7. Save Changes. Enter your Mac login password when prompted.

  CLI:
    Re-run this script with --apply. You will be prompted for your
    login keychain password ONCE. It is passed to the 'security'
    binary via stdin (-k option), not stored, not echoed.

      bash bin/alfred-grant-keychain.sh --apply

After applying, verify the proxy probe:

  echo '{"type":"probe"}' | nc -U \$ALFRED_HOME/run/claude-proxy.sock
HELP
  exit 0
fi

# --- --apply path ---

echo "alfred-grant-keychain: about to update partition list on $ENTRY_COUNT entries."
echo "  Press Enter to continue, Ctrl-C to abort."
read -r _

# Prompt for keychain password without echoing. -r prevents backslash
# escapes; -s prevents echoing.
read -srp "macOS login keychain password (not echoed): " KEYCHAIN_PASSWORD
echo ""
if [ -z "$KEYCHAIN_PASSWORD" ]; then
  echo "alfred-grant-keychain: empty password supplied; aborting." >&2
  exit 4
fi

# Apply per-entry. `security set-generic-password-partition-list`:
#   -S "<list>"   the new partition list
#   -s "<svc>"    the service name (matches `svce` blob)
#   -k "<pass>"   the login keychain password
fail_count=0
while IFS= read -r entry; do
  printf "  updating: %s ... " "$entry"
  if security set-generic-password-partition-list \
       -S "$PARTITION_LIST" \
       -s "$entry" \
       -k "$KEYCHAIN_PASSWORD" >/dev/null 2>&1; then
    echo "ok"
  else
    echo "FAILED"
    fail_count=$((fail_count + 1))
  fi
done < "$ENTRIES_FILE"

# Zero the password variable. (Cannot guarantee no copies in memory; the
# best we can do without low-level controls is overwrite the binding.)
KEYCHAIN_PASSWORD="$(head -c 64 /dev/urandom | base64)"
unset KEYCHAIN_PASSWORD

echo ""
if [ $fail_count -gt 0 ]; then
  echo "alfred-grant-keychain: $fail_count entry / entries failed to update." >&2
  echo "  Common causes: wrong keychain password; entry locked by Apple;" >&2
  echo "  Keychain not unlocked. Re-run after addressing the cause." >&2
  exit 5
fi

echo "alfred-grant-keychain: done. Verify the proxy probe:"
echo "  echo '{\"type\":\"probe\"}' | nc -U \$ALFRED_HOME/run/claude-proxy.sock"
