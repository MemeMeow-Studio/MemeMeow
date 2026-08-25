"""公共核心内部反向图片 callback HTTP 边界。

本模块负责 multipart callback 已读内容的绑定、目标图片校验、scope service 装配和错误
投影；反向图片缓存/provider、数据库 schema、路由注册和 middleware 由入口通过 callback
注入，不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from backend.callbacks import (
    CallbackError,
    CallbackBinding,
    validate_binding_task,
    validate_input_digest,
    validate_request_binding,
    validate_request_id,
)
from backend.database import DatabaseError, ScopeContext
from backend.reverse_image import ReverseImageError, ReverseImageRequest, derive_controlled_crop
from backend.scope import ScopeResolutionError


BindingProvider = Callable[[Request], Any]
RegistrationProvider = Callable[[Request], Any]
DatabaseProvider = Callable[[Request], Any]
ScopeServicesProvider = Callable[[Request, ScopeContext], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]


async def internal_reverse_image_search(
    request: Request,
    *,
    task_id: str,
    content: bytes,
    filename: str | None,
    request_id: str | None,
    input_digest: str | None,
    search_type: str,
    language: str,
    country: str | None,
    query: str | None,
    auto_crop: bool,
    refresh: bool,
    binding: BindingProvider,
    registration: RegistrationProvider,
    database: DatabaseProvider,
    scope_services: ScopeServicesProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """验证 Agent claim 后执行一次供应商无关的反向图片检索。

    输入是入口读取的图片字节、原始文件名和检索表单字段；输出是脱敏后的 reverse-image
    service 结果。调用场景是内部 Agent callback，任何绑定、目标版本、scope 或请求摘要
    异常都会在 provider/service 调用前 fail-closed。
    """
    callback_binding = binding(request)
    callback_registration = registration(request)
    if callback_registration is None or len(content) > callback_registration.max_body_bytes:
        raise error(413, "agent_callback_body_too_large", "内部请求体超过限制")
    if not isinstance(callback_binding, CallbackBinding) or callback_binding.task_id != task_id:
        raise error(401, "agent_callback_unauthorized", "内部执行凭据无效")

    try:
        # 先在 token 声明的 scope 内复核持久 Task 和目标 Meme，再装配 provider，
        # 避免旧 claim 或跨 scope 标识在安全事实确认前触发外部副作用。
        callback_scope = ScopeContext(callback_binding.scope_id)
        resources = database(request)
        with resources.environment(callback_scope) as environment:
            task = environment.tasks.get(task_id)
            validate_binding_task(callback_binding, task, callback_registration)
            target_meme = (task.payload or {}).get("meme_id") if task is not None else None
            target_record = environment.memes.get(target_meme) if isinstance(target_meme, str) else None
            if target_record is None:
                raise CallbackError("agent_callback_invalid_execution")

            # 只有先证明上传的是任务目标整图，才允许服务端执行确定性中心裁剪；
            # Agent 不能借 auto_crop 或自报 SHA 替换逻辑目标。
            source_sha256 = hashlib.sha256(content).hexdigest()
            if source_sha256 != target_record.sha256:
                raise CallbackError("agent_callback_invalid_execution")
            if auto_crop:
                content, _derived_sha256 = derive_controlled_crop(content, filename=filename or "image.png")

        services = scope_services(request, callback_scope)
        service = services.reverse_image
    except (CallbackError, ScopeResolutionError, DatabaseError, ValueError, RuntimeError) as exc:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效") from exc

    header_request_id = getattr(request.state, "callback_header_request_id", None)
    if request_id is not None and header_request_id is not None and request_id != header_request_id:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效")
    try:
        request_id = validate_request_id(request_id or header_request_id)
        input_digest = validate_input_digest(input_digest)
        request_id, input_digest = validate_request_binding(request_id, callback_binding, input_digest=input_digest)
    except CallbackError as exc:
        raise error(401, "agent_callback_invalid_execution", "内部执行绑定无效") from exc

    try:
        return service.search(
            ReverseImageRequest(
                image=content,
                filename=filename or "image",
                task_id=task_id,
                request_id=request_id,
                search_type=search_type,
                language=language,
                country=country,
                query=query,
                auto_crop=auto_crop,
                refresh=refresh,
                source_image_sha256=target_record.sha256,
                callback_binding=callback_binding,
                input_digest=input_digest,
            )
        )
    except ReverseImageError as exc:
        raise error(exc.status_code, exc.code, str(exc)) from exc
    except DatabaseError as exc:
        status = 404 if exc.code == "meme_not_found" else 409 if exc.code in {"usage_request_conflict", "usage_event_conflict", "callback_request_conflict", "callback_binding_conflict"} else 503
        raise error(status, exc.code, "反向图片请求无法完成") from exc


__all__ = ["internal_reverse_image_search"]
