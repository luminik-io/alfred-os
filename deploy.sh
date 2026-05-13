#!/usr/bin/env bash
# alfred-os — deploy framework files into ${ALFRED_HOME}/{lib,bin}/.
#
# If launchd/agents.conf exists in this checkout, this script also renders
# and installs those launchd jobs. Without agents.conf it stays framework-only,
# which is the clean default for a fresh clone.
#
# Idempotent. Safe to re-run.
#
# Env vars (defaults shown):
#   ALFRED_HOME      = $HOME/.alfred
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

: "${ALFRED_HOME:=$HOME/.alfred}"
: "${WORKSPACE_ROOT:=$HOME/code}"
export ALFRED_HOME WORKSPACE_ROOT

RUNTIME_BIN="$ALFRED_HOME/bin"
RUNTIME_LIB="$ALFRED_HOME/lib"
RUNTIME_LAUNCHD="$ALFRED_HOME/launchd"
LOCAL_BIN="${HOME}/.local/bin"

mkdir -p "$RUNTIME_BIN" "$RUNTIME_LIB" "$RUNTIME_LAUNCHD" "$LOCAL_BIN"

echo "[alfred-os/deploy] ALFRED_HOME=$ALFRED_HOME WORKSPACE_ROOT=$WORKSPACE_ROOT"

echo "[alfred-os/deploy] copying lib/"
cp "$REPO_DIR/lib/"*.py "$RUNTIME_LIB/"
chmod 644 "$RUNTIME_LIB/"*.py

echo "[alfred-os/deploy] copying bin/ (every regular file)"
for f in "$REPO_DIR/bin/"*; do
  [ -f "$f" ] || continue
  cp "$f" "$RUNTIME_BIN/"
  chmod +x "$RUNTIME_BIN/$(basename "$f")"
done

if [ -f "$RUNTIME_BIN/alfred" ]; then
  ln -sfn "$RUNTIME_BIN/alfred" "$LOCAL_BIN/alfred"
  echo "[alfred-os/deploy] linked alfred → $LOCAL_BIN/alfred"
fi

if [ -f "$RUNTIME_BIN/alfred-init.py" ]; then
  ln -sfn "$RUNTIME_BIN/alfred-init.py" "$LOCAL_BIN/alfred-init"
  echo "[alfred-os/deploy] linked alfred-init → $LOCAL_BIN/alfred-init"
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
  cp "$CONF" "$RUNTIME_LAUNCHD/agents.conf"
  OUT_DIR="$REPO_DIR/launchd/_generated"
  echo "[alfred-os/deploy] rendering launchd plists from $CONF"
  bash "$REPO_DIR/launchd/render.sh" "$OUT_DIR"

  if [ "$(uname -s)" = "Darwin" ]; then
    LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$LAUNCH_AGENTS_DIR"
    UID_VALUE="$(id -u)"
    echo "[alfred-os/deploy] installing launchd plists to $LAUNCH_AGENTS_DIR"
    CURRENT_LABELS="$(mktemp -t alfred-os-labels-XXXXXX)"
    PREVIOUS_LABELS="$(mktemp -t alfred-os-previous-labels-XXXXXX)"
    MANAGED_LABELS_TMP="$(mktemp -t alfred-os-managed-labels-XXXXXX)"
    MANAGED_LABELS_FILE="$RUNTIME_LAUNCHD/managed-labels.txt"
    trap 'rm -f "$CURRENT_LABELS" "$PREVIOUS_LABELS" "$MANAGED_LABELS_TMP"' EXIT
    for plist in "$OUT_DIR"/*.plist; do
      [ -f "$plist" ] || continue
      label="$(basename "$plist" .plist)"
      echo "$label" >> "$CURRENT_LABELS"
    done
    sort -u "$CURRENT_LABELS" -o "$CURRENT_LABELS"
    if [ -f "$MANAGED_LABELS_FILE" ]; then
      sort -u "$MANAGED_LABELS_FILE" > "$PREVIOUS_LABELS"
    else
      : > "$PREVIOUS_LABELS"
    fi

    is_current_label() {
      grep -Fxq "$1" "$CURRENT_LABELS"
    }

    launchctl_pid_for_label() {
      local target="$1"
      launchctl list 2>/dev/null | awk -v label="$target" '$NF == label { print $1; exit }'
    }

    restart_loaded_label() {
      local label="$1"
      local plist="$2"
      local pid
      pid="$(launchctl_pid_for_label "$label")"
      if [ -n "$pid" ] && [ "$pid" != "-" ] && [ "${ALFRED_DEPLOY_RESTART_RUNNING:-0}" != "1" ]; then
        echo "  - $label running pid $pid; installed but reload deferred"
        return 0
      fi
      launchctl bootout "gui/$UID_VALUE" "$plist" >/dev/null 2>&1 || true
      launchctl bootstrap "gui/$UID_VALUE" "$plist"
      echo "  - $label loaded"
    }

    while IFS= read -r label || [ -n "$label" ]; do
      [ -n "$label" ] || continue
      is_current_label "$label" && continue
      existing="$LAUNCH_AGENTS_DIR/$label.plist"
      [ -f "$existing" ] || continue
      launchctl bootout "gui/$UID_VALUE" "$existing" >/dev/null 2>&1 || true
      rm -f "$existing"
      echo "  - $label removed (not present in current agents.conf)"
    done < "$PREVIOUS_LABELS"

    for plist in "$OUT_DIR"/*.plist; do
      [ -f "$plist" ] || continue
      label="$(basename "$plist" .plist)"
      agent="${label##*.}"
      dest="$LAUNCH_AGENTS_DIR/$label.plist"
      cp "$plist" "$dest"
      if [ -f "$ALFRED_HOME/state/_paused/$agent" ]; then
        echo "  - $label paused; installed but not bootstrapped"
        launchctl bootout "gui/$UID_VALUE" "$dest" >/dev/null 2>&1 || true
        continue
      fi
      restart_loaded_label "$label" "$dest"
    done
    cp "$CURRENT_LABELS" "$MANAGED_LABELS_TMP"
    mv "$MANAGED_LABELS_TMP" "$MANAGED_LABELS_FILE"
  else
    echo "[alfred-os/deploy] non-macOS host; rendered plists but skipped launchctl"
  fi
else
  echo "[alfred-os/deploy] no launchd/agents.conf found; framework-only deploy complete"
fi

echo "[alfred-os/deploy] done"
