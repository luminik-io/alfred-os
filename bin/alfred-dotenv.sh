#!/usr/bin/env bash
# Shared dotenv helpers for small Alfred shell wrappers.
#
# Source this file from trusted Alfred scripts; it parses dotenv assignments
# from $ALFRED_HOME/.env without executing the dotenv file as shell code.

alfred_strip_inline_comment() {
  local value="$1" ch quote="" escaped=0 i previous=""
  for ((i = 0; i < ${#value}; i++)); do
    ch="${value:i:1}"
    if [ "$escaped" -eq 1 ]; then
      escaped=0
      previous="$ch"
      continue
    fi
    if [ "$ch" = "\\" ] && [ "$quote" != "'" ]; then
      escaped=1
      previous="$ch"
      continue
    fi
    if [ -n "$quote" ]; then
      if [ "$ch" = "$quote" ]; then
        quote=""
      fi
      previous="$ch"
      continue
    fi
    if [ "$ch" = "'" ] || [ "$ch" = '"' ]; then
      quote="$ch"
      previous="$ch"
      continue
    fi
    if [ "$ch" = "#" ] && [ -n "$previous" ] && [[ "$previous" =~ [[:space:]] ]]; then
      printf '%s' "${value:0:i}"
      return
    fi
    previous="$ch"
  done
  printf '%s' "$value"
}

alfred_trim_env_value() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

alfred_env_value_quote_style() {
  case "$1" in
    \'*\') printf '%s' single ;;
    \"*\") printf '%s' double ;;
    *) printf '%s' none ;;
  esac
}

alfred_decode_env_value() {
  local value="$1" sq="'" dq='"' splice
  splice="${sq}${dq}${sq}${dq}${sq}"
  case "$value" in
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      value="${value//$splice/$sq}"
      ;;
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
  esac
  printf '%s' "$value"
}

alfred_expand_user_path() {
  local path="$1" expanded=""
  case "$path" in
    "~") printf '%s' "$HOME" ;;
    "~"/*) printf '%s/%s' "$HOME" "${path#\~/}" ;;
    "~"*)
      expanded="$(python3 - "$path" <<'PY' 2>/dev/null || true
import os
import sys

print(os.path.expanduser(sys.argv[1]))
PY
)"
      if [ -n "$expanded" ]; then
        printf '%s' "$expanded"
      else
        printf '%s' "$path"
      fi
      ;;
    *) printf '%s' "$path" ;;
  esac
}

alfred_load_env_file() {
  local file="$1" no_clobber="${2:-}" line key value quote_style
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|\#*) continue ;;
    esac
    case "$line" in
      export\ *) line="${line#export }" ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      ''|[0-9]*|*[!A-Za-z0-9_]*)
        continue
        ;;
    esac
    value="$(alfred_trim_env_value "$(alfred_strip_inline_comment "$value")")"
    quote_style="$(alfred_env_value_quote_style "$value")"
    value="$(alfred_decode_env_value "$value")"
    if [ "$quote_style" != "single" ]; then
      value="${value//\$\{HOME\}/$HOME}"
      value="${value//\$HOME/$HOME}"
    fi
    if [ -n "$no_clobber" ] && [ -n "${!key+x}" ]; then
      continue
    fi
    export "$key=$value"
  done < "$file"
}
