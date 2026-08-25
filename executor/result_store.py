"""executor 结果文件协议。

该模块只处理受控结果目录、普通文件、大小限制和 JSON/schema 验证；HTTP handler
和进程 supervisor 不应直接读取任意路径。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Callable


class ExecutorResultStoreError(RuntimeError):
    """结果文件无法安全读取或通过 schema 校验。"""

    def __init__(self, code: str) -> None:
        """创建稳定结果错误。"""
        self.code = code
        super().__init__(code)


class ExecutorResultStore:
    """绑定一个根目录和结果大小上限的 executor 结果读取器。"""

    def __init__(self, root: Path, *, filename: str, max_bytes: int) -> None:
        """创建结果读取器；root 不会被自动解析到符号链接外部。"""
        self.root = Path(root)
        self.filename = filename
        self.max_bytes = max_bytes

    def read(self, path: Path, *, required_fields: set[str], validator: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """读取固定层级结果文件并执行有限 schema 验证。"""
        current = self.root
        try:
            root_info = current.lstat()
            relative = Path(path).relative_to(current)
        except (OSError, ValueError) as exc:
            raise ExecutorResultStoreError("agent_result_path_invalid") from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode) or len(relative.parts) != 2 or relative.name != self.filename:
            raise ExecutorResultStoreError("agent_result_path_invalid")
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError as exc:
                raise ExecutorResultStoreError("agent_result_file_missing") from exc
            except OSError as exc:
                raise ExecutorResultStoreError("agent_result_file_unreadable") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ExecutorResultStoreError("agent_result_path_invalid")
        try:
            info = Path(path).lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ExecutorResultStoreError("agent_result_file_unreadable")
            if info.st_size > self.max_bytes:
                raise ExecutorResultStoreError("agent_result_file_too_large")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, self.max_bytes + 1)
            finally:
                os.close(descriptor)
        except ExecutorResultStoreError:
            raise
        except FileNotFoundError as exc:
            raise ExecutorResultStoreError("agent_result_file_missing") from exc
        except OSError as exc:
            raise ExecutorResultStoreError("agent_result_file_unreadable") from exc
        if len(raw) > self.max_bytes:
            raise ExecutorResultStoreError("agent_result_file_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ExecutorResultStoreError("agent_result_file_invalid_json") from exc
        if not isinstance(value, dict) or not required_fields.issubset(value):
            raise ExecutorResultStoreError("agent_result_file_schema_invalid")
        if validator is not None:
            try:
                validator(value)
            except Exception as exc:  # noqa: BLE001 - validator 只允许映射为稳定错误
                raise ExecutorResultStoreError("agent_result_file_schema_invalid") from exc
        return value


__all__ = ["ExecutorResultStore", "ExecutorResultStoreError"]
