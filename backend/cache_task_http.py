"""公共核心 `/generate-cache` 任务提交 HTTP 边界。

本模块只负责当前 scope 的 service readiness 检查、缓存生成任务提交和稳定响应投影；
scope/service 与错误构造由入口通过 callback 注入，不反向依赖 ``api.py``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request


async def generate_cache(
    request: Request,
    *,
    service: Callable[[Request, str], Any],
    error: Callable[[int, str, str], HTTPException],
) -> dict[str, object]:
    """提交当前 scope 的缓存生成任务并返回稳定任务摘要。

    关键输入是已绑定 scope 的 Request 和 service/error callback；输出只包含 task_id、
    task_type、status 三个公开字段。调用场景是公共 `POST /generate-cache` route，search
    service 缺失时必须在读取 task service 前 fail-closed。
    """
    engine = service(request, "search")
    if engine is None:
        raise error(503, "service_unavailable", "检索服务未初始化")
    record = service(request, "tasks").submit("cache_generation", {})
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


__all__ = ["generate_cache"]
