#!/usr/bin/env bash
# candidate_teardown.sh — останавливает candidate eval стек после цикла.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$REPO_ROOT/docker-compose.candidate.yml"
PROJECT="codex-candidate"

echo "[candidate_teardown] stopping $PROJECT..."
docker compose -f "$COMPOSE_FILE" --project-name "$PROJECT" down --timeout 15 2>/dev/null || true
echo "[candidate_teardown] done"
