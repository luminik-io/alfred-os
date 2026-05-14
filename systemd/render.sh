#!/usr/bin/env bash
# Render every entry in ../launchd/agents.conf into a concrete pair of
# systemd --user units (.service + .timer).
#
# Usage:
#   ./render.sh [output_dir]
#
# Defaults the output dir to "$(dirname "$0")/_generated/". deploy.sh
# consumes the rendered copies on Linux hosts; on macOS the launchd
# renderer is used instead.
#
# agents.conf is the single source of truth for both schedulers, the same
# tab-separated six-column schema feeds launchd/render.sh and this script.
# Columns: label, script, schedule, needs_java, log_stem, role.
#
# Substitutions in _template.service / _template.timer:
#   __LABEL__              - systemd unit basename (the agents.conf label)
#   __SCRIPT__             - script filename passed to agent-launch
#   __SCHEDULE_BLOCK__     - OnCalendar= or OnUnitActiveSec= line
#   __PATH__               - colon-joined PATH for the Environment= block
#   __JAVA_BLOCK__         - Environment=JAVA_HOME=... when needs_java=yes
#   __ALFRED_BIN__         - $ALFRED_HOME/bin
#   __ALFRED_HOME__        - resolves at render time from $ALFRED_HOME
#   __WORKSPACE_ROOT__     - resolves at render time from $WORKSPACE_ROOT
#   __HOME__               - $HOME at render time
#   __LOG_STEM__           - basename for /tmp/<stem>.{stdout,stderr}
#   __AGENT_SHORT__        - label suffix, used as AGENT_CODENAME
#   __GH_ORG_BLOCK__       - Environment=GH_ORG=... when GH_ORG is set
#   __ROLE_BLOCK__         - Environment=ALFRED_<AGENT>_ROLE=<descriptor>
#                            from agents.conf column 6. Empty -> no env var.
#
# Schedule mapping (from agents.conf column 3):
#   interval:N        -> OnUnitActiveSec=Ns
#                        Matches the launchd RunAtLoad=false + StartInterval=N
#                        shape (first fire N seconds after timer activation,
#                        then every N).
#   cron:HH:MM        -> OnCalendar=*-*-* HH:MM:00
#   cron:W:HH:MM      -> OnCalendar=<Sun..Sat> *-*-* HH:MM:00
#                        (W=0..6 with 0=Sun)
#
# Override ALFRED_HOME or WORKSPACE_ROOT in the shell that runs render.sh
# to re-target the generated units at a non-default layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_TEMPLATE="$SCRIPT_DIR/_template.service"
TIMER_TEMPLATE="$SCRIPT_DIR/_template.timer"
CONF="$SCRIPT_DIR/../launchd/agents.conf"
OUT_DIR="${1:-$SCRIPT_DIR/_generated}"

if [[ ! -f "$CONF" ]]; then
  echo "render.sh: agents.conf not found at $CONF" >&2
  echo "render.sh: copy launchd/agents.conf.example to launchd/agents.conf and edit it before running deploy.sh." >&2
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

load_env_file "$HOME/.alfredrc"

: "${ALFRED_HOME:=$HOME/.alfred}"
: "${WORKSPACE_ROOT:=${WORKSPACE_ROOT:-$HOME/code}}"
export ALFRED_HOME WORKSPACE_ROOT

# Linux JAVA_HOME / PATH derivation. Unlike macOS, there is no Homebrew
# openjdk@21 prefix to interrogate; derive from `command -v java` and fall
# back to the Debian/Ubuntu openjdk-21 layout. needs_java=yes agents that
# find no java still render, but JAVA_HOME is omitted with a warning.
java_home_default() {
  local java_bin java_home jvm
  java_bin="$(command -v java 2>/dev/null || true)"
  if [[ -n "$java_bin" ]]; then
    java_bin="$(readlink -f "$java_bin")"
    java_home="$(dirname "$(dirname "$java_bin")")"
    printf '%s\n' "$java_home"
    return 0
  fi
  for jvm in \
    /usr/lib/jvm/java-21-openjdk-arm64 \
    /usr/lib/jvm/java-21-openjdk-amd64 \
    /usr/lib/jvm/default-java; do
    if [[ -d "$jvm" ]]; then
      printf '%s\n' "$jvm"
      return 0
    fi
  done
  printf '\n'
}

