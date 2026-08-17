#!/usr/bin/env bash
# 共享 Agent 容器的构建、启动、探针和停止入口。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT_DIR/scripts/runtime-identity.sh"
configure_runtime_identity
COMPOSE_FILE="${MEMEMEOW_COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"
SERVICE="mememeow-agent-runtime"

mkdir -p "$ROOT_DIR/data/opencode" "$ROOT_DIR/data/images"

compose() {
  # 用户可能尚未刷新 docker 组，统一通过 sg 运行 Docker 命令。
  if docker info >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
  else
    # sg 只接受一个命令字符串；用 bash 的 %q 为每个参数安全转义，避免路径
    # 或任务参数改变命令语义。
    local quoted argument
    printf -v quoted 'docker compose -f %q' "$COMPOSE_FILE"
    for argument in "$@"; do
      printf -v argument '%q' "$argument"
      quoted+=" $argument"
    done
    sg docker -c "$quoted"
  fi
}

case "${1:-check}" in
  build)
    compose build "$SERVICE"
    ;;
  start)
    compose up -d --build "$SERVICE"
    compose ps "$SERVICE"
    ;;
  check)
    compose ps "$SERVICE"
    compose exec -T "$SERVICE" sh -lc 'set -eu; id; opencode --version; node --version; python3 --version; file --version | head -1; convert --version | head -1; tesseract --version | head -1; jq --version; curl --version | head -1; test "$(id -u)" != 0; test -r /images && test ! -w /images; test -r /skills/research-meme-context && test ! -w /skills/research-meme-context; test -r /opt/mememeow/node_modules && test ! -w /opt/mememeow/node_modules; test -r /runtime && test -w /runtime; test ! -S /var/run/docker.sock; test ! -e /.env; python3 -m executor.probe; printf "agent runtime probe: ok\n"'
    ;;
  stop)
    compose stop "$SERVICE"
    ;;
  restart)
    compose stop "$SERVICE"
    compose up -d --build "$SERVICE"
    ;;
  logs)
    compose logs --tail=200 "$SERVICE"
    ;;
  *)
    echo "用法: $0 {build|start|check|stop|restart|logs}" >&2
    exit 2
    ;;
esac
