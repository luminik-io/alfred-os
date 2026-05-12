#!/usr/bin/env bash
# Weekly shipped-work Slack report wrapper.

set -eu

if [ -f "${HOME}/.alfredrc" ]; then
  # shellcheck disable=SC1091
  . "${HOME}/.alfredrc"
fi

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
exec "${HERMES_HOME}/bin/alfred-shipped-summary.py" --period weekly --slack "$@"
