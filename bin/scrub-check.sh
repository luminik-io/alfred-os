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
# host-private paths, but is NOT exempt from the secret scan below, a
# secret accidentally pasted into a changelog entry must still trip.
PATH_ALLOWLIST_RE='^(\./)?(bin/scrub-check\.sh|\.github/workflows/ci\.yml|site/package-lock\.json|.*\.lock)$'

# Per-line allowlist for files that legitimately contain example codenames
# matching one of the generic private-identifier patterns.
PER_LINE_ALLOW_RE='^(\./)?(launchd/agents\.conf\.example|tests/test_alfred_init\.py)$'

# Secret allowlist: nothing should be exempt from the secret-pattern scan
# (Slack tokens, AWS access keys). The list is intentionally smaller than
# the path allowlist; CHANGELOG.md is deliberately omitted so a paste into
# a changelog entry trips the scan.
SECRET_ALLOWLIST_RE='^(\./)?(bin/scrub-check\.sh|\.github/workflows/ci\.yml|.*\.lock)$'

SKIP_PATH_RE='^(\./)?(\.git/|site/node_modules/)'

patterns=(
  # Operator home-directory paths (private workspace layout).
  "/Users/[A-Za-z0-9._-]+/Claude_Workspace"
  "/Users/[A-Za-z0-9._-]+/\\.alfred"
  "/Users/[A-Za-z0-9._-]+/\\.hermes"
  "/home/[A-Za-z0-9._-]+/Claude_Workspace"

  # Luminik internal product / org references. alfred-os legitimately ships
  # from luminik-io, but other product repo names are private. The "alfred"
  # bareword catches the predecessor private repo; the long alternation
  # catches every named Luminik product repo.
  "luminik-internal"
  "luminik-orchestrator"
  "luminik-io/alfred([^A-Za-z0-9_-]|$)"
  "luminik-io/luminik-(backend|frontend|mobile|nango|agents|data-acquisition|data-infra|specs|site|design-system)"
  "luminik-(backend|frontend|mobile|nango|agents|data-acquisition|data-infra)([^A-Za-z0-9_-]|$)"
  "[A-Za-z0-9._%+-]+@luminik\\.io"
  "[A-Za-z0-9._%+-]+@dataravel\\.com"

  # Luminik staging / production hostnames and AWS account ID.
  "app-staging\\.luminik\\.io"
  "(^|[^A-Za-z0-9])241533131716([^A-Za-z0-9]|$)"

  # Operator-private Slack channel and codename prefixes.
  "#alfred-fleet"
  "#luminik\\."
  "luminik\\.eng\\."

  # Predecessor / reference fleet references.
  "private predecessor"
  "predecessor fleet"
  "reference fleet"

  # Private env var aliases used internally.
  "LUMINIK_WORKSPACE"
  "LUMINIK_FOUNDER_SLACK"
  "OPERATOR_REDDIT_HANDLE"

  # Internal-only AWS profile naming (the literal value, not the env name).
  "AWS_PROFILE_FOR_HERMES=\"hermes-alfred"

  # Internal pipelines / surfaces.
  "slack/(staging|prod|production)/"
  "e2e/(staging|prod|production)/"
)

secret_patterns=(
  # Slack tokens / webhooks.
  "https://hooks\\.slack\\.com/services/[A-Z0-9]{8,}/[A-Z0-9]{8,}/[A-Za-z0-9]{20,}"
  "xox[baprs]-[A-Za-z0-9-]{20,}"
  "xapp-[A-Za-z0-9-]{20,}"

  # AWS access keys.
  "AKIA[0-9A-Z]{16}"
  "ASIA[0-9A-Z]{16}"

  # GitHub personal / app / OAuth tokens.
  "ghp_[A-Za-z0-9]{36,}"
  "github_pat_[A-Za-z0-9_]{82,}"
  "gho_[A-Za-z0-9]{36,}"
  "ghu_[A-Za-z0-9]{36,}"
  "ghs_[A-Za-z0-9]{36,}"

  # OpenAI / Anthropic API keys.
  "sk-[A-Za-z0-9]{20,}T3BlbkFJ[A-Za-z0-9]{20,}"
  "sk-ant-api03-[A-Za-z0-9_-]{40,}"

  # Generic private keys.
  "-----BEGIN (OPENSSH|RSA|EC|DSA|PGP) PRIVATE KEY-----"
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
