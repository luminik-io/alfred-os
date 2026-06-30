#!/usr/bin/env bash
# doctor.sh - run preflight checks for every agent without burning a Claude turn.
#
# For each configured Python script, we set ALFRED_DOCTOR=1 and invoke the
# script. Each agent runs its preflight() and, on success, exits with a
# [<AGENT>-DOCTOR-OK] sentinel before doing any real work. On preflight miss
# the agent exits with a [<AGENT>-PREFLIGHT-FAILED] sentinel naming each gap.
#
# This is the canonical "is this host configured correctly?" check for both
# fresh forks and post-rotation verification (after AWS SSO refresh, after
# changing IAM policies, after `alfred claude swap`, etc.).
#
# Usage: doctor.sh [--dev] [--lifecycle]
#   --dev   Dev-install mode. Agents whose preflight() reports unconfigured
#           host state (missing GH auth, repo checkouts, secrets) are printed
#           but do NOT fail the run. Code-correctness failures (an agent that
#           crashes on import, a syntax error) still fail hard. install.sh
#           passes --dev on Linux hosts, which are a local dev lane rather
#           than a scheduled-fleet host.
#   --lifecycle
#           Run only the Batman lifecycle-path doctor. It feeds a synthetic
#           parent issue into the parser, validates bundle labels, probes the
#           Slack approval surface, and probes Claude OAuth auth.
#
# Exit code is 0 when every agent reports DOCTOR-OK, 1 if any agent fails.
# In --dev mode the exit code stays a real gate: it is 0 only when no
# code-correctness check failed.

set -uo pipefail

# --- arg parse -------------------------------------------------------------
DEV_MODE=0
LIFECYCLE_MODE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dev) DEV_MODE=1 ;;
    --lifecycle) LIFECYCLE_MODE=1 ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
    *) echo "doctor.sh: unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

load_env_file() {
  local file="$1" line key value quote_style
  [ -f "$file" ] || return 0
  env_value_quote_style() {
    case "$1" in
      \'*\') printf '%s' single ;;
      \"*\") printf '%s' double ;;
      *) printf '%s' none ;;
    esac
  }
  # Keep in lockstep with `decode_env_value` in lib/agent_runner/paths.py,
  # which the Python .env readers share. Changing the unquoting rules here
  # means changing them there too, or the two will disagree on a token value.
  decode_env_value() {
    local value="$1" sq="'" dq='"' splice
    splice="${sq}${dq}${sq}${dq}${sq}"
    case "$value" in
      \'*\')
        value="${value#\'}"
        value="${value%\'}"
        value="${value//$splice/$sq}"
        ;;
      \"*\")
        value="${value#\"}"
        value="${value%\"}"
        ;;
    esac
    printf '%s' "$value"
  }
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
    quote_style="$(env_value_quote_style "$value")"
    value="$(decode_env_value "$value")"
    if [ "$quote_style" != "single" ]; then
      value="${value//\$\{HOME\}/$HOME}"
      value="${value//\$HOME/$HOME}"
    fi
    export "$key=$value"
  done < "$file"
}

load_env_file "$HOME/.alfredrc"

: "${ALFRED_HOME:=$HOME/.alfred}"
: "${WORKSPACE_ROOT:=$HOME/code}"
export ALFRED_HOME WORKSPACE_ROOT ALFRED_DOCTOR=1

if [ -d "$REPO_DIR/lib" ]; then
  PYTHONPATH="$REPO_DIR/lib${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi

# Mirror what launchd plists put on PATH so doctor.sh matches firing-time
# conditions even when invoked from a non-login subshell. fnm init is
# shell-rc-driven, so a bare `bash doctor.sh` wouldn't see `claude` or
# `npm` otherwise. Keeping these prepends idempotent: if they're already
# present (e.g. doctor.sh was run from the operator's interactive zsh)
# they no-op.
prepend_path_if_dir() {
  case ":$PATH:" in
    *":$1:"*) ;;
    *) [ -d "$1" ] && PATH="$1:$PATH" ;;
  esac
}