JAVA_HOME_DEFAULT="$(java_home_default)"
JAVA_BIN=""
if [[ -n "$JAVA_HOME_DEFAULT" ]]; then
  JAVA_BIN="$JAVA_HOME_DEFAULT/bin"
fi

FNM_BIN="$HOME/.local/share/fnm/aliases/default/bin"
LOCAL_BIN="$HOME/.local/bin"
BASE_PATH="$LOCAL_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -n "$JAVA_BIN" ]]; then
  JAVA_PATH="$JAVA_BIN:$FNM_BIN:$BASE_PATH"
else
  JAVA_PATH="$FNM_BIN:$BASE_PATH"
fi

mkdir -p "$OUT_DIR"
find "$OUT_DIR" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) -delete

render_one() {
  local label="$1" script="$2" schedule="$3" needs_java="$4" log_stem="$5" role="${6:-}"
  [[ -z "$log_stem" ]] && log_stem="$label"

  # Derive the agent short-name (last dot-segment) for the per-agent
  # ALFRED_<AGENT>_ROLE env-var key and AGENT_CODENAME. e.g.
  # my.fleet.lucius -> lucius.
  local agent_short="${label##*.}"

  local schedule_block
  case "$schedule" in
    interval:*)
      local secs="${schedule#interval:}"
      schedule_block="OnUnitActiveSec=${secs}s"
      ;;
    cron:*:*:*)
      # weekly: cron:<weekday>:<HH>:<MM>
      local rest="${schedule#cron:}"
      local wday="${rest%%:*}"; rest="${rest#*:}"
      local hour="${rest%%:*}"; local minute="${rest#*:}"
      local dow
      case "$wday" in
        0) dow="Sun" ;; 1) dow="Mon" ;; 2) dow="Tue" ;; 3) dow="Wed" ;;
        4) dow="Thu" ;; 5) dow="Fri" ;; 6) dow="Sat" ;;
        *) echo "render.sh: invalid weekday '$wday' for $label" >&2; return 1 ;;
      esac
      schedule_block="OnCalendar=$dow *-*-* $(printf '%02d' "$hour"):$(printf '%02d' "$minute"):00"
      ;;
    cron:*:*)
      # daily: cron:<HH>:<MM>
      local rest="${schedule#cron:}"
      local hour="${rest%%:*}"; local minute="${rest#*:}"
      schedule_block="OnCalendar=*-*-* $(printf '%02d' "$hour"):$(printf '%02d' "$minute"):00"
      ;;
    *)
      echo "render.sh: unknown schedule format '$schedule' for $label" >&2
      return 1
      ;;
  esac

  local path_value java_block
  if [[ "$needs_java" == "yes" ]]; then
    path_value="$JAVA_PATH"
    if [[ -n "$JAVA_HOME_DEFAULT" ]]; then
      java_block="Environment=JAVA_HOME=$JAVA_HOME_DEFAULT"
    else
      echo "render.sh: warning: $label needs_java=yes but no java found on PATH or /usr/lib/jvm; JAVA_HOME omitted" >&2
      java_block=""
    fi
  else
    path_value="$BASE_PATH"
    java_block=""
  fi

  local alfred_bin="$ALFRED_HOME/bin"
  local service_out="$OUT_DIR/$label.service"
  local timer_out="$OUT_DIR/$label.timer"

  python3 - "$SERVICE_TEMPLATE" "$TIMER_TEMPLATE" "$service_out" "$timer_out" \
      "$label" "$script" "$schedule_block" "$path_value" "$java_block" \
      "$alfred_bin" "$ALFRED_HOME" "$WORKSPACE_ROOT" "$HOME" "$log_stem" "${GH_ORG:-}" \
      "$agent_short" "$role" <<'PY'
