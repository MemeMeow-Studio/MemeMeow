"""公共核心任务控制 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
import backend.task_http as task_http
from backend.tasks import TaskRecord


def _request(*, tasks: object | None = None, metadata: object | None = None, settings: object | None = None) -> SimpleNamespace:
    """构造不启动 lifespan 的最小任务请求。"""
    state = SimpleNamespace(tasks=tasks, metadata=metadata, settings=settings, agent_activity=None)
    return SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace())


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造任务入口使用的稳定错误 detail。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _repository(_request: object) -> SimpleNamespace:
    """构造没有图片处理父 Job 的 repository。"""
    return SimpleNamespace(snapshot=lambda _task_id: None)


def test_task_http_module_keeps_one_way_dependency_and_legacy_aliases() -> None:
    """新模块不反向导入入口，旧任务符号仍保留。"""
    source = Path(task_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "api" not in imported_modules
    assert "server_api" not in imported_modules
    assert api.list_tasks.__name__ == "list_tasks"
    assert api.get_task.__name__ == "get_task"
    assert api.cancel_task.__name__ == "cancel_task"
    assert api.retry_task.__name__ == "retry_task"
    assert api._task_summary.__name__ == "_task_summary"


def test_task_routes_keep_metadata_and_order() -> None:
    """四个任务 route 保持 tasks tag、method、status 和相对顺序。"""
    paths = {"/search", "/generate-cache", "/tasks", "/tasks/{task_id}", "/tasks/{task_id}/cancel", "/tasks/{task_id}/retry"}
    routes = [route for route in api.app.routes if getattr(route, "path", None) in paths]
    assert [route.path for route in routes] == [
        "/search",
        "/generate-cache",
        "/tasks",
        "/tasks/{task_id}",
        "/tasks/{task_id}/cancel",
        "/tasks/{task_id}/retry",
    ]
    assert [route.methods for route in routes] == [{"POST"}, {"POST"}, {"GET"}, {"GET"}, {"POST"}, {"POST"}]
    assert [route.status_code for route in routes] == [None, 202, None, None, None, 202]
    assert all(route.tags == (["search"] if route.path == "/search" else ["tasks"]) for route in routes)


class _Tasks:
    """记录任务 service 的读取、取消和重试调用。"""

    def __init__(self, records: list[TaskRecord]) -> None:
        self.records = {record.task_id: record for record in records}
        self.calls: list[tuple[str, object]] = []

    def list(self, **kwargs: object) -> tuple[list[TaskRecord], str | None]:
        """返回固定任务页并记录分页参数。"""
        self.calls.append(("list", kwargs))
        return list(self.records.values()), "next-page"

    def get(self, task_id: str) -> TaskRecord | None:
        """按任务 ID 返回当前 scope 任务。"""
        self.calls.append(("get", task_id))
        return self.records.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """记录取消并把任务收束为失败状态。"""
        self.calls.append(("cancel", task_id))
        record = self.records.get(task_id)
        if record is None:
            return False
        record.status = "failed"
        return True

    def retry(self, task_id: str) -> TaskRecord:
        """返回重试后的任务，或抛出稳定错误。"""
        self.calls.append(("retry", task_id))
        record = self.records.get(task_id)
        if record is None:
            raise RuntimeError("task_not_found")
        if record.status != "failed":
            raise RuntimeError("task_not_failed: current status")
        record.status = "queued"
        return record


def test_list_reads_current_service_once_and_projects_metadata() -> None:
    """任务列表只读取当前 scope service，并不返回 payload。"""
    record = TaskRecord(task_id="task-1", task_type="cache_generation", payload={"secret": "hidden"})
    tasks = _Tasks([record])
    metadata = SimpleNamespace(image_for_meme=lambda _meme_id: (None, Path("sample.png")))
    request = _request(tasks=tasks, metadata=metadata)
    service_calls: list[str] = []

    def service(_request: object, name: str) -> object:
        """记录 service 名称并只暴露 fake scope service。"""
        service_calls.append(name)
        return tasks if name == "tasks" else metadata

    response = asyncio.run(
        task_http.list_tasks(
            request,
            status=["queued"],
            task_type=["cache_generation"],
            cursor="cursor-1",
            limit=10,
            service=service,
            processing_repository=_repository,
        )
    )
    assert response["next_cursor"] == "next-page"
    assert response["items"][0]["task_id"] == "task-1"
    assert "payload" not in response["items"][0]
    assert service_calls == ["tasks"]
    assert tasks.calls[0] == (
        "list",
        {"statuses": {"queued"}, "task_types": {"cache_generation"}, "cursor": "cursor-1", "limit": 10},
    )


def test_missing_task_uses_processing_fallback_before_stable_404() -> None:
    """普通任务缺失时查询图片处理回退，均缺失才返回 task_not_found。"""
    tasks = _Tasks([])
    request = _request(tasks=tasks)
    snapshots = {"job-1": SimpleNamespace(as_dict=lambda: {"task_id": "job-1", "status": "running"})}
    repository_calls: list[str] = []

    def repository(_request: object) -> SimpleNamespace:
        """记录父 Job 查询并返回受控快照。"""
        return SimpleNamespace(snapshot=lambda task_id: repository_calls.append(task_id) or snapshots.get(task_id))

    def service(_request: object, name: str) -> object:
        """只返回当前 scope task service。"""
        assert name == "tasks"
        return tasks

    fallback = asyncio.run(task_http.get_task(request, "job-1", service=service, error=_error, processing_repository=repository))
    assert fallback["task_type"] == "visual_embedding_generation"
    assert fallback["image_stage"] == "visual"
    assert repository_calls == ["job-1"]

    with pytest.raises(HTTPException) as caught:
        asyncio.run(task_http.get_task(request, "missing", service=service, error=_error, processing_repository=repository))
    assert caught.value.status_code == 404
    assert caught.value.detail["error"] == "task_not_found"


def test_cancel_calls_agent_adapter_only_for_running_context_task() -> None:
    """取消语境任务时只向适配器传递当前 task id。"""
    record = TaskRecord(task_id="agent-1", task_type="meme_context_generation", status="running")
    tasks = _Tasks([record])
    request = _request(tasks=tasks)
    cancelled: list[str] = []

    def service(_request: object, name: str) -> object:
        """返回当前 scope task service。"""
        assert name == "tasks"
        return tasks

    response = asyncio.run(
        task_http.cancel_task(
            request,
            "agent-1",
            service=service,
            error=_error,
            processing_repository=_repository,
            cancel_agent=lambda _request, task_id: cancelled.append(task_id),
        )
    )
    assert response["status"] == "failed"
    assert cancelled == ["agent-1"]
    assert [call[0] for call in tasks.calls] == ["get", "cancel", "get"]


@pytest.mark.parametrize(
    ("task_id", "status", "expected_status", "expected_code"),
    [("missing", "failed", 404, "task_not_found"), ("queued", "queued", 409, "task_not_failed")],
)
def test_retry_maps_stable_task_errors(task_id: str, status: str, expected_status: int, expected_code: str) -> None:
    """重试错误只返回稳定 HTTP code，不泄露底层诊断文本。"""
    records = [] if task_id == "missing" else [TaskRecord(task_id=task_id, task_type="cache_generation", status=status)]
    tasks = _Tasks(records)
    request = _request(tasks=tasks)

    def service(_request: object, name: str) -> object:
        """返回当前 scope task service。"""
        assert name == "tasks"
        return tasks

    with pytest.raises(HTTPException) as caught:
        asyncio.run(task_http.retry_task(request, task_id, service=service, error=_error, processing_repository=_repository))
    assert caught.value.status_code == expected_status
    assert caught.value.detail == {"error": expected_code, "message": "任务不存在" if expected_code == "task_not_found" else "只有失败任务可以重试"}
