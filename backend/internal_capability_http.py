"""公共 HTTP 能力和内部 callback 装配边界。

本模块承载 operation availability 以及两个内部 callback 入口的薄 HTTP glue。反向图片
和视觉匹配的 task、claim、scope、provider/service 逻辑继续位于各自领域模块；入口通过
显式 callback 注入 database、registry、binding、错误工厂和 delegate，禁止反向导入
``api.py`` 或 ``server_api.py``。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Awaitable

from fastapi import HTTPException, Request

from backend.callbacks import DEFAULT_CALLBACK_REGISTRY
from backend.operation_policy import Operations, OperationPolicyError
from backend.reverse_image_http import internal_reverse_image_search as _reverse_image_search_http
from backend.visual_callback_http import internal_visual_search_match as _visual_search_match_http


ErrorFactory = Callable[[int, str, str], HTTPException]


async def operation_availability(
    request: Request,
    operation: str | None = None,
    *,
    request_scope: Callable[[Request], Any],
    operation_gateway: Callable[[Request], Any],
    error: ErrorFactory,
) -> dict[str, object]:
    """返回当前 scope 的 operation 能力提示且不建立 reservation。

    输入是请求、可选 operation 名称和入口注入的 scope/policy/error callback；输出是稳定
    `items` 列表。调用场景是公开能力查询，始终只调用 gateway probe，不调用 acquire、
    commit 或 release。
    """
    names = [operation] if operation else sorted(Operations.ALL)
    if any(name not in Operations.ALL for name in names):
        raise error(400, "operation_unknown", "操作类型无效")
    gateway = operation_gateway(request)
    values: list[dict[str, object]] = []
    for name in names:
        try:
            decision = gateway.probe(gateway.request(request_scope(request), name, f"probe:{name}"))
        except OperationPolicyError as exc:
            item = {"operation": name, "available": False, "reason": exc.code}
            if exc.retry_at is not None:
                item["retry_at"] = exc.retry_at.isoformat() if isinstance(exc.retry_at, datetime) else str(exc.retry_at)
            values.append(item)
            continue
        item = {"operation": name, "available": bool(decision.allowed)}
        if not decision.allowed:
            item["reason"] = decision.reason if decision.reason in {"operation_forbidden", "operation_limit_exceeded", "operation_policy_unavailable"} else "operation_policy_unavailable"
        if decision.retry_at is not None:
            item["retry_at"] = decision.retry_at.isoformat() if isinstance(decision.retry_at, datetime) else str(decision.retry_at)
        values.append(item)
    return {"items": values}


async def internal_reverse_image_search(
    request: Request,
    *,
    task_id: str,
    image: Any,
    request_id: str | None,
    input_digest: str | None,
    search_type: str,
    language: str,
    country: str | None,
    query: str | None,
    auto_crop: bool,
    refresh: bool,
    binding: Callable[[Request], Any],
    registration: Callable[[Request], Any],
    database: Callable[[Request], Any],
    scope_services: Callable[[Request, Any], Any],
    error: ErrorFactory,
    delegate: Callable[..., Awaitable[dict[str, object]]] = _reverse_image_search_http,
) -> dict[str, object]:
    """在 callback binding 初步可信后读取图片并委托领域反向检索 handler。

    输入是 FastAPI UploadFile、callback 字段和显式宿主依赖；输出是领域 handler 的脱敏
    结果。调用场景是内部 multipart callback：缺少 registration/binding 或 task 不匹配时
    先拒绝并不调用 `image.read()`，随后由已有领域模块完成完整 target SHA、claim、scope
    和 request digest 复核。
    """
    callback_binding = binding(request)
    callback_registration = registration(request)
    if callback_registration is None:
        raise error(413, "agent_callback_body_too_large", "内部请求体超过限制")
    if callback_binding is None or getattr(callback_binding, "task_id", None) != task_id:
        raise error(401, "agent_callback_unauthorized", "内部执行凭据无效")
    content = await image.read()
    return await delegate(
        request,
        task_id=task_id,
        content=content,
        filename=getattr(image, "filename", None),
        request_id=request_id,
        input_digest=input_digest,
        search_type=search_type,
        language=language,
        country=country,
        query=query,
        auto_crop=auto_crop,
        refresh=refresh,
        binding=binding,
        registration=registration,
        database=database,
        scope_services=scope_services,
        error=error,
    )


async def internal_visual_search_match(
    request: Request,
    payload: Any,
    *,
    binding: Callable[[Request], Any],
    registration: Callable[[Request], Any],
    database: Callable[[Request], Any],
    scope_services: Callable[[Request, Any], Any],
    error: ErrorFactory,
    delegate: Callable[..., Awaitable[dict[str, object]]] = _visual_search_match_http,
) -> dict[str, object]:
    """通过显式依赖委托内部视觉匹配 callback。

    输入是 callback JSON payload 和 binding/registry/database/service/error callback；输出是
    视觉领域模块的稳定结果。调用场景是内部 callback 路由，所有 task、claim、scope、事实
    幂等和错误状态仍由领域 delegate 负责，入口不重复维护安全事实。
    """
    return await delegate(
        request,
        payload,
        binding=binding,
        registration=registration,
        database=database,
        scope_services=scope_services,
        error=error,
    )


def callback_registration(request: Request) -> Any:
    """读取当前 callback path 的 registry 注册项。

    输入是 request；输出是注册描述或 None。调用场景是入口注入 callback delegate，缺失
    registry 时明确返回 None，由内部 handler 映射为稳定拒绝而不安装默认跨宿主状态。
    """
    registry = getattr(request.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
    return registry.get(request.url.path) if registry is not None else None


__all__ = [
    "callback_registration",
    "internal_reverse_image_search",
    "internal_visual_search_match",
    "operation_availability",
]
