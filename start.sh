#!/usr/bin/env bash
# 一键停止旧实例，并在 tmux 中启动 MemeMeow 前后端。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SESSION_NAME="mememeow"
BACKEND_PORT="8275"
FRONTEND_PORT="5275"
LEGACY_BACKEND_PORT="8000"
LEGACY_FRONTEND_PORT="5173"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
UV_COMMAND="$(command -v uv || true)"
if [[ -z "$UV_COMMAND" && -n "${HOME:-}" && -x "$HOME/.local/bin/uv" ]]; then
  UV_COMMAND="$HOME/.local/bin/uv"
fi

stop_port_processes() {
  # 只处理本脚本使用的固定端口，避免误杀其他服务。
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -ti "tcp:${port}" || true)"
    if [[ -n "$pids" ]]; then
      kill $pids 2>/dev/null || true
    fi
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

stop_services() {
  if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux kill-session -t "$SESSION_NAME"
  fi
  stop_port_processes "$BACKEND_PORT"
  stop_port_processes "$FRONTEND_PORT"
  # 清理迁移前曾使用的默认端口，避免旧实例继续占用资源。
  stop_port_processes "$LEGACY_BACKEND_PORT"
  stop_port_processes "$LEGACY_FRONTEND_PORT"
}

case "${1:-start}" in
  stop)
    stop_services
    echo "MemeMeow 服务已停止。"
    exit 0
    ;;
  attach)
    exec tmux attach-session -t "$SESSION_NAME"
    ;;
  start)
    ;;
  *)
    echo "用法: $0 [start|stop|attach]" >&2
    exit 2
    ;;
esac

if ! command -v tmux >/dev/null 2>&1; then
  echo "未找到 tmux，请先安装 tmux。" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "未找到 npm，请先安装 Node.js。" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "未找到 Python 虚拟环境，正在使用 uv 创建。"
  if [[ -z "$UV_COMMAND" ]]; then
    echo "未找到 uv，请先安装 uv。" >&2
    exit 1
  fi
  (cd "$ROOT_DIR" && "$UV_COMMAND" sync --dev)
fi

stop_services

if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
  (cd "$ROOT_DIR/frontend" && npm install)
fi

tmux new-session -d -s "$SESSION_NAME" -n backend
tmux send-keys -t "$SESSION_NAME:backend" "cd '$ROOT_DIR' && '$PYTHON_BIN' -m uvicorn api:app --host 0.0.0.0 --port '$BACKEND_PORT'" C-m
tmux new-window -t "$SESSION_NAME" -n frontend
tmux send-keys -t "$SESSION_NAME:frontend" "cd '$ROOT_DIR/frontend' && npm run dev -- --host 0.0.0.0 --port '$FRONTEND_PORT'" C-m

echo "MemeMeow 已在 tmux 会话 '$SESSION_NAME' 中启动。"
echo "前端: http://127.0.0.1:${FRONTEND_PORT}"
echo "后端: http://127.0.0.1:${BACKEND_PORT}"
echo "查看日志: tmux attach -t ${SESSION_NAME}"
