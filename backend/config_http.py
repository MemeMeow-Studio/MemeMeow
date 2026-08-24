"""公共核心 `/config` HTTP 边界。

本模块负责配置状态的脱敏投影和存储预检摘要。scope 与业务 service 的解析由入口通过
callback 注入，避免复制 scope 校验或反向依赖 ``api.py``；应用入口只保留路由兼容 wrapper。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Request

from backend.scope import ScopeServices


STORAGE_PREFLIGHT_BLOCKING_KEYS = ("non_flat_keys", "nested_images", "missing_files", "mismatched")


def _storage_preflight_summary(report: Mapping[str, object] | None) -> dict[str, object]:
    """生成不包含文件名的存储预检摘要，供健康检查和配置接口诊断。"""
    report = report or {}
    blocking = {
        key: len(value) if isinstance(value, (list, tuple, set, dict)) else 0
        for key in STORAGE_PREFLIGHT_BLOCKING_KEYS
        if (value := report.get(key))
    }
    orphan_files = report.get("orphan_files")
    return {
        "status": "warning" if orphan_files else "ok",
        "orphan_files": len(orphan_files) if isinstance(orphan_files, (list, tuple, set, dict)) else 0,
        "blocking_errors": blocking,
    }


async def config_status(
    request: Request,
    *,
    request_scope: Callable[[Request], Any],
    service: Callable[[Request, str], Any],
) -> dict[str, object]:
    """返回脱敏配置状态，并复用入口注入的 scope/service 解析。

    关键输入是已由应用入口装配的 Request 和两个 scope-bound callback；输出只包含固定
    配置、缓存、存储、视觉和 runtime 状态字段。调用场景是公共核心的 `GET /config`
    路由 wrapper，callback 不得回退到跨请求 singleton。
    """
    status = request.app.state.settings.status()
    # embedding 缓存属于运行时状态，供前端判断当前是否可以直接检索。
    services = getattr(request.state, "services", None)
    engine = services.search if isinstance(services, ScopeServices) else getattr(request.app.state, "search_engine", None)
    status["embedding_cache_ready"] = bool(engine and engine.has_cache())
    status["database_ready"] = True
    if getattr(request.app.state, "expose_scope", True):
        status["scope_id"] = request_scope(request).scope_id
    status["storage_preflight"] = _storage_preflight_summary(getattr(request.app.state, "storage_preflight", None))
    reverse_service = service(request, "reverse_image") if hasattr(request.app.state, "reverse_image") or getattr(request, "state", None) and getattr(request.state, "services", None) is not None else None
    status["reverse_image_available"] = bool(reverse_service and reverse_service.available)
    visual_client = getattr(request.app.state, "visual_inference", None)
    if visual_client is not None:
        status["visual_available"] = bool(visual_client.health().get("available"))
    runtime = request.app.state.opencode.runtime_probe()
    # /config 只暴露固定标识和布尔探针，不返回宿主绝对路径、诊断原文或任何凭据。
    status["runtime_ready"] = bool(runtime.get("verified"))
    status["agent_runtime"] = {
        key: runtime[key]
        for key in (
            "mode",
            "executor_running",
            "runtime_root_ready",
            "workspace_ready",
            "executable_ready",
            "skills_ready",
            "dependencies_ready",
            "mounts_ready",
            "non_root",
            "network_ready",
            "docker_socket_absent",
            "concurrency",
            "verified",
        )
        if key in runtime
    }
    return status


__all__ = [
    "STORAGE_PREFLIGHT_BLOCKING_KEYS",
    "_storage_preflight_summary",
    "config_status",
]