import sys

(service_template, timer_template, service_out, timer_out, label, script,
 schedule_block, path_value, java_block, alfred_bin, alfred_home,
 workspace_root, home_dir, log_stem, gh_org, agent_short, role) = sys.argv[1:]

with open(service_template) as f:
    service_txt = f.read()
with open(timer_template) as f:
    timer_txt = f.read()


def env_line(key, value):
    # systemd splits Environment=KEY=VAL on whitespace, treating each token
    # as its own KEY=VAL assignment. Quote values containing spaces so they
    # survive as a single env var. Embedded `"` are unlikely in agents.conf
    # today but escape just in case.
    if " " in value or "\t" in value:
        value = '"' + value.replace('"', '\\"') + '"'
    return f"Environment={key}={value}"


role_block = ""
if role:
    env_key = "ALFRED_" + agent_short.upper().replace("-", "_") + "_ROLE"
    role_block = env_line(env_key, role)
gh_org_block = env_line("GH_ORG", gh_org) if gh_org else ""

mapping = {
    "__LABEL__": label,
    "__SCRIPT__": script,
    "__SCHEDULE_BLOCK__": schedule_block,
    "__PATH__": path_value,
    "__JAVA_BLOCK__": java_block,
    "__ALFRED_BIN__": alfred_bin,
    "__ALFRED_HOME__": alfred_home,
    "__WORKSPACE_ROOT__": workspace_root,
    "__AGENT_SHORT__": agent_short,
    "__GH_ORG_BLOCK__": gh_org_block,
    "__HOME__": home_dir,
    "__LOG_STEM__": log_stem,
    "__ROLE_BLOCK__": role_block,
}


def render(txt):
    for k, v in mapping.items():
        txt = txt.replace(k, v)
    # Replace the render-host's literal $HOME with systemd's %h specifier so
    # the rendered units are operator-agnostic. systemd expands %h to the
    # invoking user's home at unit-load time. Skip when home_dir is empty or
    # "/" to avoid corrupting absolute paths. Done before blank-line cleanup
    # so the replacement cannot accidentally re-introduce blank lines.
    if home_dir and home_dir != "/":
        txt = txt.replace(home_dir, "%h")
    # Collapse consecutive blank lines to one. Empty __JAVA_BLOCK__ /
    # __ROLE_BLOCK__ / __GH_ORG_BLOCK__ each leave a blank line behind.
    out = []
    prev_blank = False
    for ln in txt.splitlines():
        blank = (ln.strip() == "")
        if blank and prev_blank:
            continue
        out.append(ln)
        prev_blank = blank
    while out and out[0].strip() == "":
        out.pop(0)
    return "\n".join(out).rstrip() + "\n"


with open(service_out, "w") as f:
    f.write(render(service_txt))
with open(timer_out, "w") as f:
    f.write(render(timer_txt))
PY
}

# Bash treats tab as a whitespace IFS char and collapses consecutive tabs
# into one separator, which corrupts empty middle columns. Pre-expand each
# record into a non-whitespace field separator (\x1f) so read preserves
# empties.
awk -F'\t' '
  /^[[:space:]]*$/ { next }
  /^[[:space:]]*#/ { next }
  { printf "%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n", $1, $2, $3, $4, $5, $6 }
' "$CONF" | while IFS=$'\x1f' read -r label script schedule needs_java log_stem role; do
  [[ -z "$label" ]] && continue
  render_one "$label" "$script" "$schedule" "${needs_java:-no}" "${log_stem:-}" "${role:-}"
  echo "  rendered $label.service + .timer"
done

unit_count="$(find "$OUT_DIR" -maxdepth 1 -type f \( -name '*.service' -o -name '*.timer' \) | wc -l | tr -d ' ')"
echo "[render] wrote $unit_count unit files to $OUT_DIR"
