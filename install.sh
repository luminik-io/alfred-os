#!/usr/bin/env bash
# alfred-os — fresh-machine bootstrap.
#
# What this script does (idempotent — safe to re-run):
#   1. Checks macOS (the framework currently runs on launchd; Linux users
#      should follow docs/LINUX.md for the current limitations).
#   2. Installs Homebrew if missing.
#   3. Installs the CLI tools every alfred-os fleet needs: python@3.11, git,
#      gh, jq, awscli, uv (fast Python runner used by the test suite).
#   4. Installs Claude Code (the @anthropic-ai/claude-code CLI) via npm.
#   5. Creates $ALFRED_HOME and $WORKSPACE_ROOT if missing.
#   6. Drops a starter ~/.alfredrc from .alfredrc.example and
#      prompts for the values it cannot infer.
#   7. Appends ~/.alfredrc sourcing to your shell rc so
#      every new shell sees them (skipped if already present).
#   8. Prints the exact next 3 commands you should run.
#
# What this script deliberately does NOT do (you'll see WHY in the printed
# next-steps):
#   - Authenticate gh / aws / claude. Those require interactive auth flows
#     and we want the operator to see what's happening.
#   - Create AWS IAM users or Secrets Manager entries. One-time decisions
#     better made with eyes on the AWS console.
#   - Create a Slack incoming webhook. Same reason.
#   - Run deploy.sh. The launchd plist install side-effects; the operator
#     should pull the trigger after reading what's about to load.
#   - Touch runtime data outside ~/.alfred.
#
# Non-interactive mode: set ALFRED_NONINTERACTIVE=1 and the script
# uses defaults for every prompt. Set GH_ORG / OPERATOR_NAME /
# OPERATOR_EMAIL in the env to override the defaults non-interactively.

set -euo pipefail

# --------------------------------------------------------------------------
# Pretty output
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_BLUE='\033[1;34m'
  C_GREEN='\033[1;32m'
  C_YELLOW='\033[1;33m'
  C_RED='\033[1;31m'
  C_DIM='\033[2m'
  C_OFF='\033[0m'
else
  C_BLUE='' C_GREEN='' C_YELLOW='' C_RED='' C_DIM='' C_OFF=''
fi

step()  { printf "${C_BLUE}==>${C_OFF} %s\n" "$*"; }
ok()    { printf "${C_GREEN}  ok${C_OFF} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}  !${C_OFF}  %s\n" "$*" >&2; }
die()   { printf "${C_RED}  !!${C_OFF} %s\n" "$*" >&2; exit 1; }
note()  { printf "${C_DIM}     %s${C_OFF}\n" "$*"; }

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
NONINTERACTIVE="${ALFRED_NONINTERACTIVE:-}"
SKIP_NPM="${ALFRED_SKIP_NPM:-}"
SKIP_BREW="${ALFRED_SKIP_BREW:-}"

usage() {
  cat <<EOF
Usage: $0 [--non-interactive] [--skip-brew] [--skip-npm]

Environment overrides:
  GH_ORG          Pre-fill the GitHub org/user for your fleet
  OPERATOR_NAME   Display name used in agent prompts
  OPERATOR_EMAIL  Operator email used in agent prompts
  ALFRED_HOME     Runtime root (default: \$HOME/.alfred)
  WORKSPACE_ROOT  Where you check out repos (default: \$HOME/code)

  ALFRED_NONINTERACTIVE=1   Same as --non-interactive
  ALFRED_SKIP_NPM=1         Skip Claude Code install via npm
  ALFRED_SKIP_BREW=1        Skip Homebrew package install
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NONINTERACTIVE=1; shift;;
    --skip-brew)       SKIP_BREW=1; shift;;
    --skip-npm)        SKIP_NPM=1; shift;;
    -h|--help)         usage; exit 0;;
    *) die "unknown arg: $1 (try --help)";;
  esac
done

ask() {
  # ask <prompt> <default> -> echoes the chosen value
  local prompt="$1" default="$2" answer
  if [[ -n "$NONINTERACTIVE" ]]; then
    printf '%s\n' "$default"
    return 0
  fi
  if [[ -n "$default" ]]; then
    printf "${C_BLUE}?${C_OFF}  %s [%s]: " "$prompt" "$default" >&2
  else
    printf "${C_BLUE}?${C_OFF}  %s: " "$prompt" >&2
  fi
  IFS= read -r answer || answer=""
  printf '%s\n' "${answer:-$default}"
}

