#!/usr/bin/env bash
# Render every entry in agents.conf into a concrete .plist file.
#
# Usage:
#   ./render.sh [output_dir]
#
# Defaults the output dir to "$(dirname "$0")/_generated/" so the
# rendered plists sit next to the canonical hand-edited copies without
# stomping them. deploy.sh consumes the rendered copies.
#
# Substitutions:
#   __LABEL__              - launchd job label
#   __SCRIPT__             - script filename in HERMES_BIN
#   __SCHEDULE_BLOCK__     - either StartInterval or StartCalendarInterval
#   __PATH__               - colon-joined PATH for the EnvironmentVariables block
#   __JAVA_BLOCK__         - JAVA_HOME entry (empty when needs_java=no)
#   __HERMES_BIN__         - $HERMES_HOME/bin
#   __HERMES_HOME__        - resolves at render time from $HERMES_HOME or ~/.hermes
#   __WORKSPACE_ROOT__  - resolves at render time from $WORKSPACE_ROOT
#   __HOME__               - $HOME at render time
#   __LOG_STEM__           - basename for /tmp/<stem>.{stdout,stderr}
#
# launchd does not interpolate env vars inside plists, so these
# substitutions happen here at deploy time. Override HERMES_HOME or
# WORKSPACE_ROOT in the shell that runs render.sh to re-target the
# generated plists at a non-default layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/_template.plist"
CONF="$SCRIPT_DIR/agents.conf"
OUT_DIR="${1:-$SCRIPT_DIR/_generated}"

: "${HERMES_HOME:=$HOME/.hermes}"
: "${WORKSPACE_ROOT:=${WORKSPACE_ROOT:-$HOME/Workspace}}"

JAVA_HOME_DEFAULT="/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"
JAVA_BIN="$JAVA_HOME_DEFAULT/bin"
FNM_BIN="$HOME/.local/share/fnm/aliases/default/bin"
BREW_PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
JAVA_PATH="$JAVA_BIN:$FNM_BIN:$BREW_PATH"

mkdir -p "$OUT_DIR"

render_one() {
  local label="$1" script="$2" schedule="$3" needs_java="$4" log_stem="$5"
  [[ -z "$log_stem" ]] && log_stem="$label"

  local schedule_block
  case "$schedule" in
    interval:*)
      local secs="${schedule#interval:}"
      schedule_block="  <key>StartInterval</key>
  <integer>$secs</integer>"
      ;;
    cron:*:*:*)
      # weekly: cron:<weekday>:<HH>:<MM>
      local rest="${schedule#cron:}"
      local wday="${rest%%:*}"; rest="${rest#*:}"
      local hour="${rest%%:*}"; local minute="${rest#*:}"
      schedule_block="  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key>
    <integer>$wday</integer>
    <key>Hour</key>
    <integer>$hour</integer>
    <key>Minute</key>
    <integer>$minute</integer>
  </dict>"
      ;;
    cron:*:*)
      # daily: cron:<HH>:<MM>
      local rest="${schedule#cron:}"
      local hour="${rest%%:*}"; local minute="${rest#*:}"
      schedule_block="  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>$hour</integer>
    <key>Minute</key>
    <integer>$minute</integer>
  </dict>"
      ;;
    *)
      echo "render.sh: unknown schedule format '$schedule' for $label" >&2
      return 1
      ;;
  esac

  local path_value java_block
  if [[ "$needs_java" == "yes" ]]; then
    path_value="$JAVA_PATH"
    java_block="    <key>JAVA_HOME</key>
    <string>$JAVA_HOME_DEFAULT</string>"
  else
    path_value="$BREW_PATH"
    java_block=""
  fi

  local hermes_bin="$HERMES_HOME/bin"
  local out="$OUT_DIR/$label.plist"

  python3 - "$TEMPLATE" "$out" \
      "$label" "$script" "$schedule_block" "$path_value" "$java_block" \
      "$hermes_bin" "$HERMES_HOME" "$WORKSPACE_ROOT" "$HOME" "$log_stem" "${GH_ORG:-}" <<'PY'
import sys
template_path, out_path, label, script, schedule_block, path_value, java_block, \
    hermes_bin, hermes_home, workspace_root, home_dir, log_stem, gh_org = sys.argv[1:]
with open(template_path) as f:
    txt = f.read()
mapping = {
    "__LABEL__": label,
    "__SCRIPT__": script,
    "__SCHEDULE_BLOCK__": schedule_block,
    "__PATH__": path_value,
    "__JAVA_BLOCK__": java_block,
    "__HERMES_BIN__": hermes_bin,
    "__HERMES_HOME__": hermes_home,
    "__WORKSPACE_ROOT__": workspace_root,
    "__GH_ORG_BLOCK__": (
        f'    <key>GH_ORG</key>\n    <string>{gh_org}</string>'
        if gh_org else ""
    ),
    "__HOME__": home_dir,
    "__LOG_STEM__": log_stem,
}
for k, v in mapping.items():
    txt = txt.replace(k, v)
# Strip whitespace-only lines that the empty __JAVA_BLOCK__ leaves behind.
lines = [ln for ln in txt.splitlines() if ln.strip() or ln == ""]
cleaned = []
for ln in lines:
    if not ln.strip() and cleaned and not cleaned[-1].strip():
        continue
    if not ln.strip() and cleaned and cleaned[-1].endswith("</string>"):
        # Drop blank line that appeared where __JAVA_BLOCK__ used to be.
        continue
    cleaned.append(ln)
with open(out_path, "w") as f:
    f.write("\n".join(cleaned).rstrip() + "\n")
PY
}

while IFS=$'\t' read -r label script schedule needs_java log_stem rest; do
  [[ -z "$label" ]] && continue
  case "$label" in \#*) continue ;; esac
  render_one "$label" "$script" "$schedule" "${needs_java:-no}" "${log_stem:-}"
  echo "  rendered $label.plist"
done < "$CONF"

echo "[render] wrote $(ls -1 "$OUT_DIR" | wc -l | tr -d ' ') plists to $OUT_DIR"
