#!/usr/bin/env bash
# doctor.sh - run preflight checks for every agent without burning a Claude turn.
#
# For each script in ${HERMES_HOME}/bin/, we set HERMES_DOCTOR=1 and invoke the
# script. Each agent runs its preflight() and, on success, exits with a
# [<AGENT>-DOCTOR-OK] sentinel before doing any real work. On preflight miss
# the agent exits with a [<AGENT>-PREFLIGHT-FAILED] sentinel naming each gap.
#
# This is the canonical "is this host configured correctly?" check for both
# fresh forks and post-rotation verification (after AWS SSO refresh, after
# changing IAM policies, after `hermes-claude swap`, etc.).
#
# Exit code is 0 when every agent reports DOCTOR-OK, 1 if any agent fails.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

: "${HERMES_HOME:=$HOME/.hermes}"
: "${WORKSPACE_ROOT:=$HOME/Workspace}"
export HERMES_HOME WORKSPACE_ROOT HERMES_DOCTOR=1

if [ -d "$REPO_DIR/lib" ]; then
  PYTHONPATH="$REPO_DIR/lib${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
fi

# Mirror what launchd plists put on PATH so doctor.sh matches cron-time
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
export PATH

# macOS does not ship GNU coreutils' `timeout`. Auto-detect a usable
# implementation; on Linux this finds `timeout`, on a fresh Mac with
# `brew install coreutils` it finds `gtimeout`, and otherwise falls
# back to running the command without a wall-clock cap. Recommended:
# `brew install coreutils` for parity with Linux behaviour.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_BIN="gtimeout"
else
  TIMEOUT_BIN=""
fi

# Run a command with a wall-clock cap if a timeout binary is available;
# otherwise just run it. Usage: _run_with_timeout <secs> <cmd> [args...]
_run_with_timeout() {
  local secs="$1"; shift
  if [ -n "$TIMEOUT_BIN" ]; then
    "$TIMEOUT_BIN" "$secs" "$@"
  else
    "$@"
  fi
}

# Prefer running the deployed scripts when present (they're the live runtime),
# fall back to the in-repo copies when not deployed yet (a fresh checkout
# wants to verify before its first deploy.sh run).
if [ -d "$HERMES_HOME/bin" ] && ls "$HERMES_HOME/bin"/*.py >/dev/null 2>&1; then
  BIN_DIR="$HERMES_HOME/bin"
else
  BIN_DIR="$SCRIPT_DIR"
fi

echo "doctor: checking agents under $BIN_DIR"
echo "        HERMES_HOME=$HERMES_HOME"
echo "        WORKSPACE_ROOT=$WORKSPACE_ROOT"
echo

pass=0
fail=0
for script in "$BIN_DIR"/*.py; do
  [ -f "$script" ] || continue
  name=$(basename "$script" .py)
  printf "  %-30s " "$name"

  # Each agent's preflight should complete in well under 30s. Capture stdout
  # so we can inspect the sentinel without flooding the terminal. Use the
  # detected timeout helper so this works on a fresh Mac without coreutils.
  output=$(_run_with_timeout 30 python3 "$script" 2>&1) || true

  if echo "$output" | grep -q "DOCTOR-OK"; then
    printf "✅ ok\n"
    pass=$((pass + 1))
  elif echo "$output" | grep -q "PREFLIGHT-FAILED"; then
    printf "❌ preflight failed\n"
    echo "$output" | sed -n '/PREFLIGHT-FAILED/,/^$/p' | sed 's/^/      /'
    fail=$((fail + 1))
  else
    # Unexpected: preflight passed but the agent neither emitted DOCTOR-OK
    # nor exited cleanly. Most likely a script that pre-dates the preflight
    # rollout or a runtime error.
    printf "⚠️  unexpected output\n"
    echo "$output" | head -5 | sed 's/^/      /'
    fail=$((fail + 1))
  fi
done

echo
echo "doctor: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