LOCAL_BIN="$HOME/.local/bin"
FNM_BIN="$HOME/.local/share/fnm/aliases/default/bin"

# Detect openjdk@21 path via brew so this works on both Apple Silicon
# (`/opt/homebrew`) and Intel Macs (`/usr/local`). Fall back gracefully
# when brew isn't installed.
if command -v brew >/dev/null 2>&1; then
  JAVA_BREW_PREFIX="$(brew --prefix openjdk@21 2>/dev/null || true)"
else
  JAVA_BREW_PREFIX=""
fi
if [ -n "$JAVA_BREW_PREFIX" ]; then
  JAVA_BIN="$JAVA_BREW_PREFIX/libexec/openjdk.jdk/Contents/Home/bin"
else
  JAVA_BIN="/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home/bin"
fi

prepend_path_if_dir "$LOCAL_BIN"
prepend_path_if_dir "$FNM_BIN"
prepend_path_if_dir "$JAVA_BIN"
prepend_path_if_dir "/opt/homebrew/bin"
prepend_path_if_dir "/opt/homebrew/sbin"
prepend_path_if_dir "/usr/local/bin"
# Linux openjdk-21 lives under /usr/lib/jvm rather than a Homebrew prefix.
# These no-op on macOS (the directories do not exist) and mirror what
# systemd/render.sh puts on a Linux agent's PATH.
prepend_path_if_dir "/usr/lib/jvm/java-21-openjdk-amd64/bin"
prepend_path_if_dir "/usr/lib/jvm/java-21-openjdk-arm64/bin"
prepend_path_if_dir "/usr/lib/jvm/default-java/bin"
export PATH

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "doctor.sh: warning: ANTHROPIC_API_KEY is set; Claude Code prefers API keys over Pro/Max subscription auth. Alfred does not require it." >&2
fi
if [ -n "${OPENAI_API_KEY:-}" ]; then
  echo "doctor.sh: warning: OPENAI_API_KEY is set; Codex may use API billing instead of ChatGPT-plan auth. Alfred does not require it." >&2
fi

if [ "$LIFECYCLE_MODE" -eq 1 ]; then
  lifecycle_fixture="$REPO_DIR/lib/agent_runner/fixtures/lifecycle_doctor_body.md"
  lifecycle_python="python3"
  if [ -x "$ALFRED_HOME/venv/bin/python" ]; then
    lifecycle_python="$ALFRED_HOME/venv/bin/python"
  fi
  if [ -f "$lifecycle_fixture" ]; then
    "$lifecycle_python" -m agent_runner.lifecycle_doctor --fixture "$lifecycle_fixture"
  else
    "$lifecycle_python" -m agent_runner.lifecycle_doctor
  fi
  exit $?
fi

# macOS does not ship GNU coreutils' `timeout`. Auto-detect a usable
# implementation; on Linux this finds `timeout`, on a fresh Mac with
# `brew install coreutils` it finds `gtimeout`, and otherwise falls
# back to a tiny Python timeout wrapper.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="gtimeout"
else
  TIMEOUT_BIN=""
fi

# Run a command with a wall-clock cap if a timeout binary is available;
# otherwise use a stdlib Python wrapper. Usage:
# _run_with_timeout <secs> <cmd> [args...]
_run_with_timeout() {
  local secs="$1"; shift
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "$secs" "$@"
  else
    python3 - "$secs" "$@" <<'PY'
import subprocess
import sys

secs = int(sys.argv[1])
cmd = sys.argv[2:]
try:
    proc = subprocess.run(cmd, timeout=secs)
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.exit(proc.returncode)
PY
  fi
}

