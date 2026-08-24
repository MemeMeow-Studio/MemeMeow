"""公共核心 `/generate-cache` HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
import backend.cache_task_http as cache_task_http


def _request() -> SimpleNamespace:
    """构造不启动 lifespan 的最小缓存任务请求。"""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), state=SimpleNamespace())


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造公共入口使用的稳定错误 detail。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def test_cache_task_boundary_preserves_alias_and_one_way_dependency() -> None:
    """新模块与旧入口保留 handler 兼容名称，且不反向依赖入口。"""
    assert api.generate_cache.__name__ == "generate_cache"
    source = Path(cache_task_http.__file__).read_text(encoding="utf-8")
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


def test_cache_task_route_snapshot_keeps_metadata_and_order() -> None:
    """`/generate-cache` 继续是单个 202 POST tasks 路由，位于 search 后。"""
    relevant = [route for route in api.app.routes if getattr(route, "path", None) in {"/search", "/generate-cache", "/tasks"}]
    assert [route.path for route in relevant[:2]] == ["/search", "/generate-cache"]
    route = relevant[1]
    assert route.methods == {"POST"}
    assert route.status_code == 202
    assert route.name == "generate_cache"
    assert route.tags == ["tasks"]
    assert sum(getattr(item, "path", None) == "/generate-cache" for item in api.app.routes) == 1


def test_cache_task_submission_checks_search_then_submits_empty_payload() -> None:
    """缓存任务先确认 search service，再提交一次空 payload 并投影公开字段。"""
    request = _request()
    calls: list[tuple[str, object]] = []

    class Tasks:
        """记录缓存任务提交参数。"""

        def submit(self, task_type: str, payload: dict[str, object]) -> SimpleNamespace:
            """返回最小可轮询任务摘要。"""
            calls.append((task_type, payload))
            return SimpleNamespace(task_id="task-1", task_type=task_type, status="queued", payload={"secret": "hidden"})

    tasks = Tasks()

    def service(_request: object, name: str) -> object:
        """按 readiness 顺序返回 search/tasks service。"""
        calls.append((name, None))
        return SimpleNamespace() if name == "search" else tasks

    result = asyncio.run(cache_task_http.generate_cache(request, service=service, error=_error))
    assert result == {"task_id": "task-1", "task_type": "cache_generation", "status": "queued"}
    assert calls == [("search", None), ("tasks", None), ("cache_generation", {})]
    assert "secret" not in repr(result)


def test_cache_task_service_unavailable_fails_before_tasks_lookup() -> None:
    """search service 缺失时返回稳定错误且不读取 task service。"""
    request = _request()
    calls: list[str] = []

    def service(_request: object, name: str) -> None:
        """记录不应越过 search readiness 的访问。"""
        calls.append(name)
        return None

    with pytest.raises(HTTPException) as caught:
        asyncio.run(cache_task_http.generate_cache(request, service=service, error=_error))
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "service_unavailable"
    assert calls == ["search"]
