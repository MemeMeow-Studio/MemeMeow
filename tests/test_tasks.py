"""进程内长任务状态机测试。"""

from __future__ import annotations

import time
from threading import Event

from backend.tasks import TaskManager


def wait_for_terminal(manager: TaskManager, task_id: str, timeout: float = 2.0):
    """轮询测试任务直到终态，超时即失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = manager.get(task_id)
        if record and record.status in {"succeeded", "failed"}:
            return record
        time.sleep(0.01)
    raise AssertionError("任务未在测试时限内完成")


def test_task_succeeds_and_reports_progress():
    """成功任务按 queued/running/succeeded 状态机结束。"""
    manager = TaskManager(max_workers=1)
    record = manager.submit("cache_generation", lambda progress: progress(0.5, "half"))
    completed = wait_for_terminal(manager, record.task_id)
    assert completed.status == "succeeded"
    assert completed.progress == 1.0
    assert completed.completed_at is not None
    manager.shutdown()


def test_duplicate_active_type_returns_same_task():
    """同类型未完成任务不会并发执行。"""
    release = Event()
    manager = TaskManager(max_workers=1)
    first = manager.submit("cache_generation", lambda progress: release.wait(1))
    second = manager.submit("cache_generation", lambda progress: None)
    assert second.task_id == first.task_id
    release.set()
    wait_for_terminal(manager, first.task_id)
    manager.shutdown()


def test_failure_is_diagnostic_and_shutdown_marks_pending_failed():
    """异常和服务关闭都生成稳定失败信息。"""
    manager = TaskManager(max_workers=1)

    def fail(progress):
        raise RuntimeError("boom")

    failed = manager.submit("failure", fail)
    completed = wait_for_terminal(manager, failed.task_id)
    assert completed.status == "failed"
    assert completed.error["error"] == "task_failed"

    release = Event()
    pending = manager.submit("pending", lambda progress: release.wait(1))
    manager.shutdown()
    stopped = manager.get(pending.task_id)
    assert stopped.status == "failed"
    assert stopped.error["error"] == "task_not_recoverable"
    release.set()
