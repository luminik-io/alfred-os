#!/usr/bin/env bash
# fleet-recap - end-of-day digest wrapper.
#
# Sources ${HOME}/.alfredrc if present so the operator's env vars (GH_ORG,
# HERMES_HOME, etc.) are available, then exec's the alfred-status reporter
# in --slack mode. Configurable via env: FLEET_RECAP_STATUS_BIN points at
# the status reporter (default: ${HERMES_HOME}/bin/alfred-status.py).

set -eu

if [ -f "${HOME}/.alfredrc" ]; then
  # shellcheck disable=SC1091
  . "${HOME}/.alfredrc"
fi

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
STATUS_BIN="${FLEET_RECAP_STATUS_BIN:-${HERMES_HOME}/bin/alfred-status.py}"

if [ ! -x "${STATUS_BIN}" ]; then
  echo "[FLEET-RECAP-IDLE] status reporter not found at ${STATUS_BIN}" >&2
  exit 0
fi

exec "${STATUS_BIN}" --slack "$@"