configured_agents() {
  local conf=""
  if [ -f "$ALFRED_HOME/launchd/agents.conf" ]; then
    conf="$ALFRED_HOME/launchd/agents.conf"
  elif [ -f "$REPO_DIR/launchd/agents.conf" ]; then
    conf="$REPO_DIR/launchd/agents.conf"
  fi
  if [ -n "$conf" ]; then
    awk -F'\t' '
      /^[[:space:]]*$/ { next }
      /^[[:space:]]*#/ { next }
      $2 ~ /\.py$/ { print $1 "\t" $2 }
    ' "$conf" | sort -u
    return 0
  fi

  if [ -d "$HOME/Library/LaunchAgents" ]; then
    python3 - "$HOME/Library/LaunchAgents" <<'PY'
from pathlib import Path
import plistlib
import sys

for plist in sorted(Path(sys.argv[1]).glob("*.plist")):
    try:
        data = plistlib.loads(plist.read_bytes())
    except Exception:
        continue
    label = str(data.get("Label") or "")
    if not label.startswith(("alfred.", "my.fleet.")):
        continue
    args = data.get("ProgramArguments") or []
    if not args:
        continue
    candidate = None
    if str(args[0]).endswith(".py"):
        candidate = args[0]
    elif Path(str(args[0])).name == "agent-launch" and len(args) > 1:
        candidate = args[1]
    if candidate and str(candidate).endswith(".py"):
        print(f"{label}\t{Path(str(candidate)).name}")
PY
    return 0
  fi

  # Linux mirror of the macOS plist fallback: inspect systemd --user units
  # under ~/.config/systemd/user and parse the ExecStart= line for the
  # wrapped script. Matches the macOS behavior of discovering deployed
  # agents when agents.conf is unreachable.
  systemd_user_dir="${ALFRED_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
  if [ -d "$systemd_user_dir" ]; then
    python3 - "$systemd_user_dir" <<'PY'
from pathlib import Path
import shlex
import sys

for unit in sorted(Path(sys.argv[1]).glob("*.service")):
    label = unit.stem
    if not label.startswith(("alfred.", "my.fleet.")):
        continue
    try:
        text = unit.read_text()
    except OSError:
        continue
    candidate = None
    for line in text.splitlines():
        if not line.startswith("ExecStart="):
            continue
        try:
            tokens = shlex.split(line[len("ExecStart="):])
        except ValueError:
            tokens = line[len("ExecStart="):].split()
        if not tokens:
            break
        if tokens[0].endswith(".py"):
            candidate = tokens[0]
        elif Path(tokens[0]).name == "agent-launch" and len(tokens) > 1:
            candidate = tokens[1]
        break
    if candidate and str(candidate).endswith(".py"):
        print(f"{label}\t{Path(str(candidate)).name}")
PY
  fi
}

echo "doctor: checking configured agents"
echo "        ALFRED_HOME=$ALFRED_HOME"
echo "        WORKSPACE_ROOT=$WORKSPACE_ROOT"
echo

# --------------------------------------------------------------------------
# Base-dep importability gate.
#
# install.sh provisions $ALFRED_HOME/venv with the runtime deps used by both
# scheduled agents and the native dashboard API. If an import fails, agents or
# `alfred serve` would crash at first use. Catch that here instead.
# --------------------------------------------------------------------------
venv_deps_fail=0
if [ -x "$ALFRED_HOME/venv/bin/python" ]; then
  venv_python="$ALFRED_HOME/venv/bin/python"
  printf "  %-30s " "alfred-venv base deps"
  if "$venv_python" -c "import boto3, fastapi, httpx, jinja2, slack_sdk, uvicorn" >/dev/null 2>&1; then
    echo "✅"
  elif [ "$DEV_MODE" = "1" ]; then
    echo "⚠️  --dev: runtime deps import failed in \$ALFRED_HOME/venv"
  else
    echo "❌ import boto3, fastapi, httpx, jinja2, slack_sdk, uvicorn failed against $venv_python"
    echo "       (re-run install.sh to repair, or pip install into the venv manually)"
    venv_deps_fail=1
  fi
elif [ "$DEV_MODE" = "1" ]; then
  printf "  %-30s ⚠️  --dev: \$ALFRED_HOME/venv not provisioned (agent-launch will fall through to system python3)\n" "alfred-venv base deps"
else
  printf "  %-30s ⚠️  \$ALFRED_HOME/venv not provisioned; scheduled agents and alfred serve may hit ModuleNotFoundError\n" "alfred-venv base deps"
  echo "       (re-run install.sh to provision)"
fi
echo

