#!/usr/bin/env bash
# Launch the local Redis Agent Memory Server used by Alfred memory.
#
# The server binds loopback by default. Config comes from memory.ams_server and
# can be overridden with ALFRED_AMS_* values in $ALFRED_HOME/.env.

set -uo pipefail

load_env_file() {
  local file="$1" line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ''|[0-9]*|*[!A-Za-z0-9_]*) continue ;;
    esac
    case "$value" in
      \"*\") value="${value#\"}"; value="${value%\"}" ;;
      \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    value="${value//\$\{HOME\}/$HOME}"
    value="${value//\$HOME/$HOME}"
    export "$key=$value"
  done < "$file"
}

ALFRED_HOME="${ALFRED_HOME:-${HERMES_HOME:-$HOME/.alfred}}"
load_env_file "$HOME/.alfredrc"
load_env_file "$ALFRED_HOME/.env"
ALFRED_LIB="$ALFRED_HOME/lib"

AMS_ENV_EXPORTS="$(
  PYTHONPATH="$ALFRED_LIB${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import shlex
import sys

try:
    from memory.ams_server import ams_server_env
except Exception as exc:
    sys.stderr.write(f"[ams-launch] could not resolve config: {exc}\n")
    raise SystemExit(3)

for key, value in ams_server_env().items():
    print(f"export {key}={shlex.quote(value)}")
PY
)"
if [ -z "$AMS_ENV_EXPORTS" ]; then
  echo "[ams-launch] empty server env; refusing to start" >&2
  exit 3
fi
eval "$AMS_ENV_EXPORTS"

AMS_HOST="$(
  PYTHONPATH="$ALFRED_LIB${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from memory.ams_server import AmsServerConfig
print(AmsServerConfig.from_env().host)
PY
)"
AMS_PORT="$(
  PYTHONPATH="$ALFRED_LIB${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
from memory.ams_server import AmsServerConfig
print(AmsServerConfig.from_env().port)
PY
)"
AMS_HOST="${AMS_HOST:-127.0.0.1}"
AMS_PORT="${AMS_PORT:-8088}"

echo "[ams-launch] starting Agent Memory Server on ${AMS_HOST}:${AMS_PORT}" >&2
echo "[ams-launch] embedding_model=${EMBEDDING_MODEL:-unset} auth_mode=${AUTH_MODE:-unset}" >&2

redis_answers_ping() {
  redis-cli -u "$1" ping >/dev/null 2>&1
}

redis_has_redisearch() {
  local url="$1" modules ftlist
  modules="$(redis-cli -u "$url" MODULE LIST 2>/dev/null)"
  if printf '%s' "$modules" | grep -qiE 'search|ft'; then
    return 0
  fi
  ftlist="$(redis-cli -u "$url" FT._LIST 2>&1)"
  case "$ftlist" in
    *ERR*|*"unknown command"*|*WRONGTYPE*) return 1 ;;
    *) return 0 ;;
  esac
}

