#!/usr/bin/env bash
# alfred-os, fresh-machine bootstrap.
#
# Supported hosts:
#   - macOS, full fleet, launchd-scheduled.
#   - Debian/Ubuntu Linux, full fleet, systemd --user-scheduled. apt for the
#     CLI tools; uv installs from the official script.
#
# What this script does (idempotent, safe to re-run):
#   1. Detects the host OS (macOS or Debian/Ubuntu Linux) and picks the
#      package-manager lane. Other hosts are refused with a clear message.
#   2. macOS: installs Homebrew if missing. Linux: uses apt-get.
#   3. Installs the CLI tools every alfred-os fleet needs: python@3.11, git,
#      gh, jq, uv (fast Python runner used by the test suite). macOS also
#      installs awscli + node via brew; on Linux uv is fetched from its
#      official installer and AWS CLI v2 is left to the operator.
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
#   - Run deploy.sh. Installing scheduled jobs (launchd plists on macOS,
#     systemd --user timers on Linux) side-effects; the operator should pull
#     the trigger after reading what's about to load.
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
SKIP_PYTHON_VENV="${ALFRED_SKIP_PYTHON_VENV:-}"

usage() {
  cat <<EOF
Usage: $0 [--non-interactive] [--skip-brew] [--skip-npm] [--skip-python-venv]

Environment overrides:
  GH_ORG          Pre-fill the GitHub org/user for your fleet
  OPERATOR_NAME   Display name used in agent prompts
  OPERATOR_EMAIL  Operator email used in agent prompts
  ALFRED_HOME     Runtime root (default: \$HOME/.alfred)
  WORKSPACE_ROOT  Where you check out repos (default: \$HOME/code)

  ALFRED_NONINTERACTIVE=1   Same as --non-interactive
  ALFRED_SKIP_NPM=1         Skip Claude Code install via npm
  ALFRED_SKIP_BREW=1        Skip Homebrew package install
  ALFRED_SKIP_PYTHON_VENV=1 Skip \$ALFRED_HOME/venv provisioning
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) NONINTERACTIVE=1; shift;;
    --skip-brew)       SKIP_BREW=1; shift;;
    --skip-npm)        SKIP_NPM=1; shift;;
    --skip-python-venv) SKIP_PYTHON_VENV=1; shift;;
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
# 1. Host detection
# --------------------------------------------------------------------------
# ALFRED_OS picks the package-manager lane: "darwin" (Homebrew, launchd) or
# "linux" (Debian/Ubuntu apt, systemd --user). Anything else is refused.
step "Checking host"
case "$(uname -s)" in
  Darwin)
    ALFRED_OS="darwin"
    ok "macOS $(sw_vers -productVersion 2>/dev/null || echo 'unknown'), launchd scheduling"
    ;;
  Linux)
    ALFRED_OS="linux"
    if [[ ! -r /etc/os-release ]]; then
      die "/etc/os-release not readable, cannot identify this Linux distro. Debian/Ubuntu required."
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    case " ${ID:-} ${ID_LIKE:-} " in
      *" debian "*|*" ubuntu "*) ;;
      *)
        die "Only Debian/Ubuntu Linux is supported (got ID=${ID:-?} ID_LIKE=${ID_LIKE:-}). See docs/LINUX.md."
        ;;
    esac
    if ! command -v apt-get >/dev/null 2>&1; then
      die "apt-get not found, Debian/Ubuntu apt is required. See docs/LINUX.md."
    fi
    ok "${PRETTY_NAME:-Debian/Ubuntu Linux}, systemd --user scheduling"
    ;;
  *)
    die "Unsupported host: $(uname -s). alfred-os runs on macOS or Debian/Ubuntu Linux. See docs/LINUX.md."
    ;;
esac