# --------------------------------------------------------------------------
# 1. macOS check
# --------------------------------------------------------------------------
step "Checking host"
if [[ "$(uname -s)" != "Darwin" ]]; then
  warn "Alfred currently runs on macOS only (launchd-based scheduling)."
  warn "Linux support requires a systemd port; tracked but not yet shipped."
  warn "If you want to proceed anyway, set ALFRED_FORCE_LINUX=1."
  if [[ "${ALFRED_FORCE_LINUX:-}" != "1" ]]; then
    die "Refusing to install on non-macOS host. See docs/LINUX.md."
  fi
fi
ok "macOS $(sw_vers -productVersion 2>/dev/null || echo 'unknown')"

# --------------------------------------------------------------------------
# 2. Homebrew
# --------------------------------------------------------------------------
if [[ -n "$SKIP_BREW" ]]; then
  warn "Skipping Homebrew + brew packages per --skip-brew."
else
  step "Homebrew"
  if ! command -v brew >/dev/null 2>&1; then
    note "Installing Homebrew (will prompt for sudo password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Ensure brew is on PATH for the rest of this script
    if [[ -x /opt/homebrew/bin/brew ]]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  fi
  ok "brew $(brew --version | head -1)"

  # --------------------------------------------------------------------
  # 3. Brew packages
  # --------------------------------------------------------------------
  step "Installing CLI dependencies"
  declare -a packages=(
    git
    gh
    jq
    awscli
    python@3.11
    node
    uv
  )
  for pkg in "${packages[@]}"; do
    if brew list --formula | grep -q "^${pkg%@*}\$"; then
      ok "$pkg already installed"
    else
      note "brew install $pkg"
      brew install "$pkg" >/dev/null
      ok "$pkg installed"
    fi
  done
fi

# --------------------------------------------------------------------------
# 4. Claude Code
# --------------------------------------------------------------------------
if [[ -n "$SKIP_NPM" ]]; then
  warn "Skipping Claude Code install per --skip-npm."
elif command -v claude >/dev/null 2>&1; then
  ok "claude already installed: $(claude --version 2>/dev/null | head -1 || echo '?')"
else
  step "Installing Claude Code (@anthropic-ai/claude-code)"
  if command -v npm >/dev/null 2>&1; then
    note "npm install -g @anthropic-ai/claude-code"
    npm install -g @anthropic-ai/claude-code >/dev/null
    ok "Claude Code installed: $(claude --version 2>/dev/null | head -1 || echo '?')"
  else
    warn "npm not found; skipping Claude Code install. Install Node first or run --skip-npm."
  fi
fi

# --------------------------------------------------------------------------
# 5. Runtime directories
# --------------------------------------------------------------------------
step "Runtime directories"
ALFRED_HOME="${ALFRED_HOME:-$HOME/.alfred}"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$HOME/code}"

if [[ ! -d "$ALFRED_HOME" ]]; then
  mkdir -p "$ALFRED_HOME"/{bin,lib,state,worktrees}
  ok "created $ALFRED_HOME (with bin/ lib/ state/ worktrees/)"
else
  ok "$ALFRED_HOME already exists"
fi

if [[ ! -d "$WORKSPACE_ROOT" ]]; then
  mkdir -p "$WORKSPACE_ROOT"
  ok "created $WORKSPACE_ROOT"
else
  ok "$WORKSPACE_ROOT already exists"
fi

# --------------------------------------------------------------------------
# 6. Operator config
# --------------------------------------------------------------------------
step "Operator config (~/.alfredrc)"
RC_FILE="$HOME/.alfredrc"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/.alfredrc.example"

if [[ ! -f "$TEMPLATE" ]]; then
  warn "$.alfredrc.example not found in this clone; skipping operator config seeding."
elif [[ -f "$RC_FILE" ]]; then
  ok "$RC_FILE already exists; not overwriting"