wait_for_redis_ping() {
  local url="$1" i=0
  while [ "$i" -lt 15 ]; do
    if redis_answers_ping "$url"; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

ollama_answers() {
  curl -fsS "${1%/}/api/tags" >/dev/null 2>&1
}

wait_for_ollama() {
  local url="$1" i=0
  while [ "$i" -lt 30 ]; do
    if ollama_answers "$url"; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

ensure_redis_with_redisearch() {
  local url="$1"
  command -v redis-cli >/dev/null 2>&1 || {
    echo "[ams-launch] redis-cli not on PATH; cannot verify Redis Stack on $url" >&2
    return 0
  }
  if redis_answers_ping "$url"; then
    if redis_has_redisearch "$url"; then
      echo "[ams-launch] Redis Stack is serving on $url" >&2
      return 0
    fi
    echo "[ams-launch] plain Redis is running on $url, but Alfred memory needs Redis Stack with RediSearch" >&2
    echo "[ams-launch] stop plain Redis or set ALFRED_AMS_REDIS_URL to a Redis Stack instance" >&2
    return 1
  fi
  if command -v redis-stack-server >/dev/null 2>&1; then
    echo "[ams-launch] starting redis-stack-server" >&2
    nohup redis-stack-server --port 6379 --bind 127.0.0.1 >/dev/null 2>&1 &
    if ! wait_for_redis_ping "$url"; then
      echo "[ams-launch] redis-stack-server did not answer ping within 15s" >&2
      return 1
    fi
    if ! redis_has_redisearch "$url"; then
      echo "[ams-launch] redis-stack-server started but RediSearch is unavailable" >&2
      return 1
    fi
    return 0
  fi
  echo "[ams-launch] no Redis Stack on $url; install redis-stack-server" >&2
  return 1
}

if ! ensure_redis_with_redisearch "${REDIS_URL:-redis://127.0.0.1:6379/0}"; then
  exit 4
fi

run_agent_memory_cli() {
  if command -v agent-memory >/dev/null 2>&1; then
    agent-memory "$@"
    return $?
  fi
  if command -v uvx >/dev/null 2>&1; then
    uvx --python 3.12 --from "${ALFRED_AMS_UVX_SPEC:-git+https://github.com/redis-developer/agent-memory-server.git}" agent-memory "$@"
    return $?
  fi
  return 127
}

if [ "${AUTH_MODE:-disabled}" = "token" ]; then
  if [ -z "${ALFRED_AMS_TOKEN:-}" ]; then
    echo "[ams-launch] ALFRED_AMS_AUTH_MODE=token requires ALFRED_AMS_TOKEN" >&2
    exit 6
  fi
  if ! run_agent_memory_cli token add --description "Alfred local AMS" --token "$ALFRED_AMS_TOKEN" --format json >/dev/null; then
    echo "[ams-launch] could not register ALFRED_AMS_TOKEN with agent-memory token add" >&2
    exit 6
  fi
fi

if command -v ollama >/dev/null 2>&1; then
  OLLAMA_READY_URL="${OLLAMA_API_BASE:-http://127.0.0.1:11434}"
  if ! ollama_answers "$OLLAMA_READY_URL"; then
    echo "[ams-launch] starting ollama serve" >&2
    nohup ollama serve >/dev/null 2>&1 &
  fi
  if ! wait_for_ollama "$OLLAMA_READY_URL"; then
    echo "[ams-launch] ollama did not answer ${OLLAMA_READY_URL%/}/api/tags within 30s" >&2
    exit 5
  fi
else
  echo "[ams-launch] ollama not on PATH; embeddings will fail until it is installed" >&2
fi

AMS_API_ARGS=(api --host "$AMS_HOST" --port "$AMS_PORT" --task-backend=asyncio)

agent_memory_runs() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 30 "$@" --help >/dev/null 2>&1
  else
    "$@" --help >/dev/null 2>&1
  fi
}

if command -v agent-memory >/dev/null 2>&1; then
  if agent_memory_runs agent-memory; then
    echo "[ams-launch] exec agent-memory ${AMS_API_ARGS[*]}" >&2
    exec agent-memory "${AMS_API_ARGS[@]}"
  fi
  echo "[ams-launch] installed agent-memory failed --help; falling back to uvx" >&2
fi

if command -v uvx >/dev/null 2>&1; then
  AMS_UVX_SPEC="${ALFRED_AMS_UVX_SPEC:-git+https://github.com/redis-developer/agent-memory-server.git}"
  echo "[ams-launch] exec uvx --from $AMS_UVX_SPEC agent-memory ${AMS_API_ARGS[*]}" >&2
  exec uvx --python 3.12 --from "$AMS_UVX_SPEC" agent-memory "${AMS_API_ARGS[@]}"
fi

echo "[ams-launch] neither agent-memory nor uvx is on PATH" >&2
echo "[ams-launch] install with: uv tool install --python 3.12 'git+https://github.com/redis-developer/agent-memory-server.git'" >&2
exit 127
