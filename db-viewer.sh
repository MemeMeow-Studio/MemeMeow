#!/usr/bin/env bash
# 启动本地 Adminer 查看 MemeMeow PostgreSQL；只绑定回环地址，不向公网暴露数据库查看器。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT_DIR/scripts/runtime-identity.sh"
configure_runtime_identity
VIEWER_FALLBACK_IMAGE="${MEMEMEOW_DB_VIEWER_FALLBACK_IMAGE-docker.m.daocloud.io/library/adminer:4}"
COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml" --profile db-viewer)

compose() {
  (cd "$ROOT_DIR" && "${COMPOSE[@]}" "$@")
}

ensure_database() {
  # 复用项目现有入口，保证 PostgreSQL、迁移和 local scope 都已准备完成。
  "$ROOT_DIR/database.sh" start
}

# 确保 Adminer 镜像可用；无参数，成功返回 0。start 创建容器前调用，Docker Hub 不可达时改用备用源。
ensure_viewer_image() {
  if compose pull --policy missing db-viewer; then
    return 0
  fi

  if [[ -z "$VIEWER_FALLBACK_IMAGE" ]]; then
    printf 'Adminer 镜像拉取失败，且备用镜像已禁用。请设置 MEMEMEOW_DB_VIEWER_IMAGE 后重试。\n' >&2
    return 1
  fi

  printf '默认 Adminer 镜像拉取失败，尝试备用镜像: %s\n' "$VIEWER_FALLBACK_IMAGE" >&2
  if ! docker image inspect "$VIEWER_FALLBACK_IMAGE" >/dev/null 2>&1; then
    docker pull "$VIEWER_FALLBACK_IMAGE"
  fi
  docker tag "$VIEWER_FALLBACK_IMAGE" adminer:4
  export MEMEMEOW_DB_VIEWER_IMAGE=adminer:4
}

# 输出当前 Adminer 地址；输入动作名称，容器存在时从 Compose 读取实际端口，供 start 和 restart 回显。
print_viewer_url() {
  local action="$1" address port
  address="$(compose port db-viewer 8080 2>/dev/null || true)"
  port="${address##*:}"
  printf '数据库查看器已%s: http://127.0.0.1:%s\n' "$action" "${port:-8080}"
}

print_usage() {
  printf '用法: %s start|stop|restart|status|logs\n' "$0"
}

case "${1:-start}" in
  start)
    ensure_database
    ensure_viewer_image
    compose up -d db-viewer
    print_viewer_url 启动
    printf 'Adminer 登录时服务器填写 postgres（容器内默认端口 5432）。\n'
    ;;
  stop)
    compose stop db-viewer
    ;;
  restart)
    compose restart db-viewer
    print_viewer_url 重启
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