# --------------------------------------------------------------------------
# 2. Package manager
# --------------------------------------------------------------------------
# sudo wrapper for the Linux lane: empty when already root, "sudo" otherwise.
SUDO=""
if [[ "$ALFRED_OS" == "linux" && "$(id -u)" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    die "running as non-root and 'sudo' is not installed; install sudo or run as root."
  fi
  SUDO="sudo"
fi

install_darwin_packages() {
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

  step "Installing CLI dependencies"
  if ! brew tap | grep -qx "redis-stack/redis-stack"; then
    note "brew tap redis-stack/redis-stack"
    brew tap redis-stack/redis-stack >/dev/null
  fi
  local pkg
  declare -a packages=(
    git
    gh
    jq
    awscli
    python@3.11
    node
    uv
    redis-stack-server
    ollama
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
}

install_linux_packages() {
  step "Installing CLI dependencies (apt)"
  # Distro packages. node is pulled in for npm (Claude Code install). uv has
  # no apt package, so it installs from the official script below. AWS CLI v2
  # is intentionally not auto-installed: apt's awscli is v1.x and scheduled
  # fleet jobs that touch AWS want v2.
  local apt_pkgs="ca-certificates curl gnupg git jq python3-venv python3-pip nodejs npm redis-tools"
  note "apt-get update"
  ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get update -qq
  note "apt-get install -y ${apt_pkgs}"
  # shellcheck disable=SC2086
  ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get install -y ${apt_pkgs} >/dev/null
  ok "apt packages installed"

  # python3.11: Hermes-shaped agents pin 3.11, but recent Ubuntu/Debian ship a
  # newer default. Prefer a uv-managed 3.11 (set up after uv installs below);
  # if the distro happens to ship python3.11, that is fine too.
  if command -v python3.11 >/dev/null 2>&1; then
    ok "python3.11 present: $(python3.11 --version 2>/dev/null || echo '?')"
  else
    note "python3.11 not in apt; will be provisioned via 'uv python install 3.11'"
  fi

  # gh: prefer the distro package; fall back to GitHub's official apt repo.
  if command -v gh >/dev/null 2>&1; then
    ok "gh already installed"
  elif ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get install -y gh >/dev/null 2>&1; then
    ok "gh installed from distro repo"
  else
    note "gh not in distro repo, adding GitHub's official apt repo"
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | ${SUDO} dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
    ${SUDO} chmod a+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | ${SUDO} tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get update -qq
    ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get install -y gh >/dev/null
    ok "gh installed from GitHub apt repo"
  fi

  # uv (Astral): no apt package, install from the official script into
  # ~/.local/bin. Idempotent: the installer no-ops if uv is current.
  if command -v uv >/dev/null 2>&1; then
    ok "uv already installed"
  else
    note "installing uv from https://astral.sh/uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  if [[ -d "$HOME/.local/bin" ]]; then
    case ":$PATH:" in
      *":$HOME/.local/bin:"*) ;;
      *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
    esac
  fi

  # Redis Agent Memory Server needs Redis Stack because vector search depends
  # on RediSearch. Do not install the distro redis-server here; it can occupy
  # 127.0.0.1:6379 before Redis Stack starts.
  if ! command -v redis-stack-server >/dev/null 2>&1; then
    note "installing Redis Stack from packages.redis.io"
    if curl -fsSL https://packages.redis.io/gpg \
      | ${SUDO} gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg 2>/dev/null; then
      local redis_codename="bookworm"
      if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        redis_codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
      fi
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb ${redis_codename} main" \
        | ${SUDO} tee /etc/apt/sources.list.d/redis.list >/dev/null
      ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
      if ${SUDO} DEBIAN_FRONTEND=noninteractive apt-get install -y redis-stack-server >/dev/null 2>&1; then
        ok "redis-stack-server installed"
      else
        warn "Redis Stack install failed; Alfred memory needs redis-stack-server for semantic search."
      fi
    else
      warn "Could not add the Redis apt repo; install redis-stack-server manually for Alfred memory."
    fi
  fi

  if ! command -v ollama >/dev/null 2>&1; then
    if [[ "${ALFRED_INSTALL_OLLAMA:-}" == "1" ]]; then
      note "installing Ollama for local memory embeddings"
      local ollama_install
      ollama_install="$(mktemp)"
      if curl -fsSL https://ollama.com/install.sh -o "$ollama_install" && sh "$ollama_install"; then
        ok "ollama installed"
        rm -f "$ollama_install"
      else
        rm -f "$ollama_install"
        warn "Ollama install failed; install Ollama and run 'ollama pull mxbai-embed-large' and 'ollama pull llama3.2'."
      fi
    else
      warn "Ollama is not installed. Install it manually, or re-run with ALFRED_INSTALL_OLLAMA=1 to use Ollama's official install script."
    fi
  fi

  # python3.11 via uv when the distro did not ship it. Keeps the runtime off
  # the distro-release treadmill.
  if ! command -v python3.11 >/dev/null 2>&1; then
    if command -v uv >/dev/null 2>&1; then
      if uv python find 3.11 >/dev/null 2>&1; then
        ok "uv-managed python 3.11 already present"
      else
        note "uv python install 3.11"
        uv python install 3.11
        ok "python 3.11 installed via uv"
      fi
    else
      warn "uv not on PATH after install; python3.11 not provisioned. Add ~/.local/bin to PATH and re-run."
    fi
  fi

  # AWS CLI v2 is operator-installed when scheduled fleet jobs need it.
  if ! command -v aws >/dev/null 2>&1; then
    warn "AWS CLI not on PATH, install AWS CLI v2 manually if scheduled jobs touch AWS:"
    warn "  https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  fi
}

if [[ -n "$SKIP_BREW" ]]; then
  warn "Skipping package install per --skip-brew."
else
  case "$ALFRED_OS" in
    darwin) install_darwin_packages ;;
    linux)  install_linux_packages ;;
  esac
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
# 5b. Python dependency venv
#
# v0.4.0 promoted slack-sdk + boto3 from optional extras into the base
# `dependencies` list of pyproject.toml. Without a controlled install step
# any agent that resolves a Slack token or hits AWS Secrets Manager
# crashes at first-use with ModuleNotFoundError. uv pip install --system
# either needs sudo (system Python) or refuses (uv-managed Python is
# "externally managed"). Operator-owned venv under $ALFRED_HOME is the
# clean middle ground: agent-launch picks ${ALFRED_HOME}/venv/bin/python
# when present (see bin/agent-launch).
# --------------------------------------------------------------------------
step "Python dependency venv (\$ALFRED_HOME/venv)"
ALFRED_VENV="$ALFRED_HOME/venv"
if [[ -n "$SKIP_PYTHON_VENV" ]]; then
  warn "Skipping venv setup per --skip-python-venv. slack_sdk and boto3 must already be importable from the agent shebang interpreter."
