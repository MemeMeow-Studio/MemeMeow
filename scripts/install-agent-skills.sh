#!/usr/bin/env bash
# 将项目内受版本控制的共享 skill 链接到各 Agent 的发现目录。

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_NAME="research-meme-context"
SOURCE_DIR="$ROOT_DIR/skills/$SKILL_NAME"
RELATIVE_SOURCE="../../skills/$SKILL_NAME"

if [[ ! -f "$SOURCE_DIR/SKILL.md" ]]; then
  echo "缺少 skill 入口: $SOURCE_DIR/SKILL.md" >&2
  exit 1
fi

# 为指定 Agent 创建可移植的相对链接。输入为发现目录；成功时创建或规范化链接，
# 已存在的真实文件、目录或指向其他位置的链接会导致安装失败，避免覆盖用户内容。
install_skill_link() {
  local skills_dir="$1"
  local target="$skills_dir/$SKILL_NAME"

  mkdir -p "$skills_dir"

  if [[ -L "$target" ]]; then
    if [[ "$(readlink -f -- "$target")" != "$(readlink -f -- "$SOURCE_DIR")" ]]; then
      echo "已有符号链接指向其他位置: $target" >&2
      return 1
    fi
    ln -sfn -- "$RELATIVE_SOURCE" "$target"
    echo "已确认 skill 链接: $target"
    return
  fi

  if [[ -e "$target" ]]; then
    echo "目标已存在且不是符号链接，不会覆盖: $target" >&2
    return 1
  fi

  ln -s -- "$RELATIVE_SOURCE" "$target"
  echo "已安装 skill 链接: $target"
}

install_skill_link "$ROOT_DIR/.agents/skills"
install_skill_link "$ROOT_DIR/.opencode/skills"
