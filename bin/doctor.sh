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

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${HERMES_HOME:=$HOME/.hermes}"
: "${LUMINIK_WORKSPACE:=$HOME/Workspace}"
: "${WORKSPACE_ROOT:=$LUMINIK_WORKSPACE}"
export HERMES_HOME WORKSPACE_ROOT LUMINIK_WORKSPACE HERMES_DOCTOR=1

# Mirror what launchd plists put on PATH so doctor.sh matches cron-time
# conditions even when invoked from a non-login subshell. fnm init is
# shell-rc-driven, so a bare `bash doctor.sh` wouldn't see `claude` or
# `npm` otherwise. Keeping these prepends idempotent: if they're already
# present (e.g. doctor.sh was run from the operator's interactive zsh)
# they no-op.
FNM_BIN="$HOME/.local/share/fnm/aliases/default/bin"
JAVA_BIN="/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home/bin"
case ":$PATH:" in
  *":$FNM_BIN:"*) ;;
  *) [ -d "$FNM_BIN" ] && PATH="$FNM_BIN:$PATH" ;;
esac
case ":$PATH:" in
  *":$JAVA_BIN:"*) ;;
  *) [ -d "$JAVA_BIN" ] && PATH="$JAVA_BIN:$PATH" ;;
esac
export PATH

# Prefer running the deployed scripts when present (they're the live runtime),
# fall back to the in-repo copies when not deployed yet (a fresh checkout
# wants to verify before its first deploy.sh run).
if [ -d "$HERMES_HOME/bin" ] && ls "$HERMES_HOME/bin"/*.py >/dev/null 2>&1; then
  BIN_DIR="$HERMES_HOME/bin"
else
  BIN_DIR="$REPO_DIR/bin"
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
  # so we can inspect the sentinel without flooding the terminal.
  output=$(timeout 30 python3 "$script" 2>&1) || true

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
