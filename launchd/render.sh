#!/usr/bin/env bash
# Render every entry in agents.conf into a concrete .plist file.
#
# Usage:
#   ./render.sh [output_dir]
#
# Defaults the output dir to "$(dirname "$0")/_generated/" so rendered
# snapshots sit next to the template and agents.conf. deploy.sh consumes the
# rendered copies.
#
# Substitutions:
#   __LABEL__              - launchd job label
#   __SCRIPT__             - script filename in ALFRED_BIN, passed to agent-launch
#   __SCHEDULE_BLOCK__     - either StartInterval or StartCalendarInterval
#   __PATH__               - colon-joined PATH for the EnvironmentVariables block
#   __JAVA_BLOCK__         - JAVA_HOME entry (empty when needs_java=no)
#   __ALFRED_BIN__         - $ALFRED_HOME/bin
#   __ALFRED_HOME__        - resolves at render time from $ALFRED_HOME or ~/.alfred
#   __ALFREDRC__           - resolves at render time from $ALFREDRC or ~/.alfredrc
#   __WORKSPACE_ROOT__  - resolves at render time from $WORKSPACE_ROOT
#   __HOME__               - $HOME at render time
#   __LOG_STEM__           - basename for /tmp/<stem>.{stdout,stderr}
#   __AGENT_SHORT__        - label suffix, used as AGENT_CODENAME
#   __ROLE_BLOCK__         - ALFRED_<AGENT>_ROLE=<one-line descriptor>
#                            from agents.conf column 6. Empty -> no env
#                            var. Read by agent_role() so Slack post
#                            prefixes and operator CLI surface the
#                            human-readable role next to the codename.
#
# launchd does not interpolate env vars inside plists, so these
# substitutions happen here at deploy time. Override ALFRED_HOME or
# WORKSPACE_ROOT in the shell that runs render.sh to re-target the
# generated plists at a non-default layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/_template.plist"
CONF="$SCRIPT_DIR/agents.conf"
OUT_DIR="${1:-$SCRIPT_DIR/_generated}"

if [[ ! -f "$CONF" ]]; then
  echo "render.sh: agents.conf not found at $CONF" >&2
  echo "render.sh: copy agents.conf.example to agents.conf and edit it before running deploy.sh." >&2
  exit 1
fi

load_env_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
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

: "${ALFREDRC:=$HOME/.alfredrc}"
load_env_file "$ALFREDRC"

: "${ALFRED_HOME:=$HOME/.alfred}"
: "${WORKSPACE_ROOT:=${WORKSPACE_ROOT:-$HOME/code}}"
export ALFRED_HOME WORKSPACE_ROOT ALFREDRC

# Detect openjdk@21 install path at render time so this works across
# Apple Silicon (`/opt/homebrew`), Intel Macs (`/usr/local`), and Linux
# Homebrew (`/home/linuxbrew/.linuxbrew`). Fall back to a sensible default
# if `brew` is missing entirely.
if command -v brew >/dev/null 2>&1; then
  JAVA_BREW_PREFIX="$(brew --prefix openjdk@21 2>/dev/null || true)"
else
  JAVA_BREW_PREFIX=""
fi
if [[ -n "$JAVA_BREW_PREFIX" ]]; then
  JAVA_HOME_DEFAULT="$JAVA_BREW_PREFIX/libexec/openjdk.jdk/Contents/Home"
else
  JAVA_HOME_DEFAULT="/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"
fi
JAVA_BIN="$JAVA_HOME_DEFAULT/bin"
FNM_BIN="$HOME/.local/share/fnm/aliases/default/bin"
LOCAL_BIN="$HOME/.local/bin"
BREW_PATH="$LOCAL_BIN:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
JAVA_PATH="$JAVA_BIN:$FNM_BIN:$BREW_PATH"

mkdir -p "$OUT_DIR"
find "$OUT_DIR" -maxdepth 1 -type f -name '*.plist' -delete

render_one() {
  local label="$1" script="$2" schedule="$3" needs_java="$4" log_stem="$5" role="${6:-}"
  [[ -z "$log_stem" ]] && log_stem="$label"

  # Derive the agent short-name (last dot-segment) for the per-agent
  # ALFRED_<AGENT>_ROLE env-var key. e.g. my.fleet.lucius -> lucius.
  local agent_short="${label##*.}"

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

  local alfred_bin="$ALFRED_HOME/bin"
  local out="$OUT_DIR/$label.plist"

  python3 - "$TEMPLATE" "$out" \
      "$label" "$script" "$schedule_block" "$path_value" "$java_block" \
      "$alfred_bin" "$ALFRED_HOME" "$ALFREDRC" "$WORKSPACE_ROOT" "$HOME" "$log_stem" "${GH_ORG:-}" \
      "$agent_short" "$role" <<'PY'
import sys
from xml.sax.saxutils import escape
template_path, out_path, label, script, schedule_block, path_value, java_block, \
    alfred_bin, alfred_home, alfredrc, workspace_root, home_dir, log_stem, gh_org, \
    agent_short, role = sys.argv[1:]
with open(template_path) as f:
    txt = f.read()
role_block = ""
if role:
    env_key = "ALFRED_" + agent_short.upper().replace("-", "_") + "_ROLE"
    # Roles can in principle contain & < > characters; escape them so
    # the rendered plist remains valid XML. Real-world values today are
    # plain ASCII, but the escape is cheap insurance.
    role_block = (
        f'    <key>{env_key}</key>\n'
        f'    <string>{escape(role)}</string>'
    )
mapping = {
    "__LABEL__": label,
    "__SCRIPT__": script,
    "__SCHEDULE_BLOCK__": schedule_block,
    "__PATH__": path_value,
    "__JAVA_BLOCK__": java_block,
    "__ALFRED_BIN__": alfred_bin,
    "__ALFRED_HOME__": alfred_home,
    "__ALFREDRC__": alfredrc,
    "__WORKSPACE_ROOT__": workspace_root,
    "__AGENT_SHORT__": agent_short,
    "__GH_ORG_BLOCK__": (
        f'    <key>GH_ORG</key>\n    <string>{gh_org}</string>'
        if gh_org else ""
    ),
    "__HOME__": home_dir,
    "__LOG_STEM__": log_stem,
    "__ROLE_BLOCK__": role_block,
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

# Bash treats tab as a whitespace IFS char and collapses consecutive tabs
# into one separator, which corrupts empty middle columns (an unset
# log_stem with a role still set turns into "log_stem=<role>, role=<empty>").
# Pre-expand each record into a non-whitespace field separator (\x1f) so
# read preserves empties.
awk -F'\t' '
  /^[[:space:]]*$/ { next }
  /^[[:space:]]*#/ { next }
  { printf "%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n", $1, $2, $3, $4, $5, $6 }
' "$CONF" | while IFS=$'\x1f' read -r label script schedule needs_java log_stem role; do
  [[ -z "$label" ]] && continue
  render_one "$label" "$script" "$schedule" "${needs_java:-no}" "${log_stem:-}" "${role:-}"
  echo "  rendered $label.plist"
done

plist_count="$(find "$OUT_DIR" -maxdepth 1 -type f -name '*.plist' | wc -l | tr -d ' ')"
echo "[render] wrote $plist_count plists to $OUT_DIR"
