#!/usr/bin/env bash
# Weekly shipped-work Slack report wrapper.

set -eu

ALFRED_HOME="${ALFRED_HOME:-${HOME}/.alfred}"
if [ -f "${ALFRED_HOME}/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${ALFRED_HOME}/.env"
  set +a
fi
export ALFRED_HOME
exec "${ALFRED_HOME}/bin/alfred-shipped-summary.py" --period weekly --slack "$@"
