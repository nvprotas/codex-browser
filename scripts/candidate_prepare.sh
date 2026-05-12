#!/usr/bin/env bash
# candidate_prepare.sh — поднимает candidate eval стек для evolve-цикла.
# Вызывается evolve_buyer_loop.py через --candidate-prepare-command.
#
# Обязательные env vars (задаёт evolve loop):
#   EVOLVE_CANDIDATE_WORKTREE  — путь к worktree кандидата на хосте
#   EVOLVE_CANDIDATE_EVAL_BASE_URL — ожидаемый URL candidate eval service
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.candidate.yml"
PROJECT="codex-candidate"

WORKTREE="${EVOLVE_CANDIDATE_WORKTREE:?EVOLVE_CANDIDATE_WORKTREE is not set}"

echo "[candidate_prepare] worktree: $WORKTREE"
echo "[candidate_prepare] compose file: $COMPOSE_FILE"

# Остановить предыдущий candidate-стек если запущен
docker compose -f "$COMPOSE_FILE" --project-name "$PROJECT" down --timeout 10 2>/dev/null || true

# Поднять новый candidate-стек с worktree кандидата
CANDIDATE_WORKSPACE="$WORKTREE" \
  docker compose \
    --env-file "$REPO_ROOT/.env" \
    -f "$COMPOSE_FILE" \
    --project-name "$PROJECT" \
    up -d

echo "[candidate_prepare] waiting for eval_service_cand to become healthy..."

# Ждём healthcheck eval_service_cand (до 120 секунд)
for i in $(seq 1 24); do
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${PROJECT}-eval_service_cand-1" 2>/dev/null || echo "missing")
  if [ "$STATUS" = "healthy" ]; then
    echo "[candidate_prepare] eval_service_cand is healthy"
    exit 0
  fi
  echo "[candidate_prepare] attempt $i/24: status=$STATUS, waiting 5s..."
  sleep 5
done

echo "[candidate_prepare] ERROR: eval_service_cand did not become healthy in time" >&2
docker logs "${PROJECT}-eval_service_cand-1" --tail 30 >&2 || true
exit 1
