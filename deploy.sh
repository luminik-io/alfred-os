#!/usr/bin/env bash
# alfred-os — deploy framework files into ${HERMES_HOME}/{lib,bin}/ and
# install the launchd plist template renderer.
#
# This script deploys ONLY the framework. Codename-specific bin scripts and
# launchd/agents.conf live in the consuming repo (e.g. luminik-io/alfred,
# which imports alfred-os as a git submodule and calls this script before
# deploying its own per-codename pieces).
#
# Idempotent. Safe to re-run.
#
# Env vars (defaults shown):
#   HERMES_HOME      = $HOME/.hermes
#   WORKSPACE_ROOT   = $HOME/Workspace
# (Both flow into rendered launchd plists for any consumer that ships agents.)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${HERMES_HOME:=$HOME/.hermes}"
: "${WORKSPACE_ROOT:=$HOME/Workspace}"
export HERMES_HOME WORKSPACE_ROOT

HERMES_BIN="$HERMES_HOME/bin"
HERMES_LIB="$HERMES_HOME/lib"
LOCAL_BIN="${HOME}/.local/bin"

mkdir -p "$HERMES_BIN" "$HERMES_LIB" "$LOCAL_BIN"

echo "[alfred-os/deploy] HERMES_HOME=$HERMES_HOME WORKSPACE_ROOT=$WORKSPACE_ROOT"

echo "[alfred-os/deploy] copying lib/"
cp "$REPO_DIR/lib/"*.py "$HERMES_LIB/"
chmod 644 "$HERMES_LIB/"*.py

echo "[alfred-os/deploy] copying bin/ (every regular file)"
for f in "$REPO_DIR/bin/"*; do
  [ -f "$f" ] || continue
  cp "$f" "$HERMES_BIN/"
  chmod +x "$HERMES_BIN/$(basename "$f")"
done

# hermes-claude is invoked interactively from $PATH (not by launchd cron),
# so expose it under ~/.local/bin via a stable symlink that survives redeploys.
if [ -f "$HERMES_BIN/hermes-claude" ]; then
  ln -sfn "$HERMES_BIN/hermes-claude" "$LOCAL_BIN/hermes-claude"
  echo "[alfred-os/deploy] linked hermes-claude → $LOCAL_BIN/hermes-claude"
fi

# Some GUI-installed CLIs, notably Codex.app on macOS, live outside the
# launchd PATH even though the interactive shell can resolve them. If the
# operator already has codex on the interactive PATH, expose it through
# ~/.local/bin so rendered plists and doctor.sh resolve it consistently.
if command -v codex >/dev/null 2>&1; then
  CODEX_SOURCE="$(command -v codex)"
  if [ "$CODEX_SOURCE" != "$LOCAL_BIN/codex" ]; then
    ln -sfn "$CODEX_SOURCE" "$LOCAL_BIN/codex"
  fi
  echo "[alfred-os/deploy] linked codex → $LOCAL_BIN/codex"
fi

# doctor.sh ships into HERMES_BIN via the bin/ copy loop above. Consumer
# repos can wrap it with their own env defaults; alfred-os itself does
# NOT install a user-facing symlink — that's the consumer's choice.

echo "[alfred-os/deploy] framework deployed. Consumer-side deploy can now"
echo "[alfred-os/deploy] copy its own bin/<codename>.py and render plists"
echo "[alfred-os/deploy] from launchd/_template.plist + its own agents.conf."
