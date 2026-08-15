#!/usr/bin/env bash
# PostgreSQL 16 + pgvector 生命周期入口；不管理宿主机 API/Vue，也不删除具名 volume。

set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml")

compose() { (cd "$ROOT_DIR" && "${COMPOSE[@]}" "$@"); }

wait_healthy() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose exec -T postgres pg_isready -U "${POSTGRES_USER:-mememeow}" -d "${POSTGRES_DB:-mememeow}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "PostgreSQL 健康检查超时，请执行 ./database.sh logs" >&2
  return 1
}

run_migrations() {
  (cd "$ROOT_DIR" && MEMEMEOW_DATABASE_URL="${MEMEMEOW_DATABASE_URL_CONTAINER:-postgresql+psycopg://mememeow:mememeow@postgres:5432/mememeow}" docker compose --profile tools run --rm --build db-init alembic upgrade head)
  (cd "$ROOT_DIR" && MEMEMEOW_DATABASE_URL="${MEMEMEOW_DATABASE_URL_CONTAINER:-postgresql+psycopg://mememeow:mememeow@postgres:5432/mememeow}" docker compose --profile tools run --rm --build db-init python -m scripts.init_database)
}

case "${1:-start}" in
  start)
    compose up -d postgres
    wait_healthy
    run_migrations
    echo "PostgreSQL 已就绪，schema 与 local scope 已初始化。"
    ;;
  stop) compose stop postgres ;;
  restart) compose restart postgres; wait_healthy ;;
  status) compose ps postgres ;;
  logs) compose logs --tail="${2:-200}" postgres ;;
  migrate) wait_healthy; run_migrations ;;
  *) echo "用法: $0 start|stop|restart|status|logs|migrate" >&2; exit 2 ;;
esac
