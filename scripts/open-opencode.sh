#!/usr/bin/env bash
# 使用项目虚拟环境启动共享 OpenCode runtime 的会话检查入口。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${MEMEMEOW_PYTHON:-$ROOT_DIR/.venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "未找到项目 Python: $PYTHON_BIN" >&2
  echo "请先创建 .venv，或通过 MEMEMEOW_PYTHON 指定解释器。" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/open_opencode.py" "$@"
