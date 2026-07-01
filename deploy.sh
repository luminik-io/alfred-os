#!/usr/bin/env bash
# alfred-os, deploy framework files into ${ALFRED_HOME}/{lib,bin}/.
#
# If launchd/agents.conf exists in this checkout, this script also renders
# and installs the scheduled jobs for the host OS: launchd plists on macOS,
# systemd --user timers on Linux. Without agents.conf it stays framework-only
# on fresh clones, unless custom-agent rows or a previous Alfred scheduler
# ledger require rendering/reaping.
#
# Idempotent. Safe to re-run.
#
# Env vars (defaults shown):
#   ALFRED_HOME      = $HOME/.alfred
#   WORKSPACE_ROOT   = $HOME/code
# These flow into the rendered launchd plists / systemd units for any
# consumer that ships agents.

set -euo pipefail

SYSTEMD_LABEL_TMPFILES=()

cleanup_systemd_label_tmpfiles() {
  if [ "${#SYSTEMD_LABEL_TMPFILES[@]}" -gt 0 ]; then
    rm -f "${SYSTEMD_LABEL_TMPFILES[@]}"
  fi
}

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

strip_inline_comment() {
  local value="$1" ch quote="" escaped=0 i previous=""
  for ((i = 0; i < ${#value}; i++)); do
    ch="${value:i:1}"
    if [ "$escaped" -eq 1 ]; then
      escaped=0
      previous="$ch"
      continue
    fi
    if [ "$ch" = "\\" ] && [ "$quote" != "'" ]; then
      escaped=1
      previous="$ch"
      continue
    fi
    if [ -n "$quote" ]; then
      if [ "$ch" = "$quote" ]; then
        quote=""
      fi
      previous="$ch"
      continue
    fi
    if [ "$ch" = "'" ] || [ "$ch" = '"' ]; then
      quote="$ch"
      previous="$ch"
      continue
    fi
    if [ "$ch" = "#" ] && [ -n "$previous" ] && [[ "$previous" =~ [[:space:]] ]]; then
      printf '%s' "${value:0:i}"
      return
    fi
    previous="$ch"
  done
  printf '%s' "$value"
}

trim_env_value() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

load_env_file() {
  local file="$1" no_clobber="${2:-}" line key value
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
    value="$(trim_env_value "$(strip_inline_comment "$value")")"
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    value="${value//\$\{HOME\}/$HOME}"
    value="${value//\$HOME/$HOME}"
    if [ -n "$no_clobber" ] && [ -n "${!key+x}" ]; then
      continue
    fi
    export "$key=$value"
  done < "$file"
}

expand_user_path() {
  local path="$1" expanded=""
  case "$path" in
    "~") printf '%s' "$HOME" ;;
    "~"/*) printf '%s/%s' "$HOME" "${path#\~/}" ;;
    "~"*)
      expanded="$(python3 - "$path" <<'PY' 2>/dev/null || true
import os
import sys

print(os.path.expanduser(sys.argv[1]))
PY
)"
      if [ -n "$expanded" ]; then
        printf '%s' "$expanded"
      else
        printf '%s' "$path"
      fi
      ;;
    "%h") printf '%s' "$HOME" ;;
    "%h"/*) printf '%s/%s' "$HOME" "${path#%h/}" ;;
    *) printf '%s' "$path" ;;
  esac
}

: "${ALFRED_HOME:=$HOME/.alfred}"
ALFRED_HOME="$(expand_user_path "$ALFRED_HOME")"
load_env_file "$ALFRED_HOME/.env" no_clobber
: "${WORKSPACE_ROOT:=$HOME/code}"
WORKSPACE_ROOT="$(expand_user_path "$WORKSPACE_ROOT")"
export ALFRED_HOME WORKSPACE_ROOT

RUNTIME_BIN="$ALFRED_HOME/bin"
RUNTIME_LIB="$ALFRED_HOME/lib"
RUNTIME_LAUNCHD="$ALFRED_HOME/launchd"
RUNTIME_PROMPTS="$ALFRED_HOME/prompts"
LOCAL_BIN="${HOME}/.local/bin"

mkdir -p "$RUNTIME_BIN" "$RUNTIME_LIB" "$RUNTIME_LAUNCHD" "$RUNTIME_PROMPTS" "$LOCAL_BIN"

echo "[alfred-os/deploy] ALFRED_HOME=$ALFRED_HOME WORKSPACE_ROOT=$WORKSPACE_ROOT"

printf '%s\n' "$REPO_DIR" > "$RUNTIME_LAUNCHD/source-repo.txt"
chmod 644 "$RUNTIME_LAUNCHD/source-repo.txt"
rm -f "$RUNTIME_LAUNCHD/alfredrc.path"

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

ensure_runtime_python_deps() {
  local venv_python="$ALFRED_HOME/venv/bin/python"
  [ "${ALFRED_DEPLOY_SKIP_PYTHON_DEPS:-0}" = "1" ] && return 0
  [ -x "$venv_python" ] || return 0
  if "$venv_python" -c "import boto3, fastapi, httpx, jinja2, slack_sdk, uvicorn" >/dev/null 2>&1; then
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "[alfred-os/deploy] $venv_python is missing runtime deps; uv not found, so run install.sh to repair"
    return 0
  fi
  echo "[alfred-os/deploy] installing Alfred runtime Python deps into $ALFRED_HOME/venv"
  if ! uv pip install --python "$venv_python" \
    "slack-sdk>=3.27" \
    "boto3>=1.34" \
    "fastapi>=0.110" \
    "httpx>=0.27" \
    "uvicorn>=0.27" \
    "jinja2>=3.1" >/dev/null; then
    echo "[alfred-os/deploy] WARNING: runtime dependency install failed; re-run install.sh to repair" >&2
    return 0
  fi
  if ! "$venv_python" -c "import boto3, fastapi, httpx, jinja2, slack_sdk, uvicorn" >/dev/null 2>&1; then
    echo "[alfred-os/deploy] WARNING: runtime dependency install completed, but imports still fail; re-run install.sh to repair" >&2
  fi
}

ensure_runtime_python_deps

if [ -f "$REPO_DIR/prompts/spec-interrogator.md" ]; then
  cp "$REPO_DIR/prompts/spec-interrogator.md" "$RUNTIME_PROMPTS/spec-interrogator.md"
  chmod 644 "$RUNTIME_PROMPTS/spec-interrogator.md"
  echo "[alfred-os/deploy] copied prompts/spec-interrogator.md"
fi

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

install_ams_service_linux() {
  local systemd_user_dir="${ALFRED_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
  local service="$systemd_user_dir/alfred-ams.service"
  mkdir -p "$systemd_user_dir"
  cat > "$service" <<EOF
[Unit]
Description=Alfred Redis Agent Memory Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$RUNTIME_BIN/ams-launch.sh
Restart=always
RestartSec=5
WorkingDirectory=$HOME
Environment=PATH=$LOCAL_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=$HOME
Environment=ALFRED_HOME=$ALFRED_HOME
Environment=WORKSPACE_ROOT=$WORKSPACE_ROOT

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if systemctl --user enable --now alfred-ams.service >/dev/null 2>&1; then
    if systemctl --user restart alfred-ams.service >/dev/null 2>&1; then
      echo "[alfred-os/deploy] alfred-ams.service enabled and restarted"
    else
      echo "[alfred-os/deploy] alfred-ams.service enabled; restart failed, see 'systemctl --user status alfred-ams.service'"
    fi
  else
    echo "[alfred-os/deploy] alfred-ams.service installed; enable failed, see 'systemctl --user status alfred-ams.service'"
  fi
}

install_ams_service_launchd() {
  local launch_agents_dir="$HOME/Library/LaunchAgents"
  local plist="$launch_agents_dir/io.luminik.alfred.ams.plist"
  local uid_value
  mkdir -p "$launch_agents_dir"
  uid_value="$(id -u)"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.luminik.alfred.ams</string>
  <key>ProgramArguments</key>
  <array>
    <string>$RUNTIME_BIN/ams-launch.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/alfred-ams.stdout</string>
  <key>StandardErrorPath</key>
  <string>/tmp/alfred-ams.stderr</string>
  <key>WorkingDirectory</key>
  <string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$LOCAL_BIN:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>$HOME</string>
    <key>ALFRED_HOME</key>
    <string>$ALFRED_HOME</string>
    <key>WORKSPACE_ROOT</key>
    <string>$WORKSPACE_ROOT</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$uid_value" "$plist" >/dev/null 2>&1 || true
  if launchctl bootstrap "gui/$uid_value" "$plist" >/dev/null 2>&1; then
    echo "[alfred-os/deploy] io.luminik.alfred.ams loaded"
  else
    echo "[alfred-os/deploy] io.luminik.alfred.ams installed; bootstrap failed, see /tmp/alfred-ams.stderr"
  fi
}

has_enabled_custom_agents() {
  PYTHONPATH="$RUNTIME_LIB${PYTHONPATH:+:$PYTHONPATH}" python3 - "$ALFRED_HOME" <<'PY'
import sys
from pathlib import Path

try:
    from custom_agents import CustomAgentError, CustomAgentStore
except Exception:
    raise SystemExit(1)

try:
    rows = CustomAgentStore.from_state_root(Path(sys.argv[1]) / "state").conf_rows(
        enabled_only=True,
        strict=True,
    )
except CustomAgentError as exc:
    print(f"[alfred-os/deploy] custom agent manifest invalid: {exc}", file=sys.stderr)
    raise SystemExit(2)

raise SystemExit(0 if rows else 1)
PY
}

is_alfred_systemd_service_file() {
  local service_file="$1"
  [ -f "$service_file" ] || return 1
  grep -Eq '^Description=alfred-os[[:space:]]' "$service_file"
}

existing_systemd_managed_labels() {
  local systemd_user_dir="${ALFRED_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
  local timer_path label service_path
  for timer_path in "$systemd_user_dir"/*.timer; do
    [ -e "$timer_path" ] || continue
    label="$(basename "$timer_path" .timer)"
    service_path="$systemd_user_dir/$label.service"
    is_alfred_systemd_service_file "$service_path" || continue
    echo "$label"
  done | sort -u
}

has_existing_systemd_managed_labels() {
  [ "$(uname -s)" = "Linux" ] || return 1
  [ -n "$(existing_systemd_managed_labels)" ]
}

has_previous_managed_scheduler_labels() {
  local labels_file
  if [ "$(uname -s)" = "Linux" ]; then
    labels_file="$ALFRED_HOME/systemd/managed-labels.txt"
    [ -s "$labels_file" ] || has_existing_systemd_managed_labels
  else
    labels_file="$RUNTIME_LAUNCHD/managed-labels.txt"
    [ -s "$labels_file" ]
  fi
}

if [ "$(uname -s)" = "Linux" ]; then
  install_ams_service_linux
elif [ "$(uname -s)" = "Darwin" ]; then
  install_ams_service_launchd
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
  local runtime_systemd="$ALFRED_HOME/systemd"
  local managed_labels_file="$runtime_systemd/managed-labels.txt"
  local out_dir="$REPO_DIR/systemd/_generated"
  local pause_dir="$ALFRED_HOME/state/_paused"
  mkdir -p "$systemd_user_dir" "$runtime_systemd"

  echo "[alfred-os/deploy] rendering systemd user units from $conf"
  bash "$REPO_DIR/systemd/render.sh" "$out_dir"

  # Build the keep-list (labels deployed this run) so the reaper can spot rows
  # that were removed from agents.conf. The reaper is intentionally scoped to
  # the persisted Alfred-managed ledger; a fresh/custom-only checkout must not
  # sweep unrelated user timers just because they share a .service basename.
  local current_labels previous_labels managed_labels_tmp unit_path label short_name
  current_labels="$(mktemp -t alfred-os-systemd-current-XXXXXX)"
  previous_labels="$(mktemp -t alfred-os-systemd-previous-XXXXXX)"
  managed_labels_tmp="$(mktemp -t alfred-os-systemd-managed-XXXXXX)"
  SYSTEMD_LABEL_TMPFILES=("$current_labels" "$previous_labels" "$managed_labels_tmp")
  trap 'cleanup_systemd_label_tmpfiles' EXIT

  for unit_path in "$out_dir"/*.timer; do
    [ -e "$unit_path" ] || continue
    label="$(basename "$unit_path" .timer)"
    echo "$label" >> "$current_labels"
  done
  sort -u "$current_labels" -o "$current_labels"
  if [ -f "$managed_labels_file" ]; then
    sort -u "$managed_labels_file" > "$previous_labels"
  else
    existing_systemd_managed_labels > "$previous_labels"
  fi

  is_current_systemd_label() {
    grep -Fxq "$1" "$current_labels"
  }

  persist_systemd_managed_labels() {
    cp "$current_labels" "$managed_labels_tmp"
    mv "$managed_labels_tmp" "$managed_labels_file"
    cleanup_systemd_label_tmpfiles
    SYSTEMD_LABEL_TMPFILES=()
  }

  echo "[alfred-os/deploy] reaping orphaned systemd units (previous Alfred-managed rows removed from agents.conf)"
  while IFS= read -r label || [ -n "$label" ]; do
    [ -n "$label" ] || continue
    is_current_systemd_label "$label" && continue
    [ -f "$systemd_user_dir/$label.timer" ] || continue
    [ -f "$systemd_user_dir/$label.service" ] || continue
    systemctl --user disable --now "$label.timer" >/dev/null 2>&1 || true
    rm -f "$systemd_user_dir/$label.timer" "$systemd_user_dir/$label.service"
    echo "  - $label removed (not present in current agents.conf)"
  done < "$previous_labels"

  # An agents.conf with zero active rows (every line commented out) renders
  # no units. The reaper above still ran, so anything previously installed is
  # cleaned up; there is just nothing to install. Skip the copy/enable rather
  # than letting an unmatched glob reach `cp`.
  if [ ! -s "$current_labels" ]; then
    echo "[alfred-os/deploy] no agents in $conf; nothing to install"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    persist_systemd_managed_labels
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
  persist_systemd_managed_labels

  echo "[alfred-os/deploy] active timers:"
  systemctl --user list-units --type=timer --state=active --no-legend 2>/dev/null \
    | awk '{print "  " $1}' || echo "  (none)"
}

CONF="$REPO_DIR/launchd/agents.conf"
if [ -f "$CONF" ]; then
  cp "$CONF" "$RUNTIME_LAUNCHD/agents.conf"
else
  : > "$RUNTIME_LAUNCHD/agents.conf"
  echo "[alfred-os/deploy] no launchd/agents.conf found; using an empty base roster"
fi

if [ ! -f "$CONF" ]; then
  has_custom_agents=0
  custom_agent_status=0
  if has_enabled_custom_agents; then
    has_custom_agents=1
  else
    custom_agent_status=$?
  fi
  if [ "$custom_agent_status" -gt 1 ]; then
    exit "$custom_agent_status"
  fi
  if [ "$has_custom_agents" -eq 0 ] && ! has_previous_managed_scheduler_labels; then
    echo "[alfred-os/deploy] no launchd/agents.conf or enabled custom agents found; framework-only deploy complete"
    echo "[alfred-os/deploy] done"
    exit 0
  fi
  if [ "$(uname -s)" = "Linux" ] && [ ! -f "$REPO_DIR/systemd/render.sh" ]; then
    echo "[alfred-os/deploy] no systemd renderer found; framework-only deploy complete"
    echo "[alfred-os/deploy] done"
    exit 0
  fi
  if [ "$(uname -s)" != "Linux" ] && [ ! -f "$REPO_DIR/launchd/render.sh" ]; then
    echo "[alfred-os/deploy] no launchd renderer found; framework-only deploy complete"
    echo "[alfred-os/deploy] done"
    exit 0
  fi
fi

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

echo "[alfred-os/deploy] done"
