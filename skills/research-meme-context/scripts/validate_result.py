"""校验共享 Agent 容器的最终研究结果文件。

本脚本位于 ``research-meme-context`` Skill 中，供 Agent 在结束任务前确认
任务结果遵循后端的文件交付协议。后端仍会执行完整的 schema 与业务模型校验。
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path


RESULT_FILE_NAME = "result.json.tmp"
COMMON_MISNAMED_FILE = "result.json"
DEFAULT_MAX_BYTES = 1024 * 1024


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """解析任务结果目录和可选的文件大小上限。

    Agent 在完成 JSON 原子替换后传入任务专属目录；成功时返回零退出码。
    """
    parser = argparse.ArgumentParser(description="校验 MemeMeow Agent 结果文件协议")
    parser.add_argument("task_directory", type=Path, help="/runtime/task-results/<task_id> 目录")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"允许的最大文件大小，默认 {DEFAULT_MAX_BYTES} 字节",
    )
    return parser.parse_args(arguments)


def validate_result_file(task_directory: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> list[str]:
    """检查结果文件协议并返回诊断信息。

    输入为单个任务结果目录和大小上限；输出为空列表表示文件名、节点类型、大小和
    JSON 语法均有效，否则返回可直接据此修复的错误列表。
    """
    errors: list[str] = []
    if max_bytes <= 0:
        return ["--max-bytes 必须大于 0"]

    result_path = task_directory / RESULT_FILE_NAME
    common_misnamed_path = task_directory / COMMON_MISNAMED_FILE
    try:
        info = result_path.lstat()
    except FileNotFoundError:
        if common_misnamed_path.exists():
            errors.append(
                f"缺少 {RESULT_FILE_NAME}；发现 {COMMON_MISNAMED_FILE}，"
                f"请将其原子重命名为 {RESULT_FILE_NAME}"
            )
        else:
            errors.append(f"缺少最终结果文件 {RESULT_FILE_NAME}")
        return errors
    except OSError as exc:
        return [f"无法检查 {RESULT_FILE_NAME}: {exc}"]

    if stat.S_ISLNK(info.st_mode):
        return [f"{RESULT_FILE_NAME} 不能是符号链接"]
    if not stat.S_ISREG(info.st_mode):
        return [f"{RESULT_FILE_NAME} 必须是普通文件"]
    if info.st_size == 0:
        return [f"{RESULT_FILE_NAME} 不能为空"]
    if info.st_size > max_bytes:
        return [f"{RESULT_FILE_NAME} 超过 {max_bytes} 字节限制"]

    try:
        with result_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"{RESULT_FILE_NAME} 不是有效 UTF-8 JSON: {exc}"]
    if not isinstance(value, dict):
        return [f"{RESULT_FILE_NAME} 的根节点必须是 JSON 对象"]
    return errors


def main(arguments: list[str] | None = None) -> int:
    """运行结果文件协议校验并以稳定退出码向 Agent 报告结果。

    校验失败时逐行输出诊断到标准错误并返回 1，供 Agent 修复后重新执行。
    """
    args = parse_args(arguments)
    errors = validate_result_file(args.task_directory, max_bytes=args.max_bytes)
    if errors:
        print("Agent 结果校验失败：", file=sys.stderr)
        print(*errors, sep="\n", file=sys.stderr)
        return 1
    print(f"Agent 结果校验通过：{args.task_directory / RESULT_FILE_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