elif ! command -v uv >/dev/null 2>&1; then
  warn "uv not on PATH; cannot bootstrap \$ALFRED_HOME/venv. Add ~/.local/bin to PATH and re-run, or pass --skip-python-venv if deps are already importable."
else
  if [[ ! -x "$ALFRED_VENV/bin/python" ]]; then
    note "uv venv --python 3.11 $ALFRED_VENV"
    uv venv --python 3.11 "$ALFRED_VENV" >/dev/null
    ok "created venv at $ALFRED_VENV"
  else
    ok "venv at $ALFRED_VENV already exists"
  fi
  # Install the same base deps pyproject.toml declares. Pinning the floor
  # versions here mirrors `pyproject.toml`'s base dependencies list; if
  # they drift, the doctor check below catches it (assertion that both
  # imports succeed against $ALFRED_HOME/venv/bin/python).
  note "uv pip install --python $ALFRED_VENV/bin/python slack-sdk boto3"
  uv pip install --python "$ALFRED_VENV/bin/python" "slack-sdk>=3.27" "boto3>=1.34" >/dev/null
  if "$ALFRED_VENV/bin/python" -c "import slack_sdk, boto3" >/dev/null 2>&1; then
    ok "slack-sdk + boto3 importable from \$ALFRED_HOME/venv"
  else
    warn "venv install reported success but imports fail; check $ALFRED_VENV manually"
  fi
fi

# --------------------------------------------------------------------------
# 5c. Redis Agent Memory Server
# --------------------------------------------------------------------------
step "Redis Agent Memory Server"
AMS_SPEC="${ALFRED_AMS_UVX_SPEC:-git+https://github.com/redis-developer/agent-memory-server.git}"
if command -v agent-memory >/dev/null 2>&1; then
  ok "agent-memory already installed"
elif command -v uv >/dev/null 2>&1; then
  note "uv tool install --python 3.12 $AMS_SPEC"
  if uv tool install --python 3.12 "$AMS_SPEC" >/dev/null 2>&1; then
    ok "agent-memory installed"
  else
    warn "Could not install agent-memory now; ams-launch.sh will fall back to uvx at runtime."
  fi
else
  warn "uv not on PATH; install agent-memory manually for Redis-backed memory."
fi

