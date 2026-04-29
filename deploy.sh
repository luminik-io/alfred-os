#!/usr/bin/env bash
# pennyworth — deploy framework files into ${HERMES_HOME}/{lib,bin}/ and
# install the launchd plist template renderer.
#
# This script deploys ONLY the framework. Codename-specific bin scripts and
# launchd/agents.conf live in the consuming repo (e.g. luminik-io/alfred,
# which imports pennyworth as a git submodule and calls this script before
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

echo "[pennyworth/deploy] HERMES_HOME=$HERMES_HOME WORKSPACE_ROOT=$WORKSPACE_ROOT"

echo "[pennyworth/deploy] copying lib/"
cp "$REPO_DIR/lib/"*.py "$HERMES_LIB/"
chmod 644 "$HERMES_LIB/"*.py

echo "[pennyworth/deploy] copying bin/ (every regular file)"
for f in "$REPO_DIR/bin/"*; do
  [ -f "$f" ] || continue
  cp "$f" "$HERMES_BIN/"
  chmod +x "$HERMES_BIN/$(basename "$f")"
done

# hermes-claude is invoked interactively from $PATH (not by launchd cron),
# so expose it under ~/.local/bin via a stable symlink that survives redeploys.
if [ -f "$HERMES_BIN/hermes-claude" ]; then
  ln -sfn "$HERMES_BIN/hermes-claude" "$LOCAL_BIN/hermes-claude"
  echo "[pennyworth/deploy] linked hermes-claude → $LOCAL_BIN/hermes-claude"
fi

# doctor.sh too — it's a tool the operator runs by hand or invokes via
# their own deploy.sh wrapper. Make it discoverable on PATH.
if [ -f "$HERMES_BIN/doctor.sh" ]; then
  ln -sfn "$HERMES_BIN/doctor.sh" "$LOCAL_BIN/pennyworth-doctor"
  echo "[pennyworth/deploy] linked doctor.sh → $LOCAL_BIN/pennyworth-doctor"
fi

echo "[pennyworth/deploy] framework deployed. Consumer-side deploy can now"
echo "[pennyworth/deploy] copy its own bin/<codename>.py and render plists"
echo "[pennyworth/deploy] from launchd/_template.plist + its own agents.conf."
