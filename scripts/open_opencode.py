"""打开 MemeMeow 专用 OpenCode runtime，供开发者检查历史图片分析会话。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import Settings  # noqa: E402
from backend.opencode import OpenCodeError, OpenCodeRunner  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """创建启动器参数解析器；未知参数原样交给 OpenCode TUI。"""
    parser = argparse.ArgumentParser(description="打开 MemeMeow OpenCode 会话检查界面")
    parser.add_argument("--list", action="store_true", help="在终端列出历史 session 后退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    """准备共享 runtime 并以同一 DB 启动 TUI，调用场景为本地诊断历史 session。"""
    parser = build_parser()
    known, passthrough = parser.parse_known_args(argv)
    settings = Settings.from_env(PROJECT_ROOT / ".env")
    runner = OpenCodeRunner(settings, project_root=PROJECT_ROOT)
    try:
        runner.prepare_runtime()
    except OpenCodeError as exc:
        parser.exit(1, f"OpenCode runtime 初始化失败 [{exc.code}]: {exc}\n")

    executable = str(settings.opencode_executable)
    environment = runner.build_environment()
    if known.list:
        command = [executable, "session", "list", *passthrough]
    else:
        command = [
            executable,
            str(runner.workspace),
            "--model",
            str(settings.opencode_model),
            *passthrough,
        ]
    try:
        os.chdir(runner.workspace)
        os.execvpe(executable, command, environment)
    except OSError as exc:
        parser.exit(1, f"无法启动 OpenCode: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
