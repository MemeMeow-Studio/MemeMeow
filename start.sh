#!/usr/bin/env bash
# MemeMeow 的 Compose 全栈生命周期入口；不再启动宿主机进程或 tmux 窗口。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${MEMEMEOW_COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"
if [[ "$COMPOSE_FILE" != /* ]]; then
  COMPOSE_FILE="$ROOT_DIR/$COMPOSE_FILE"
fi

SESSION_NAME="mememeow"
BACKEND_PORT="8275"
FRONTEND_PORT="5275"
VITE_HOST="${MEMEMEOW_VITE_HOST:-0.0.0.0}"
VITE_PORT="${MEMEMEOW_VITE_PORT:-$FRONTEND_PORT}"
LEGACY_BACKEND_PORT="8000"
LEGACY_FRONTEND_PORT="5173"
LOG_TAIL="${MEMEMEOW_LOG_TAIL:-200}"
START_TIMEOUT_SECONDS="${MEMEMEOW_START_TIMEOUT_SECONDS:-180}"
if [[ ! "$LOG_TAIL" =~ ^[0-9]+$ ]]; then
  LOG_TAIL=200
fi
if [[ ! "$START_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  START_TIMEOUT_SECONDS=180
fi

print_usage() {
  # 统一说明 Compose 运维命令和独立 Vite 开发模式，避免混淆两种运行入口。
  printf '用法: %s [--vite|start|stop|status|logs [服务名] [Compose 日志参数...]]\n' "$0"
}

compose() {
  # 优先使用当前进程的 Docker 权限；权限尚未刷新时通过 docker 组临时执行。
  if docker info >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
    return
  fi

  if ! command -v sg >/dev/null 2>&1 || ! getent group docker >/dev/null 2>&1; then
    echo "Docker daemon 不可访问；请确认 Docker 正在运行，并重新登录 docker 组。" >&2
    return 1
  fi

  local command_line argument quoted
  printf -v command_line 'docker compose -f %q' "$COMPOSE_FILE"
  for argument in "$@"; do
    printf -v quoted '%q' "$argument"
    command_line+=" $quoted"
  done
  sg docker -c "$command_line"
}

require_compose() {
  # 启动任何服务前校验 CLI、Compose 文件和插值配置，失败时不触碰旧进程。
  if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 docker；请先安装 Docker Engine/CLI。" >&2
    return 1
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "未找到 curl；start.sh 需要它执行本机 API 健康检查。" >&2
    return 1
  fi
  if [[ ! -f "$COMPOSE_FILE" ]]; then
    echo "未找到 Compose 文件: $COMPOSE_FILE" >&2
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    echo "当前 Docker CLI 未提供 docker compose 子命令。" >&2
    return 1
  fi
  if ! compose --profile app config --quiet; then
    echo "Compose 配置校验失败，请检查 .env 和 $COMPOSE_FILE。" >&2
    return 1
  fi
}

listen_pids() {
  # 只查询 TCP 监听进程，避免误杀连接到应用端口的无关客户端。
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
  fi
}

stop_port_processes() {
  # 只处理本项目固定的旧宿主端口；Docker 代理由 Compose 自己管理。
  local port="$1" pids pid deadline remaining
  pids="$(listen_pids "$port")"
  if [[ -z "$pids" ]]; then
    if ! command -v lsof >/dev/null 2>&1 && command -v fuser >/dev/null 2>&1; then
      fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    fi
    return 0
  fi

  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    kill "$pid" 2>/dev/null || true
  done <<< "$pids"

  deadline=$((SECONDS + 5))
  while (( SECONDS < deadline )); do
    remaining="$(listen_pids "$port")"
    [[ -z "$remaining" ]] && return 0
    sleep 0.2
  done

  # 旧开发服务器若忽略 SIGTERM，最后只对仍占用固定端口的 PID 强制收束。
  remaining="$(listen_pids "$port")"
  while read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    kill -KILL "$pid" 2>/dev/null || true
  done <<< "$remaining"
}

stop_tmux_session() {
  # 清理迁移前遗留的 tmux 会话；没有 tmux 或会话时视为幂等成功。
  if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux kill-session -t "$SESSION_NAME"
  fi
}

compose_service_running() {
  # 判断 Compose 是否已经拥有 API 端口，从而避免启动时误杀 Compose 的 Docker 代理。
  local services
  services="$(compose --profile app ps --status running --services 2>/dev/null || true)"
  grep -Fxq "${1}" <<< "$services"
}

cleanup_legacy_instances() {
  # 启动前清理旧 tmux 和宿主开发端口；正在运行的 Compose API 保持幂等不被误杀。
  stop_tmux_session
  stop_port_processes "$FRONTEND_PORT"
  stop_port_processes "$LEGACY_BACKEND_PORT"
  stop_port_processes "$LEGACY_FRONTEND_PORT"
  if ! compose_service_running mememeow; then
    stop_port_processes "$BACKEND_PORT"
  fi
}

require_vite() {
  # 启动前端开发服务器前检查 Node/npm 与前端工程，避免误触 Compose 或静默失败。
  if ! command -v node >/dev/null 2>&1; then
    echo "未找到 node；--vite 需要安装 Node.js。" >&2
    return 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    echo "未找到 npm；--vite 需要安装 npm。" >&2
    return 1
  fi
  if [[ ! -f "$ROOT_DIR/frontend/package.json" ]]; then
    echo "未找到前端工程: $ROOT_DIR/frontend/package.json" >&2
    return 1
  fi
  if [[ ! "$VITE_PORT" =~ ^[1-9][0-9]*$ ]]; then
    echo "MEMEMEOW_VITE_PORT 必须是正整数。" >&2
    return 1
  fi
}

start_vite() {
  # 保持 Compose 后端独立运行，只在前台启动 Vite 以提供热更新开发入口。
  require_vite
  stop_tmux_session
  stop_port_processes "$VITE_PORT"
  if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
    echo "未找到前端依赖，正在运行 npm install。"
    (cd "$ROOT_DIR/frontend" && npm install)
  fi
  printf 'Vite 开发服务器启动中: http://%s:%s\n' "$VITE_HOST" "$VITE_PORT"
  printf 'API 代理目标: http://127.0.0.1:%s\n' "$BACKEND_PORT"
  cd "$ROOT_DIR/frontend"
  exec npm run dev -- --host "$VITE_HOST" --port "$VITE_PORT"
}

compose_snapshot() {
  # 返回可脚本解析的服务状态快照，异常时交给启动诊断统一处理。
  compose --profile app ps --all --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null || true
}

service_line() {
  # 从 Compose 快照中提取单个服务，避免依赖容器自动生成的名称。
  local snapshot="$1" service="$2"
  awk -F'|' -v service="$service" '$1 == service { print; exit }' <<< "$snapshot"
}

stack_ready() {
  # 检查数据库、Agent、视觉和 API 的运行/健康状态，并由 HTTP 探活确认 API 已接收请求。
  local snapshot="$1" service line state health exit_code
  for service in postgres mememeow-agent-runtime mememeow-visual; do
    line="$(service_line "$snapshot" "$service")"
    IFS='|' read -r _ state health exit_code <<< "$line"
    [[ "$state" == "running" && "$health" == "healthy" ]] || return 1
  done

  line="$(service_line "$snapshot" mememeow)"
  IFS='|' read -r _ state health exit_code <<< "$line"
  [[ "$state" == "running" ]] || return 1
  [[ -z "$health" || "$health" == "healthy" ]] || return 1
  curl --fail --silent --show-error --max-time 5 -o /dev/null "http://127.0.0.1:${BACKEND_PORT}/health"
}

wait_for_stack() {
  # 在固定期限内等待 Compose healthcheck 与 API HTTP 同时就绪，超时保留诊断现场。
  local attempts=$(( (START_TIMEOUT_SECONDS + 1) / 2 )) attempt snapshot line state exit_code
  (( attempts > 0 )) || attempts=1
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    snapshot="$(compose_snapshot)"
    line="$(service_line "$snapshot" db-init)"
    if [[ -n "$line" ]]; then
      IFS='|' read -r _ state _ exit_code <<< "$line"
      if [[ "$state" == "exited" && "$exit_code" != "0" ]]; then
        echo "数据库迁移容器失败（退出码 ${exit_code}）。" >&2
        return 1
      fi
    fi
    if stack_ready "$snapshot"; then
      return 0
    fi
    sleep 2
  done
  echo "Compose 全栈在 ${START_TIMEOUT_SECONDS} 秒内未达到健康状态。" >&2
  return 1
}

print_diagnostics() {
  # 启动失败时同时展示服务状态和有限日志，便于定位镜像、迁移或健康检查问题。
  echo "--- docker compose ps --all ---" >&2
  compose --profile app ps --all >&2 || true
  echo "--- docker compose logs (tail=${LOG_TAIL}) ---" >&2
  compose --profile app logs --tail="$LOG_TAIL" >&2 || true
}

check_endpoint() {
  # 只验证 HTTP 状态，不输出 /config 响应内容，避免运维日志携带敏感配置。
  local path="$1"
  curl --fail --silent --show-error --max-time 10 -o /dev/null "http://127.0.0.1:${BACKEND_PORT}${path}"
}

start_stack() {
  # 构建并启动包含数据库、迁移、Agent、视觉和 API 的完整 Compose 应用。
  require_compose
  cleanup_legacy_instances
  if ! compose --profile app up -d --build; then
    echo "Compose 全栈启动失败。" >&2
    print_diagnostics
    return 1
  fi
  if ! wait_for_stack; then
    print_diagnostics
    return 1
  fi
  local endpoint
  for endpoint in / /health /config; do
    if ! check_endpoint "$endpoint"; then
      echo "API 端点检查失败: http://127.0.0.1:${BACKEND_PORT}${endpoint}" >&2
      print_diagnostics
      return 1
    fi
  done
  compose --profile app ps --all
  printf 'MemeMeow Compose 全栈已启动。\n'
  printf 'API: http://127.0.0.1:%s\n' "$BACKEND_PORT"
  printf '状态: %s status\n' "$0"
  printf '日志: %s logs\n' "$0"
}

stop_stack() {
  # 先停止 Compose 服务，再收束遗留宿主端口；named volume 始终保留。
  require_compose
  if ! compose --profile app stop; then
    echo "Compose 服务停止失败。" >&2
    compose --profile app ps --all >&2 || true
    return 1
  fi
  stop_tmux_session
  stop_port_processes "$BACKEND_PORT"
  stop_port_processes "$FRONTEND_PORT"
  stop_port_processes "$LEGACY_BACKEND_PORT"
  stop_port_processes "$LEGACY_FRONTEND_PORT"
  echo "MemeMeow Compose 服务已停止，数据库、图片和 named volume 未删除。"
}

show_status() {
  # 只读取 Compose 状态，不启动、停止或修改任何服务与 volume。
  require_compose
  compose --profile app ps --all
}

show_logs() {
  # 默认显示最近日志，并把额外参数透传给 Compose 以支持服务筛选或 --follow。
  require_compose
  shift
  compose --profile app logs --tail="$LOG_TAIL" "$@"
}

case "${1:-start}" in
  --vite)
    [[ "$#" -eq 1 ]] || { print_usage >&2; exit 2; }
    start_vite
    ;;
  start)
    [[ "$#" -eq 1 ]] || { print_usage >&2; exit 2; }
    start_stack
    ;;
  stop)
    [[ "$#" -eq 1 ]] || { print_usage >&2; exit 2; }
    stop_stack
    ;;
  status)
    [[ "$#" -eq 1 ]] || { print_usage >&2; exit 2; }
    show_status
    ;;
  logs)
    show_logs "$@"
    ;;
  *)
    print_usage >&2
    exit 2
    ;;
esac
