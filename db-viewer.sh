#!/usr/bin/env bash
# 启动本地 Adminer 查看 MemeMeow PostgreSQL；只绑定回环地址，不向公网暴露数据库查看器。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VIEWER_PORT="${MEMEMEOW_DB_VIEWER_PORT:-8080}"
COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml" --profile db-viewer)

compose() {
  (cd "$ROOT_DIR" && "${COMPOSE[@]}" "$@")
}

ensure_database() {
  # 复用项目现有入口，保证 PostgreSQL、迁移和 local scope 都已准备完成。
  "$ROOT_DIR/database.sh" start
}

print_usage() {
  printf '用法: %s start|stop|restart|status|logs\n' "$0"
}

case "${1:-start}" in
  start)
    ensure_database
    compose up -d db-viewer
    printf '数据库查看器已启动: http://127.0.0.1:%s\n' "$VIEWER_PORT"
    printf 'Adminer 登录时服务器填写 postgres（容器内默认端口 5432）。\n'
    ;;
  stop)
    compose stop db-viewer
    ;;
  restart)
    compose restart db-viewer
    printf '数据库查看器已重启: http://127.0.0.1:%s\n' "$VIEWER_PORT"
    ;;
  status)
    compose ps db-viewer
    ;;
  logs)
    compose logs --tail="${2:-200}" db-viewer
    ;;
  *)
    print_usage >&2
    exit 2
    ;;
esac
