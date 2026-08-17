#!/usr/bin/env bash
# Compose 运行身份解析，供各个项目运维入口共享。

validate_runtime_identity() {
  # Compose 的 user 插值只接受非 root 数值身份，避免任何隐式回退到 root。
  local name="$1" value="$2"
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "${name} 必须是非 root 正整数。" >&2
    return 1
  fi
}

read_dotenv_runtime_value() {
  # 只读取身份字段，避免为推导运行身份而执行或回显整个 dotenv 文件。
  local name="$1" env_file="${ROOT_DIR:-.}/.env" line value
  [[ -f "$env_file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ "$line" == "$name="* ]] || continue
    value="${line#*=}"
    value="${value%%#*}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" == '"'*'"' && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "$value" == "'"*"'" && ${#value} -ge 2 ]]; then
      value="${value:1:${#value}-2}"
    fi
    printf '%s' "$value"
    return 0
  done < "$env_file"
  return 1
}

configure_runtime_identity() {
  # 显式环境变量优先；未提供时采用启动入口当前服务用户的数值身份。
  local runtime_uid="${MEMEMEOW_RUNTIME_UID:-}" runtime_gid="${MEMEMEOW_RUNTIME_GID:-}"
  if [[ -z "$runtime_uid" ]]; then
    runtime_uid="$(read_dotenv_runtime_value MEMEMEOW_RUNTIME_UID || true)"
  fi
  if [[ -z "$runtime_gid" ]]; then
    runtime_gid="$(read_dotenv_runtime_value MEMEMEOW_RUNTIME_GID || true)"
  fi
  if [[ -z "$runtime_uid" ]]; then
    if ! command -v id >/dev/null 2>&1; then
      echo "未找到 id；无法推导 MEMEMEOW_RUNTIME_UID。" >&2
      return 1
    fi
    runtime_uid="$(id -u)"
  fi
  if [[ -z "$runtime_gid" ]]; then
    if ! command -v id >/dev/null 2>&1; then
      echo "未找到 id；无法推导 MEMEMEOW_RUNTIME_GID。" >&2
      return 1
    fi
    runtime_gid="$(id -g)"
  fi
  validate_runtime_identity MEMEMEOW_RUNTIME_UID "$runtime_uid"
  validate_runtime_identity MEMEMEOW_RUNTIME_GID "$runtime_gid"
  export MEMEMEOW_RUNTIME_UID="$runtime_uid"
  export MEMEMEOW_RUNTIME_GID="$runtime_gid"
}
