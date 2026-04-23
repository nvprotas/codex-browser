#!/usr/bin/env bash
set -euo pipefail

setup_codex_oauth() {
  local host_path="${CODEX_AUTH_JSON_PATH:-}"
  local mounted_auth="/run/codex/host-auth"
  local codex_home="/root/.codex"
  local target_auth="$codex_home/auth.json"

  mkdir -p "$codex_home"

  if [[ -z "$host_path" ]]; then
    echo "buyer: CODEX_AUTH_JSON_PATH не задан, OAuth auth.json не подключен."
    return 0
  fi

  if [[ ! -f "$mounted_auth" ]]; then
    echo "buyer: CODEX_AUTH_JSON_PATH задан, но файл не смонтирован в контейнер: $mounted_auth" >&2
    return 1
  fi

  if [[ ! -s "$mounted_auth" ]]; then
    echo "buyer: смонтированный auth.json пустой: $mounted_auth" >&2
    return 1
  fi

  cp "$mounted_auth" "$target_auth"
  chmod 600 "$target_auth"
  echo "buyer: OAuth auth.json загружен из host-path ($host_path)."
}

setup_codex_oauth

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
