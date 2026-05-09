#!/usr/bin/env bash
# alfred-os — deploy framework files into ${HERMES_HOME}/{lib,bin}/.
#
# If launchd/agents.conf exists in this checkout, this script also renders
# and installs those launchd jobs. Without agents.conf it stays framework-only,
# which is the clean default for a fresh clone.
#
# Idempotent. Safe to re-run.
#
# Env vars (defaults shown):
#   HERMES_HOME      = $HOME/.hermes
#   WORKSPACE_ROOT   = $HOME/code
# (Both flow into rendered launchd plists for any consumer that ships agents.)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

load_env_file() {
  local file="$1" line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    case "$line" in
      export\ *) line="${line#export }" ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ''|[0-9]*|*[!A-Za-z0-9_]*)
        continue
        ;;
    esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    value="${value//\$\{HOME\}/$HOME}"
    value="${value//\$HOME/$HOME}"
    export "$key=$value"
  done < "$file"
}

load_env_file "$HOME/.alfredrc"

: "${HERMES_HOME:=$HOME/.hermes}"
: "${WORKSPACE_ROOT:=$HOME/code}"
export HERMES_HOME WORKSPACE_ROOT

HERMES_BIN="$HERMES_HOME/bin"
HERMES_LIB="$HERMES_HOME/lib"
HERMES_LAUNCHD="$HERMES_HOME/launchd"
LOCAL_BIN="${HOME}/.local/bin"

mkdir -p "$HERMES_BIN" "$HERMES_LIB" "$HERMES_LAUNCHD" "$LOCAL_BIN"

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

if [ -f "$HERMES_BIN/alfred" ]; then
  ln -sfn "$HERMES_BIN/alfred" "$LOCAL_BIN/alfred"
  echo "[alfred-os/deploy] linked alfred → $LOCAL_BIN/alfred"
fi

if [ -f "$HERMES_BIN/alfred-init.py" ]; then
  ln -sfn "$HERMES_BIN/alfred-init.py" "$LOCAL_BIN/alfred-init"
  echo "[alfred-os/deploy] linked alfred-init → $LOCAL_BIN/alfred-init"
fi

# hermes-claude is invoked interactively from $PATH (not by launchd jobs),
# so expose it under ~/.local/bin via a stable symlink that survives redeploys.
if [ -f "$HERMES_BIN/hermes-claude" ]; then
  ln -sfn "$HERMES_BIN/hermes-claude" "$LOCAL_BIN/hermes-claude"
  echo "[alfred-os/deploy] linked hermes-claude → $LOCAL_BIN/hermes-claude"
fi

if command -v claude >/dev/null 2>&1; then
  CLAUDE_SOURCE="$(command -v claude)"
  if [ "$CLAUDE_SOURCE" != "$LOCAL_BIN/claude" ]; then
    ln -sfn "$CLAUDE_SOURCE" "$LOCAL_BIN/claude"
  fi
  echo "[alfred-os/deploy] linked claude → $LOCAL_BIN/claude"
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

CONF="$REPO_DIR/launchd/agents.conf"
if [ -f "$CONF" ]; then
  cp "$CONF" "$HERMES_LAUNCHD/agents.conf"
  OUT_DIR="$REPO_DIR/launchd/_generated"
  echo "[alfred-os/deploy] rendering launchd plists from $CONF"
  bash "$REPO_DIR/launchd/render.sh" "$OUT_DIR"

  if [ "$(uname -s)" = "Darwin" ]; then
    LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS_DIR"
    UID_VALUE="$(id -u)"
    echo "[alfred-os/deploy] installing launchd plists to $LAUNCH_AGENTS_DIR"
    CURRENT_LABELS="$(mktemp -t alfred-os-labels-XXXXXX)"
    CURRENT_PREFIXES="$(mktemp -t alfred-os-prefixes-XXXXXX)"
    trap 'rm -f "$CURRENT_LABELS" "$CURRENT_PREFIXES"' EXIT
    for plist in "$OUT_DIR"/*.plist; do
      [ -f "$plist" ] || continue
      label="$(basename "$plist" .plist)"
      echo "$label" >> "$CURRENT_LABELS"
      prefix="${label%.*}"
      if [ "$prefix" != "$label" ]; then
        echo "$prefix." >> "$CURRENT_PREFIXES"
      fi
    done
    sort -u "$CURRENT_LABELS" -o "$CURRENT_LABELS"
    sort -u "$CURRENT_PREFIXES" -o "$CURRENT_PREFIXES"

    is_current_label() {
      grep -Fxq "$1" "$CURRENT_LABELS"
    }

    has_managed_prefix() {
      local label="$1" prefix
      [ -s "$CURRENT_PREFIXES" ] || return 1
      while IFS= read -r prefix; do
        [ -n "$prefix" ] || continue
        case "$label" in
          "$prefix"*) return 0 ;;
        esac
      done < "$CURRENT_PREFIXES"
      return 1
    }

    for existing in "$LAUNCH_AGENTS_DIR"/*.plist; do
      [ -f "$existing" ] || continue
      label="$(basename "$existing" .plist)"
      is_current_label "$label" && continue
      has_managed_prefix "$label" || continue
      launchctl bootout "gui/$UID_VALUE" "$existing" >/dev/null 2>&1 || true
      rm -f "$existing"
      echo "  - $label removed (not present in current agents.conf)"
    done
    for plist in "$OUT_DIR"/*.plist; do
      [ -f "$plist" ] || continue
      label="$(basename "$plist" .plist)"
      agent="${label##*.}"
      dest="$LAUNCH_AGENTS_DIR/$label.plist"
      cp "$plist" "$dest"
      if [ -f "$HERMES_HOME/state/_paused/$agent" ]; then
        echo "  - $label paused; installed but not bootstrapped"
        launchctl bootout "gui/$UID_VALUE" "$dest" >/dev/null 2>&1 || true
        continue
      fi
      launchctl bootout "gui/$UID_VALUE" "$dest" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$UID_VALUE" "$dest"
      echo "  - $label loaded"
    done
  else
    echo "[alfred-os/deploy] non-macOS host; rendered plists but skipped launchctl"
  fi
else
  echo "[alfred-os/deploy] no launchd/agents.conf found; framework-only deploy complete"
fi

echo "[alfred-os/deploy] done"
