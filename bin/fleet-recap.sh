#!/usr/bin/env bash
# fleet-recap - end-of-day digest wrapper.
#
# Loads ${ALFRED_HOME}/.env if present, then execs the alfred-status reporter
# in --slack mode. Configurable via env: FLEET_RECAP_STATUS_BIN points at the
# status reporter (default: ${ALFRED_HOME}/bin/alfred-status.py).

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/alfred-dotenv.sh
. "${SCRIPT_DIR}/alfred-dotenv.sh"

ALFRED_HOME="${ALFRED_HOME:-${HOME}/.alfred}"
ALFRED_HOME="$(alfred_expand_user_path "$ALFRED_HOME")"
alfred_load_env_file "${ALFRED_HOME}/.env" no_clobber
export ALFRED_HOME
STATUS_BIN="${FLEET_RECAP_STATUS_BIN:-${ALFRED_HOME}/bin/alfred-status.py}"

if [ ! -x "${STATUS_BIN}" ]; then
  echo "[FLEET-RECAP-IDLE] status reporter not found at ${STATUS_BIN}" >&2
  exit 0
fi

exec "${STATUS_BIN}" --slack "$@"
