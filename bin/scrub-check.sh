#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v git >/dev/null 2>&1; then
  echo "scrub-check: git is required" >&2
  exit 2
fi

# Path-leak allowlist: files exempt from the path / private-identifier scan.
# CHANGELOG.md is allowed here so historical entries can mention prior
# host-private paths, but is NOT exempt from the secret scan below — a
# secret accidentally pasted into a changelog entry must still trip.
PATH_ALLOWLIST_RE='^(\./)?(bin/scrub-check\.sh|\.github/workflows/ci\.yml|CHANGELOG\.md|site/package-lock\.json|.*\.lock)$'

# Per-line allowlist for files that legitimately contain example codenames
# matching one of our private-identifier patterns (e.g. "my.fleet.<example>").
# Used for agents.conf* example files and the test suite that exercises
# AWS profile mapping with fictional codenames.
PER_LINE_ALLOW_RE='^(\./)?(launchd/agents\.conf\.example|infra/agents/launchd/agents\.conf\.example|tests/test_alfred_init\.py)$'

# Secret allowlist: nothing should be exempt from the secret-pattern scan
# (Slack tokens, AWS access keys). The list is intentionally smaller than
# the path allowlist; CHANGELOG.md is deliberately omitted so a paste into
# a changelog entry trips the scan.
SECRET_ALLOWLIST_RE='^(\./)?(bin/scrub-check\.sh|\.github/workflows/ci\.yml|.*\.lock)$'

SKIP_PATH_RE='^(\./)?(\.git/|site/node_modules/|infra/agents/launchd/_generated/)'

patterns=(
  "/Users/batman"
  "/Users/prasad"
  "/home/prasad"
  "luminik-internal"
  "prasad@luminik\\.io"
  "C0ATTT5DDGA"
  "T024P63979U"
  "huntress-cron"
  "slack/staging/"
  "e2e/staging/"
  "my\\.fleet\\.huntress\\b"
)

secret_patterns=(
  "https://hooks\\.slack\\.com/services/[A-Z0-9]{8,}/[A-Z0-9]{8,}/[A-Za-z0-9]{20,}"
  "xox[baprs]-[A-Za-z0-9-]{20,}"
  "xapp-[A-Za-z0-9-]{20,}"
  "AKIA[0-9A-Z]{16}"
  "ASIA[0-9A-Z]{16}"
)

candidate_files() {
  local allowlist_re="$1"
  local path
  while IFS= read -r -d "" path; do
    [ -n "$path" ] || continue
    [[ "$path" =~ $allowlist_re ]] && continue
    [[ "$path" =~ $SKIP_PATH_RE ]] && continue
    printf "%s\0" "$path"
  done < <(
    git ls-files -z
    git ls-files --others --exclude-standard -z
  )
}

scan_patterns() {
  local label="$1"
  local allowlist_re="$2"
  local per_line_skip="$3"
  shift 3
  local fail=0

  for pat in "$@"; do
    # Collect raw matches first.
    local matches
    if ! matches=$(candidate_files "$allowlist_re" | xargs -0 grep -InE "$pat" -- 2>/dev/null); then
      continue
    fi

    if [ -n "$per_line_skip" ]; then
      # Drop matches whose file path matches the per-line allow regex.
      local filtered=""
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        local file_part="${line%%:*}"
        if [[ "$file_part" =~ $per_line_skip ]]; then
          continue
        fi
        filtered+="$line"$'\n'
      done <<< "$matches"
      matches="${filtered%$'\n'}"
    fi

    if [ -n "$matches" ]; then
      printf '%s\n' "$matches"
      echo "::error::Found $label pattern: $pat" >&2
      fail=1
    fi
  done

  return "$fail"
}

fail=0
scan_patterns "private path or identifier" "$PATH_ALLOWLIST_RE" "$PER_LINE_ALLOW_RE" "${patterns[@]}" || fail=1
scan_patterns "secret" "$SECRET_ALLOWLIST_RE" "" "${secret_patterns[@]}" || fail=1

if [ "$fail" -ne 0 ]; then
  echo "scrub-check: failed" >&2
  exit 1
fi

echo "scrub-check: clean"