if command -v ollama >/dev/null 2>&1; then
  for ollama_model in mxbai-embed-large llama3.2; do
    if ollama list 2>/dev/null | grep -q "^${ollama_model}"; then
      ok "$ollama_model already pulled"
    elif ollama pull "$ollama_model" >/dev/null 2>&1; then
      ok "$ollama_model pulled"
    else
      warn "Could not pull $ollama_model; Redis memory needs this local model."
    fi
  done
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
  # Fill in the prompted values. BSD sed (macOS) needs an empty extension arg
  # after -i; GNU sed (Linux) does not accept one. Branch on $ALFRED_OS.
  if [[ "$ALFRED_OS" == "darwin" ]]; then
    sed -i '' \
      -e "s|^GH_ORG=.*|GH_ORG=${GH_ORG_VAL}|" \
      -e "s|^OPERATOR_NAME=.*|OPERATOR_NAME=${OPERATOR_NAME_VAL}|" \
      -e "s|^OPERATOR_EMAIL=.*|OPERATOR_EMAIL=${OPERATOR_EMAIL_VAL}|" \
      -e "s|^ALFRED_HOME=.*|ALFRED_HOME=${ALFRED_HOME}|" \
      -e "s|^WORKSPACE_ROOT=.*|WORKSPACE_ROOT=${WORKSPACE_ROOT}|" \
      "$RC_FILE"
  else
    sed -i \
      -e "s|^GH_ORG=.*|GH_ORG=${GH_ORG_VAL}|" \
      -e "s|^OPERATOR_NAME=.*|OPERATOR_NAME=${OPERATOR_NAME_VAL}|" \
      -e "s|^OPERATOR_EMAIL=.*|OPERATOR_EMAIL=${OPERATOR_EMAIL_VAL}|" \
      -e "s|^ALFRED_HOME=.*|ALFRED_HOME=${ALFRED_HOME}|" \
      -e "s|^WORKSPACE_ROOT=.*|WORKSPACE_ROOT=${WORKSPACE_ROOT}|" \
      "$RC_FILE"
  fi
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
APPEND_BLOCK='# alfred-os, added by install.sh
[[ -f ~/.alfredrc ]] && {
  set -a
  source ~/.alfredrc
  set +a
}'

if [[ ! -f "$SHELL_RC" ]]; then
  printf '%s\n' "$APPEND_BLOCK" > "$SHELL_RC"
  ok "created $SHELL_RC with alfred-os source line"
elif grep -qE 'alfred-os.{1,4}added by install\.sh' "$SHELL_RC"; then
  # Pattern, not a literal: older releases used an em-dash between
  # "alfred-os" and "added", current uses a comma. Recognising both keeps
  # this check idempotent across upgrades, so we never append a second
  # source-block to a shell rc that already has one.
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
    warn "gh not authenticated yet, run: gh auth login"
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

# Homebrew formula installs this script under:
#   .../Cellar/alfred-os/<version>/libexec/install.sh
# Key off this script's own location, not ambient PATH. A source checkout may
# have older Homebrew wrappers on PATH, and in that case the source checkout
# should still print source-checkout next steps.
case "$SCRIPT_DIR" in
  */Cellar/alfred-os/*/libexec)
    HOMEBREW_FORMULA_INSTALL=1
    ;;
  *)
    HOMEBREW_FORMULA_INSTALL=0
    ;;
esac

if [[ "$HOMEBREW_FORMULA_INSTALL" == "1" ]]; then
  DEPLOY_CMD="alfred-deploy"
  DOCTOR_CMD="alfred-doctor"
  INIT_CMD="alfred-init"
  SLACK_DOC="https://alfred.luminik.io/guides/slack/"
  INSTALL_DOC="https://alfred.luminik.io/getting-started/install/"
  BOOTSTRAP_DOC="https://github.com/luminik-io/alfred-os/blob/main/BOOTSTRAP.md"
  LINUX_DOC="https://alfred.luminik.io/guides/linux/"
else
  DEPLOY_CMD="bash deploy.sh"
  DOCTOR_CMD="bash bin/doctor.sh"
  INIT_CMD="./bin/alfred-init.py"
  SLACK_DOC="docs/SLACK_SETUP.md"
  INSTALL_DOC="INSTALL.md"
  BOOTSTRAP_DOC="BOOTSTRAP.md"
  LINUX_DOC="docs/LINUX.md"
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

     Scheduled (launchd / systemd) firings cannot read your host
     credential store. After 'claude' is set up, mint a long-lived
     OAuth token so your fleet authenticates without the Keychain:
       ${C_BLUE}claude setup-token${C_OFF}                # one-time, approve in browser
     'alfred-init' (step 5) wraps this for you with file write + chmod.

  3. Create a Slack incoming webhook for your fleet channel:
       See ${C_BLUE}${SLACK_DOC}${C_OFF}

  4. Deploy the framework + verify (deploy.sh self-detects the host
     scheduler: launchd plists on macOS, systemd --user timers on Linux):
       ${C_BLUE}${DEPLOY_CMD}${C_OFF}
       ${C_BLUE}${DOCTOR_CMD}${C_OFF}

  5. Configure your first fleet:
       ${C_BLUE}${INIT_CMD}${C_OFF}

  6. Read ${C_BLUE}${INSTALL_DOC}${C_OFF} for the full first-fleet walkthrough,
     then ${C_BLUE}${BOOTSTRAP_DOC}${C_OFF} for the deeper-dive operations guide.
     Linux specifics live in ${C_BLUE}${LINUX_DOC}${C_OFF}.

If anything in this script went sideways, please open an issue at
https://github.com/luminik-io/alfred-os/issues with the output.
EOF
