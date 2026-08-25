"""公共核心内部视觉匹配 callback HTTP 边界。

本模块负责 callback binding、持久 task/fact、request_id 绑定和视觉 service 的调用顺序；
数据库、scope service、路由注册和错误工厂由入口通过 callback 注入，不反向依赖
``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from backend.callbacks import (
    CallbackError,
    CallbackBinding,
    binding_input_digest,
    canonical_callback_request_id,
    validate_binding_task,
    validate_request_id,
)
from backend.database import DatabaseError, ScopeContext
from backend.scope import ScopeResolutionError
from backend.visual import VisualSearchError


class _StrictRequestModel(BaseModel):
    """内部 callback JSON 请求基类，拒绝客户端覆盖内部执行字段。"""

    model_config = ConfigDict(extra="forbid")


class VisualMatchRequest(_StrictRequestModel):
    """Agent 视觉匹配请求；scope 和查询图片只能由 task_id 推导。"""

    task_id: str = Field(min_length=1, max_length=255)
    request_id: str | None = Field(default=None, max_length=128)
    top_k: StrictInt = Field(default=20, ge=1, le=50)
    exclude_self: bool = True


BindingProvider = Callable[[Request], Any]
RegistrationProvider = Callable[[Request], Any]
DatabaseProvider = Callable[[Request], Any]
ScopeServicesProvider = Callable[[Request, ScopeContext], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]


async def internal_visual_search_match(
    request: Request,
    payload: VisualMatchRequest,
    *,
    binding: BindingProvider,
    registration: RegistrationProvider,
    database: DatabaseProvider,
    scope_services: ScopeServicesProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """按 callback claim 和 scope 事实执行幂等视觉匹配。

    输入是 callback token 已绑定的 task_id、request_id 和查询参数；输出是视觉 service 的
    安全结果。调用场景是内部 Agent callback，任何绑定、数据库或 scope 事实异常都 fail-closed。
    """
    callback_binding = binding(request)
    callback_registration = registration(request)
    if not isinstance(callback_binding, CallbackBinding) or callback_registration is None or callback_binding.task_id != payload.task_id:
        raise error(401, "agent_callback_unauthorized", "内部执行凭据无效")
    try:
        body_request_id = validate_request_id(payload.request_id)
        header_request_id = validate_request_id(getattr(request.state, "callback_header_request_id", None))
    except CallbackError as exc:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效") from exc
    if body_request_id is not None and header_request_id is not None and body_request_id != header_request_id:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效")
    request_id: str | None = body_request_id or header_request_id
    input_digest = binding_input_digest(
        callback_binding.task_id,
        callback_binding.scope_id,
        callback_binding.claim_generation,
        callback_binding.attempt,
        "analysis.visual_search",
        callback_binding.target_sha256,
        payload.top_k,
        payload.exclude_self,
    )
    if request_id is None:
        request_id = canonical_callback_request_id(input_digest)
    try:
        callback_scope = ScopeContext(callback_binding.scope_id)
        resources = database(request)
        with resources.environment(callback_scope) as environment:
            task = environment.tasks.get(payload.task_id)
            validate_binding_task(callback_binding, task, callback_registration)
            fact = environment.callback_requests.create(
                request_id=request_id,
                task_id=callback_binding.task_id,
                claim_generation=callback_binding.claim_generation,
                attempt=callback_binding.attempt,
                operation="analysis.visual_search",
                target_sha256=callback_binding.target_sha256,
                input_digest=input_digest,
            )
            # 兼容旧测试资源没有 request_id 字段的情况；空值或非字符串仍拒绝。
            fact_request_id = getattr(fact, "request_id", request_id)
            if not isinstance(fact_request_id, str) or not fact_request_id:
                raise CallbackError("agent_callback_invalid_execution")
            request_id = fact_request_id
            if fact.completed_at is not None and fact.state == "completed" and isinstance(fact.result, dict):
                return dict(fact.result)
            # 先提交 started 事实，再调用独立 service；进程退出时保留可恢复证据。
            environment.uow.session.commit()
        services = scope_services(request, callback_scope)
        service = services.visual_search
    except (CallbackError, ScopeResolutionError, DatabaseError, ValueError, RuntimeError) as exc:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效") from exc
    try:
        result = service.match(task_id=payload.task_id, top_k=payload.top_k, exclude_self=payload.exclude_self)
    except VisualSearchError as exc:
        with database(request).environment(callback_scope) as environment:
            environment.callback_requests.finish(request_id, state="failed", error={"error": exc.code})
        raise error(exc.status_code, exc.code, str(exc)) from exc
    except DatabaseError as exc:
        with database(request).environment(callback_scope) as environment:
            environment.callback_requests.finish(request_id, state="failed", error={"error": exc.code})
        status = 409 if exc.code in {"query_embedding_not_ready", "visual_model_identity_mismatch"} else 404 if exc.code in {"meme_not_found", "task_not_found"} else 503
        raise error(status, exc.code, "视觉匹配无法完成") from exc
    with database(request).environment(callback_scope) as environment:
        environment.callback_requests.finish(request_id, state="completed", result=result)
    return result


__all__ = ["VisualMatchRequest", "internal_visual_search_match"]
