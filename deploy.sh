#!/usr/bin/env bash
# alfred-os, deploy framework files into ${ALFRED_HOME}/{lib,bin}/.
#
# If launchd/agents.conf exists in this checkout, this script also renders
# and installs the scheduled jobs for the host OS: launchd plists on macOS,
# systemd --user timers on Linux. Without agents.conf it stays framework-only,
# which is the clean default for a fresh clone.
#
# Idempotent. Safe to re-run.
#
# Env vars (defaults shown):
#   ALFRED_HOME      = $HOME/.alfred
#   WORKSPACE_ROOT   = $HOME/code
# (Both flow into the rendered launchd plists / systemd units for any
# consumer that ships agents.)

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

echo "[alfred-os/deploy] copying lib/ (recursive: top-level modules + subpackages)"
# v0.4.0 introduced subpackages (agent_runner/, connectors/,
# fleet_brain/, memory/, server/). The original `cp lib/*.py` only matched
# top-level files, so the subpackages never deployed and every agent firing
# on a clean install crashed at first `from agent_runner import ...`.
# `cp -R lib/. <dest>/` recursively copies everything under lib/ including
# the dot-prefixed contents, preserving the package layout.
cp -R "$REPO_DIR/lib/." "$RUNTIME_LIB/"
# Restore file permissions; -R does not normalise modes uniformly.
find "$RUNTIME_LIB" -name '*.py' -type f -exec chmod 644 {} +
# Sanity check: assert the five v0.4.x subpackages landed. If any are
# missing the deploy is broken (the original silent-failure mode).
for pkg in agent_runner connectors fleet_brain memory server; do
  if [ ! -f "$RUNTIME_LIB/$pkg/__init__.py" ]; then
    echo "[alfred-os/deploy] ERROR: lib/$pkg/__init__.py missing after copy" >&2
    exit 1
  fi
done

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

# Render + install the systemd --user units on Linux. Mirrors the launchd
# path below: render from agents.conf, reap units for rows that were removed,
# install the current set, and skip enabling any agent whose pause marker is
# set. Units live in ~/.config/systemd/user; the .timer triggers the .service.
deploy_linux_systemd() {
  local conf="$1"
  # Honor ALFRED_SYSTEMD_USER_DIR so deploy installs to the same directory
  # lib/scheduler.py resolves units from; the default matches the scheduler's.
  local systemd_user_dir="${ALFRED_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
  local out_dir="$REPO_DIR/systemd/_generated"
  local pause_dir="$ALFRED_HOME/state/_paused"
  mkdir -p "$systemd_user_dir"

  echo "[alfred-os/deploy] rendering systemd user units from $conf"
  bash "$REPO_DIR/systemd/render.sh" "$out_dir"

  # Build the keep-list (labels deployed this run) so the reaper can spot
  # rows that were removed from agents.conf.
  local keep_list="" unit_path label short_name
  for unit_path in "$out_dir"/*.timer; do
    [ -e "$unit_path" ] || continue
    label="$(basename "$unit_path" .timer)"
    keep_list="${keep_list}${label}
"
  done

  echo "[alfred-os/deploy] reaping orphaned systemd units (rows removed from agents.conf)"
  local existing
  for existing in "$systemd_user_dir"/*.timer; do
    [ -e "$existing" ] || continue
    label="$(basename "$existing" .timer)"
    # Only reap units we manage: a matching .service must exist and the
    # label must not be in the current keep-list.
    [ -f "$systemd_user_dir/$label.service" ] || continue
    if ! printf '%s' "$keep_list" | grep -qx "$label"; then
      systemctl --user disable --now "$label.timer" >/dev/null 2>&1 || true
      rm -f "$existing" "$systemd_user_dir/$label.service"
      echo "  - $label removed (not present in current agents.conf)"
    fi
  done

  # An agents.conf with zero active rows (every line commented out) renders
  # no units. The reaper above still ran, so anything previously installed is
  # cleaned up; there is just nothing to install. Skip the copy/enable rather
  # than letting an unmatched glob reach `cp`.
  if [ -z "$keep_list" ]; then
    echo "[alfred-os/deploy] no agents in $conf; nothing to install"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    return 0
  fi

  echo "[alfred-os/deploy] installing systemd units into $systemd_user_dir"
  cp "$out_dir"/*.service "$out_dir"/*.timer "$systemd_user_dir/"
  systemctl --user daemon-reload >/dev/null 2>&1 || true

  for unit_path in "$out_dir"/*.timer; do
    [ -e "$unit_path" ] || continue
    label="$(basename "$unit_path" .timer)"
    short_name="${label##*.}"
    if [ -f "$pause_dir/$short_name" ]; then
      # Operator-paused via 'alfred pause'. Disable so the unit isn't
      # re-armed; the marker file stays the single source of truth.
      systemctl --user disable --now "$label.timer" >/dev/null 2>&1 || true
      echo "  - $label paused; installed but not enabled"
      continue
    fi
    if systemctl --user enable --now "$label.timer" >/dev/null 2>&1; then
      echo "  - $label enabled"
    else
      echo "  - $label (enable failed; see 'systemctl --user status $label.timer')"
    fi
  done

  echo "[alfred-os/deploy] active timers:"
  systemctl --user list-units --type=timer --state=active --no-legend 2>/dev/null \
    | awk '{print "  " $1}' || echo "  (none)"
}

CONF="$REPO_DIR/launchd/agents.conf"
if [ -f "$CONF" ]; then
  cp "$CONF" "$RUNTIME_LAUNCHD/agents.conf"

  if [ "$(uname -s)" = "Linux" ]; then
    deploy_linux_systemd "$CONF"
    echo "[alfred-os/deploy] done"
    exit 0
  fi

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
