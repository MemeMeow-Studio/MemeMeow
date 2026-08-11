"""进程内长任务管理器。

任务仅存于当前进程；关闭服务时未完成任务会被标记为失败，不尝试恢复。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable
from uuid import uuid4


TERMINAL = {"succeeded", "failed"}


def now() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(timezone.utc)


@dataclass
class TaskRecord:
    """任务状态、诊断信息和可选的结构化结果。"""

    task_id: str
    task_type: str
    status: str = "queued"
    progress: float | None = 0.0
    message: str | None = None
    created_at: datetime = field(default_factory=now)
    completed_at: datetime | None = None
    error: dict[str, str] | None = None
    result: Any = None

    def as_dict(self) -> dict[str, Any]:
        """序列化为稳定 JSON 结构。"""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": self.result,
        }


class TaskManager:
    """线程安全的任务注册、执行和状态查询服务。"""

    def __init__(self, max_workers: int = 2):
        self._tasks: dict[str, TaskRecord] = {}
        self._active_by_type: dict[str, str] = {}
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mememeow-task")

    def submit(self, task_type: str, fn: Callable[[Callable[[float | None, str | None], None]], Any]) -> TaskRecord:
        """提交任务；同类型任务在运行或排队时返回已有记录。"""
        with self._lock:
            existing_id = self._active_by_type.get(task_type)
            if existing_id and self._tasks[existing_id].status not in TERMINAL:
                return self._tasks[existing_id]
            record = TaskRecord(task_id=uuid4().hex, task_type=task_type)
            self._tasks[record.task_id] = record
            self._active_by_type[task_type] = record.task_id
        self._executor.submit(self._run, record.task_id, fn)
        return record

    def _run(self, task_id: str, fn: Callable) -> None:
        """在线程池中执行任务并捕获异常。"""
        self.update(task_id, status="running", message="任务开始")

        def progress(value: float | None, message: str | None = None) -> None:
            self.update(task_id, progress=value, message=message)

        try:
            result = fn(progress)
        except Exception as exc:  # noqa: BLE001
            self.update(task_id, status="failed", message="任务执行失败", error={"error": "task_failed", "message": str(exc)})
        else:
            self.update(task_id, status="succeeded", progress=1.0, message="任务完成", result=result)

    def update(self, task_id: str, **changes: Any) -> None:
        """在线程安全地更新任务字段。"""
        with self._lock:
            record = self._tasks.get(task_id)
            if not record or record.status in TERMINAL:
                return
            for key, value in changes.items():
                setattr(record, key, value)
            if record.status in TERMINAL:
                record.completed_at = now()

    def get(self, task_id: str) -> TaskRecord | None:
        """获取任务快照。"""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            return TaskRecord(**record.__dict__)

    def shutdown(self) -> None:
        """关闭执行器并将未完成任务标记为失败。"""
        with self._lock:
            for record in self._tasks.values():
                if record.status not in TERMINAL:
                    record.status = "failed"
                    record.message = "服务已重启，任务不可恢复"
                    record.error = {"error": "task_not_recoverable", "message": "服务重启导致任务失败"}
                    record.completed_at = now()
        self._executor.shutdown(wait=False, cancel_futures=True)
