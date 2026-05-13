#!/usr/bin/env bash
# Daily shipped-work Slack report wrapper.

set -eu

if [ -f "${HOME}/.alfredrc" ]; then
  # shellcheck disable=SC1091
  . "${HOME}/.alfredrc"
fi

ALFRED_HOME="${ALFRED_HOME:-${HOME}/.alfred}"
export ALFRED_HOME
exec "${ALFRED_HOME}/bin/alfred-shipped-summary.py" --period daily --slack "$@"
