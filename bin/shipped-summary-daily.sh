#!/usr/bin/env bash
# Daily shipped-work Slack report wrapper.

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=bin/alfred-dotenv.sh
. "${SCRIPT_DIR}/alfred-dotenv.sh"

ALFRED_HOME="${ALFRED_HOME:-${HOME}/.alfred}"
ALFRED_HOME="$(alfred_expand_user_path "$ALFRED_HOME")"
alfred_load_env_file "${ALFRED_HOME}/.env" no_clobber
export ALFRED_HOME
exec "${ALFRED_HOME}/bin/alfred-shipped-summary.py" --period daily --slack "$@"