pass=0
fail=$venv_deps_fail
while IFS=$'\t' read -r label script_name; do
  [ -n "$label" ] || continue
  [ -n "$script_name" ] || continue
  if [ -f "$ALFRED_HOME/bin/$script_name" ]; then
    script="$ALFRED_HOME/bin/$script_name"
    launch_arg="$script_name"
  elif [ -f "$SCRIPT_DIR/$script_name" ]; then
    script="$SCRIPT_DIR/$script_name"
    launch_arg="$script"
  else
    printf "  %-30s ❌ missing\n" "${script_name%.py}"
    fail=$((fail + 1))
    continue
  fi
  name=$(basename "$script" .py)
  printf "  %-30s " "$name"

  # Each agent's preflight should complete in well under 30s. Capture stdout
  # so we can inspect the sentinel without flooding the terminal. Use the
  # detected timeout helper so this works on a fresh Mac without coreutils.
  launcher="$ALFRED_HOME/bin/agent-launch"
  if [ ! -x "$launcher" ]; then
    launcher="$SCRIPT_DIR/agent-launch"
  fi
  if [ -x "$launcher" ]; then
    output=$(_run_with_timeout 30 env ALFRED_DOCTOR=1 \
      AGENT_CODENAME="${label##*.}" LAUNCHD_LABEL="$label" \
      "$launcher" "$launch_arg" 2>&1) || true
  else
    output=$(_run_with_timeout 30 env ALFRED_DOCTOR=1 \
      AGENT_CODENAME="${label##*.}" LAUNCHD_LABEL="$label" \
      python3 "$script" 2>&1) || true
  fi

  if echo "$output" | grep -q "DOCTOR-OK"; then
    printf "✅ ok\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -qE "\[[A-Z0-9_-]+-LOCKED\]"; then
    printf "🟡 in flight\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -qE "\[[A-Za-z0-9_-]+-DISABLED\]"; then
    # Opt-in agent that hasn't been enabled via `alfred enable <name>`.
    # Disabled agents don't run, so a missing preflight is by design.
    printf "⚪ disabled\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -qE "\[[A-Za-z0-9_-]+-NO-URL\]"; then
    # Optional reporter is scheduled, but no ingest endpoint is configured.
    # This is a clean no-op state, not an agent crash.
    printf "⚪ no URL\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -qE "\[[A-Za-z0-9_-]+-PAUSED\]"; then
    # Operator-paused agents are intentionally quiet. Count them as healthy so
    # doctor can be used on machines where noisy jobs are deliberately paused.
    printf "⏸ paused\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -q "PREFLIGHT-FAILED"; then
    # The agent's code ran and its preflight() deliberately reported missing
    # host configuration (GH auth, repo checkouts, scoped secrets). In --dev
    # mode this is a known gap on a dev box, not a code defect, so surface it
    # without failing. Crashes never reach this branch, a broken import
    # exits without emitting the sentinel and lands in "unexpected output".
    if [ "$DEV_MODE" -eq 1 ]; then
      printf "⚠️  config gap (--dev: not fatal)\n"
      echo "$output" | sed -n '/PREFLIGHT-FAILED/,/^$/p' | sed 's/^/      /'
      pass=$((pass + 1))
    else
      printf "❌ preflight failed\n"
      echo "$output" | sed -n '/PREFLIGHT-FAILED/,/^$/p' | sed 's/^/      /'
      fail=$((fail + 1))
    fi
  else
    # Unexpected: preflight passed but the agent neither emitted DOCTOR-OK
    # nor exited cleanly. Most likely a script that pre-dates the preflight
    # rollout or a runtime error. This is a code-correctness failure and
    # stays hard even in --dev mode.
    printf "⚠️  unexpected output\n"
    echo "$output" | head -5 | sed 's/^/      /'
    fail=$((fail + 1))
  fi
done < <(configured_agents | sort -u)

echo
echo "doctor: $pass passed, $fail failed"
if [ "$DEV_MODE" -eq 1 ]; then
  echo "doctor: --dev mode, config gaps above (⚠️) were not counted as failures."
fi
[ "$fail" -eq 0 ]