else
  GH_ORG_VAL="$(ask 'GitHub org/user for your fleet' "${GH_ORG:-}")"
  OPERATOR_NAME_VAL="$(ask 'Operator display name (used in prompts)' "${OPERATOR_NAME:-}")"
  OPERATOR_EMAIL_VAL="$(ask 'Operator email (used in prompts)' "${OPERATOR_EMAIL:-}")"

  cp "$TEMPLATE" "$RC_FILE"
  # Fill in the prompted values. macOS sed needs an empty arg after -i.
  sed -i '' \
    -e "s|^GH_ORG=.*|GH_ORG=${GH_ORG_VAL}|" \
    -e "s|^OPERATOR_NAME=.*|OPERATOR_NAME=${OPERATOR_NAME_VAL}|" \
    -e "s|^OPERATOR_EMAIL=.*|OPERATOR_EMAIL=${OPERATOR_EMAIL_VAL}|" \
    -e "s|^ALFRED_HOME=.*|ALFRED_HOME=${ALFRED_HOME}|" \
    -e "s|^WORKSPACE_ROOT=.*|WORKSPACE_ROOT=${WORKSPACE_ROOT}|" \
    "$RC_FILE"
  chmod 600 "$RC_FILE"
  ok "wrote $RC_FILE (chmod 600)"
fi

# --------------------------------------------------------------------------
# 7. Shell rc append
# --------------------------------------------------------------------------
step "Shell rc"
SHELL_RC=""
case "${SHELL:-}" in
  */zsh)  SHELL_RC="$HOME/.zshrc";;
  */bash) SHELL_RC="$HOME/.bashrc";;
  *)      SHELL_RC="$HOME/.profile";;
esac

# shellcheck disable=SC2016
APPEND_BLOCK='# alfred-os — added by install.sh
[[ -f ~/.alfredrc ]] && {
  set -a
  source ~/.alfredrc
  set +a
}'

if [[ ! -f "$SHELL_RC" ]]; then
  printf '%s\n' "$APPEND_BLOCK" > "$SHELL_RC"
  ok "created $SHELL_RC with alfred-os source line"
elif grep -qF "alfred-os — added by install.sh" "$SHELL_RC"; then
  ok "$SHELL_RC already sources ~/.alfredrc"
else
  printf '\n%s\n' "$APPEND_BLOCK" >> "$SHELL_RC"
  ok "appended source-block to $SHELL_RC"
fi

# --------------------------------------------------------------------------
# 8. Auth + post-install reminder
# --------------------------------------------------------------------------
step "Auth status"
if command -v gh >/dev/null 2>&1; then
  if gh auth status >/dev/null 2>&1; then
    ok "gh authenticated"
  else
    warn "gh not authenticated yet — run: gh auth login"
  fi
fi
if command -v aws >/dev/null 2>&1; then
  if aws sts get-caller-identity >/dev/null 2>&1; then
    ok "aws authenticated"
  else
    warn "aws not authenticated yet (optional). See docs/AWS_SETUP.md"
  fi
fi
if command -v claude >/dev/null 2>&1; then
  ok "claude on PATH (run \`claude\` once interactively to authenticate)"
fi

cat <<EOF

${C_GREEN}===> Install complete.${C_OFF}

Next steps (run them in this order):

  1. Open a fresh shell so ALFRED_HOME and WORKSPACE_ROOT are loaded:
       ${C_BLUE}exec \$SHELL${C_OFF}

  2. Authenticate the CLIs that need it:
       ${C_BLUE}gh auth login${C_OFF}                     # GitHub
       ${C_BLUE}claude${C_OFF}                            # Claude Code (first run prompts for sub auth)
       ${C_BLUE}aws configure --profile <agent>-cron${C_OFF}   # only if you want AWS Secrets Manager

  3. Create a Slack incoming webhook for your fleet channel:
       See ${C_BLUE}docs/SLACK_SETUP.md${C_OFF}

  4. Deploy the framework + verify:
       ${C_BLUE}bash deploy.sh${C_OFF}
       ${C_BLUE}bash bin/doctor.sh${C_OFF}

  5. Read ${C_BLUE}INSTALL.md${C_OFF} for the full first-fleet walkthrough,
     then ${C_BLUE}BOOTSTRAP.md${C_OFF} for the deeper-dive operations guide.

If anything in this script went sideways, please open an issue at
https://github.com/luminik-io/alfred-os/issues with the output.
EOF
