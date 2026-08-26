"""公共 HTTP 系统边界。

本模块位于 FastAPI 路由模板和业务服务之间，集中承载系统状态接口、请求 scope
middleware、访问保护和稳定错误投影。宿主入口只负责注册路由并装配生命周期依赖；
本模块不得反向导入 ``api.py`` 或具体 Server 入口。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from backend.app_extensions import ApplicationExtension, extension_paths, path_is_exempt
from backend.callbacks import (
    CallbackBinding,
    CallbackError,
    CallbackRegistry,
    DEFAULT_CALLBACK_REGISTRY,
    install_body_guard,
    log_callback_rejection,
    validate_callback_headers,
    verify_content_length,
)
from backend.config_http import _storage_preflight_summary
from backend.database import DatabaseError
from backend.rate_limiter import RateLimiter
from backend.scope import ScopeResolutionError, resolve_scope_async, validate_scope_services


INTERNAL_SCOPE_CALLBACK_PATHS = frozenset({"/internal/reverse-image/search", "/internal/visual-search/match"})
SCOPE_SELECTOR_FIELDS = frozenset({"scope_id", "scope-id", "user_id", "user-id"})
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def error(status: int, code: str, message: str) -> HTTPException:
    """构造统一 HTTP 错误。

    输入是稳定 HTTP 状态、错误码和公开消息；输出只包含 `{error, message}` detail。
    调用场景是系统、scope 和内部 callback 边界需要投影错误时，禁止直接暴露底层诊断。
    """
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def extension_list(app: FastAPI) -> tuple[ApplicationExtension, ...]:
    """读取应用创建时冻结的扩展列表。

    输入是已装配的 FastAPI 应用；输出为空元组或不可变扩展序列。调用场景是每个请求
    的 scope/授权 middleware，缺失状态时 fail-closed 到无扩展而不制造隐式依赖。
    """
    value = getattr(app.state, "extensions", ())
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def extension_scope_exempt(app: FastAPI, path: str) -> bool:
    """判断路径是否命中宿主声明的 scope 豁免。

    输入是应用和请求路径；输出表示是否只跳过业务 scope 解析。调用场景是 callback
    之外的健康、认证等宿主路径，扩展授权 hook 仍由 middleware 单独执行。
    """
    paths: list[str] = []
    for extension in extension_list(app):
        paths.extend(extension_paths(extension))
    return path_is_exempt(path, paths)


def scope_field_name(value: str) -> bool:
    """判断字段名是否是客户端提交的范围选择器。

    输入是 query/header 字段名；输出表示是否命中固定 scope selector 集合。调用场景是
    body 解析前的 request middleware，避免从 multipart 或 JSON body 猜测授权范围。
    """
    lowered = value.strip().lower()
    if lowered in SCOPE_SELECTOR_FIELDS:
        return True
    return lowered.startswith("x-") and lowered[2:] in SCOPE_SELECTOR_FIELDS


def request_declares_scope(request: Request) -> bool:
    """检查 query/header 是否携带客户端 scope selector。

    输入是当前 HTTP 请求；输出表示是否应在读取 body 前拒绝请求。调用场景是普通业务
    scope middleware，仅读取 query/header，不触碰请求体。
    """
    query_params = getattr(request, "query_params", {})
    headers = getattr(request, "headers", {})
    return any(scope_field_name(str(key)) for key in query_params.keys()) or any(scope_field_name(str(key)) for key in headers.keys())


async def invoke_extension_hook(extension: ApplicationExtension, name: str, *args: Any) -> Any:
    """调用同步或异步扩展 hook。

    输入是扩展、hook 名称和其参数；输出是 hook 返回值或异步结果。调用场景是请求
    授权 middleware，缺失 hook 被视为空操作而不改变已有最小扩展协议。
    """
    hook = getattr(extension, name, None)
    if not callable(hook):
        return None
    result = hook(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def authenticate_callback_request(request: Request) -> None:
    """在 ASGI body 读取前验证内部 callback 凭据和 body guard。

    输入是带 callback path 的真实请求；输出为 None，并把绑定、request id 和 body guard
    写入 request state。调用场景是内部 callback middleware，任何 registry、token、签名、
    content-length 或 header binding 异常都 fail-closed。
    """
    registry: CallbackRegistry | None = getattr(request.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
    registration = registry.get(request.url.path) if registry is not None else None
    verifier = getattr(request.app.state, "callback_verifier", None)
    if registration is None or verifier is None or not callable(getattr(verifier, "verify", None)):
        raise CallbackError("agent_callback_unavailable")
    token = request.headers.get("x-mememeow-callback") or request.headers.get("x-mememeow-callback-token")
    if not token:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not token:
        raise CallbackError()
    try:
        verify = verifier.verify
        try:
            parameters = inspect.signature(verify).parameters
        except (TypeError, ValueError) as exc:
            raise CallbackError("agent_callback_unavailable") from exc
        accepts_path = "path" in parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        # 只有签名明确支持 path 时才走兼容分支；verifier 内部 TypeError 必须按故障处理。
        binding = verify(token, path=request.url.path) if accepts_path else verify(token)
    except CallbackError:
        raise
    except Exception as exc:  # noqa: BLE001 - verifier 故障必须 fail-closed
        raise CallbackError("agent_callback_unavailable") from exc
    if not isinstance(binding, CallbackBinding) or not binding.allows_any(registration.operations):
        raise CallbackError("agent_callback_invalid_execution")
    verify_content_length(request.headers, limit=registration.max_body_bytes)
    request.state.callback_header_request_id = validate_callback_headers(request.headers, binding)
    request.state.callback_binding = binding
    install_body_guard(request, limit=registration.max_body_bytes)


async def bind_request_scope(request: Request, call_next: Any) -> Any:
    """在数据库、文件或业务服务访问前解析并冻结可信 request scope。

    输入是当前 request 和下一个 ASGI handler；输出是下游响应或稳定拒绝响应。调用场景
    是公共 API middleware：内部 callback 走独立 token/task 控制面，普通请求走可信 resolver，
    scope service 缺失或授权失败时不进入业务 handler。
    """
    # Agent callback 没有用户 request scope；其 task scope 由 callback 领域 handler 恢复。
    if request.url.path in INTERNAL_SCOPE_CALLBACK_PATHS:
        # 轻量领域测试 stub 没有 ASGI headers；真实 Request 必须先完成 callback 身份验证。
        if hasattr(request, "headers"):
            try:
                authenticate_callback_request(request)
            except CallbackError as exc:
                log_callback_rejection(request.url.path, exc, binding=getattr(request.state, "callback_binding", None))
                status = 413 if exc.code == "agent_callback_body_too_large" else 503 if exc.code == "agent_callback_unavailable" else 401
                return JSONResponse(status_code=status, content={"error": exc.code, "message": "内部执行凭据无效"})
        factory = getattr(request.app.state, "service_factory", None)
        if not callable(getattr(factory, "for_task", None)):
            return JSONResponse(status_code=503, content={"error": "scope_unavailable", "message": "请求 scope 当前不可用"})
        try:
            return await call_next(request)
        except CallbackError as exc:
            log_callback_rejection(request.url.path, exc, binding=getattr(request.state, "callback_binding", None))
            status = 413 if exc.code == "agent_callback_body_too_large" else 401
            return JSONResponse(status_code=status, content={"error": exc.code, "message": "内部执行凭据无效"})
    if request_declares_scope(request):
        return JSONResponse(status_code=400, content={"error": "scope_selector_forbidden", "message": "请求不得提交范围选择字段"})
    if extension_scope_exempt(request.app, request.url.path):
        try:
            for extension in extension_list(request.app):
                await invoke_extension_hook(extension, "authorize_exempt_request", request)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": "request_forbidden", "message": "请求未获授权"}
            return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
        except Exception as exc:  # noqa: BLE001 - 宿主身份失败必须进入稳定可用性边界
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
            if status in {401, 403, 429, 503} and isinstance(code, str) and code:
                message = "需要有效会话" if status == 401 else "请求未获授权" if status in {403, 429} else "请求授权服务当前不可用"
                return JSONResponse(status_code=status, content={"error": code, "message": message})
            if not isinstance(exc, (DatabaseError, ValueError, RuntimeError)):
                raise
            return JSONResponse(status_code=503, content={"error": "request_authorization_unavailable", "message": "请求授权服务当前不可用"})
        return await call_next(request)
    try:
        scope = await resolve_scope_async(request)
        factory = getattr(request.app.state, "service_factory", None)
        if factory is None or not callable(getattr(factory, "for_scope", None)):
            raise ScopeResolutionError("应用未配置 scope service factory")
        services = validate_scope_services(scope, factory.for_scope(scope))
        for extension in extension_list(request.app):
            await invoke_extension_hook(extension, "authorize_request", request, scope, services)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": "request_forbidden", "message": "请求未获授权"}
        return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
    except ScopeResolutionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": "请求 scope 无法解析"})
    except Exception as exc:  # noqa: BLE001 - 适配器只允许声明稳定身份/可用性边界
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        if status in {401, 503} and isinstance(code, str) and code:
            message = "需要有效会话" if status == 401 else "请求 scope 当前不可用"
            return JSONResponse(status_code=status, content={"error": code, "message": message})
        if not isinstance(exc, (DatabaseError, ValueError, RuntimeError)):
            raise
        code = code if isinstance(code, str) and code else "scope_unavailable"
        return JSONResponse(status_code=503, content={"error": code, "message": "请求 scope 当前不可用"})
    request.state.scope = scope
    request.state.services = services
    return await call_next(request)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """把 FastAPI 校验异常投影为稳定业务错误。

    输入是请求和 FastAPI 校验异常；输出是统一 JSON response。调用场景是应用异常
    handler，合集路径保留 422，其它路径保持 400。
    """
    status = 422 if request.url.path == "/collections" or request.url.path.startswith("/collections/") else 400
    code = "invalid_collection_request" if status == 422 else "invalid_request"
    return JSONResponse(status_code=status, content={"error": code, "message": "请求参数校验失败"})


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """把 HTTPException 投影为 `{error, message}` 响应。

    输入是请求和异常；输出保留 status/header 以及已有公开 detail。调用场景是公共
    应用异常处理，非结构化 detail 不向客户端暴露内部对象表示。
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = exc.detail
    else:
        content = {"error": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


async def access_policy(request: Request, call_next: Any) -> Any:
    """按环境配置执行保护模式白名单。

    输入是请求和下游 handler；输出是下游响应或 403。调用场景是系统 middleware，路径
    只按显式白名单匹配，不改变 scope/auth 解析顺序。
    """
    settings = getattr(request.app.state, "settings", None)
    if settings and settings.protected_mode:
        path = request.url.path.rstrip("/") or "/"
        allowed = {p.rstrip("/") or "/" for p in settings.allowed_endpoints}
        if not any(path == p or (p and p != "/" and path.startswith(p + "/")) for p in allowed):
            return JSONResponse(status_code=403, content={"error": "protected", "message": "接口未在保护模式白名单中"})
    return await call_next(request)


async def access_rate_limit(request: Request, call_next: Any) -> Any:
    """按客户端 IP 执行配置的内存限流。

    输入是请求和下游 handler；输出是下游响应或带 Retry-After 的 429。调用场景是
    公共入口 middleware，限流器只保存在当前 app state，不跨应用共享。
    """
    settings = getattr(request.app.state, "settings", None)
    if settings and settings.rate_limit_enabled:
        limiter = getattr(request.app.state, "limiter", None)
        if limiter is None:
            limiter = request.app.state.limiter = RateLimiter()
        client = request.client.host if request.client else "unknown"
        if not limiter.check(client, settings.rate_limit_requests, settings.rate_limit_window):
            return JSONResponse(status_code=429, content={"error": "rate_limited", "message": "请求过于频繁"}, headers={"Retry-After": str(settings.rate_limit_window)})
    return await call_next(request)


async def root(request: Request) -> Any:
    """返回服务基本状态或前端入口文件。

    输入是当前请求；输出是固定状态字典或受控 frontend index 文件。调用场景是根
    系统路由，文件路径固定在仓库 frontend/dist 内，不接受客户端路径。
    """
    if (FRONTEND_DIST / "index.html").is_file() and "text/html" in request.headers.get("accept", ""):
        return FileResponse(FRONTEND_DIST / "index.html", media_type="text/html")
    return {"name": "MemeMeow", "version": request.app.version, "status": "ok"}


async def health(request: Request) -> dict[str, object]:
    """返回容器探活所需的脱敏状态。

    输入是当前请求；输出是固定健康字段和 storage preflight 计数摘要。调用场景是
    `/health`，不返回视觉诊断、绝对路径、文件名或凭据。
    """
    settings = getattr(request.app.state, "settings", None)
    visual_client = getattr(request.app.state, "visual_inference", None)
    visual_status = visual_client.health() if visual_client is not None else {"available": False}
    return {
        "status": "ok" if getattr(request.app.state, "service_factory", None) is not None else "degraded",
        "visual_available": bool(visual_status.get("available")),
        "agent_resume_enabled": bool(getattr(settings, "agent_resume_enabled", False)),
        "storage_preflight": _storage_preflight_summary(getattr(request.app.state, "storage_preflight", None)),
    }


__all__ = [
    "FRONTEND_DIST",
    "INTERNAL_SCOPE_CALLBACK_PATHS",
    "SCOPE_SELECTOR_FIELDS",
    "access_policy",
    "access_rate_limit",
    "authenticate_callback_request",
    "bind_request_scope",
    "error",
    "extension_list",
    "extension_scope_exempt",
    "health",
    "http_error_handler",
    "invoke_extension_hook",
    "request_declares_scope",
    "root",
    "scope_field_name",
    "validation_error_handler",
]
