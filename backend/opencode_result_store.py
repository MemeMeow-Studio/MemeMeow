"""OpenCode 任务结果文件的受控存储边界。

该模块只负责 task 专属目录、临时文件和原子结果文件的生命周期，不解析业务
schema，也不访问数据库。``OpenCodeRunner`` 通过它准备/清理文件；业务字段校验
仍由 runner 的公开 DTO 规则完成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ResultStoreError(RuntimeError):
    """结果文件路径、大小或 JSON 结构不符合受控存储协议。"""

    def __init__(self, code: str) -> None:
        """创建带稳定错误码的结果存储异常。"""
        self.code = code
        super().__init__(code)


class OpenCodeResultStore:
    """按 task 管理 draft/result 文件，并拒绝符号链接和越界路径。"""

    def __init__(self, root: Path, *, result_name: str = "result.json.tmp", draft_name: str = "result.json.draft", max_bytes: int = 1024 * 1024) -> None:
        """创建结果 store；root 和大小上限由 runtime 配置提供。"""
        if max_bytes <= 0:
            raise ValueError("result_size_limit_invalid")
        self.root = Path(root).expanduser().absolute()
        self.result_name = result_name
        self.draft_name = draft_name
        self.max_bytes = max_bytes

    def paths(self, task_id: str) -> tuple[Path, Path]:
        """返回 task 专属 draft/result 路径，不创建目录或文件。"""
        if not isinstance(task_id, str) or not task_id or "/" in task_id or "\\" in task_id or task_id in {".", ".."}:
            raise ResultStoreError("agent_result_path_invalid")
        directory = self.root / task_id
        return directory / self.draft_name, directory / self.result_name

    @staticmethod
    def _assert_regular(path: Path, *, allow_missing: bool) -> None:
        """拒绝 symlink、目录和不可见的特殊文件。"""
        if path.is_symlink():
            raise ResultStoreError("agent_result_path_invalid")
        if not path.exists():
            if allow_missing:
                return
            raise ResultStoreError("agent_result_file_missing")
        if not path.is_file():
            raise ResultStoreError("agent_result_path_invalid")

    def prepare(self, task_id: str) -> tuple[Path, Path]:
        """创建 task 结果目录并清理首次 attempt 的旧文件。"""
        draft, result = self.paths(task_id)
        self.root.mkdir(parents=True, exist_ok=True)
        directory = draft.parent
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ResultStoreError("agent_result_path_invalid")
        directory.mkdir(parents=True, exist_ok=True)
        for path in (draft, result):
            self._assert_regular(path, allow_missing=True)
            path.unlink(missing_ok=True)
        return draft, result

    def read_json(self, path: Path) -> dict[str, Any]:
        """在大小、普通文件和 JSON 对象边界内读取结果。"""
        path = Path(path)
        self._assert_regular(path, allow_missing=False)
        try:
            metadata = path.stat()
        except OSError as exc:
            raise ResultStoreError("agent_result_file_unreadable") from exc
        if metadata.st_size > self.max_bytes:
            raise ResultStoreError("agent_result_file_too_large")
        try:
            with path.open("rb") as handle:
                raw = handle.read(self.max_bytes + 1)
        except OSError as exc:
            raise ResultStoreError("agent_result_file_unreadable") from exc
        if len(raw) > self.max_bytes:
            raise ResultStoreError("agent_result_file_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResultStoreError("agent_result_file_invalid_json") from exc
        if not isinstance(value, dict):
            raise ResultStoreError("agent_result_file_schema_invalid")
        return value

    def write_atomic(self, task_id: str, value: dict[str, Any]) -> Path:
        """以同一文件系统的临时文件替换结果文件。"""
        draft, result = self.paths(task_id)
        if not isinstance(value, dict):
            raise ResultStoreError("agent_result_file_schema_invalid")
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ResultStoreError("agent_result_file_too_large")
        result.parent.mkdir(parents=True, exist_ok=True)
        self._assert_regular(result.parent, allow_missing=True)
        temporary = result.with_name(f".{result.name}.tmp.{os.getpid()}")
        self._assert_regular(temporary, allow_missing=True)
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, result)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ResultStoreError("agent_result_file_unreadable") from exc
        _ = draft
        return result

    def cleanup(self, *, keep_task_id: str | None = None) -> int:
        """删除 root 下的旧 task 结果目录并保留当前 task。"""
        if not self.root.exists():
            return 0
        removed = 0
        for directory in self.root.iterdir():
            if keep_task_id is not None and directory.name == keep_task_id:
                continue
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                for child in directory.iterdir():
                    if child.is_symlink() or not child.is_file():
                        continue
                    child.unlink(missing_ok=True)
                directory.rmdir()
                removed += 1
            except OSError:
                continue
        return removed


__all__ = ["OpenCodeResultStore", "ResultStoreError"]
