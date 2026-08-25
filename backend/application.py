"""公共 FastAPI 应用装配边界。

该模块位于 HTTP 路由模板和运行时生命周期之间，只负责创建应用实例、复制
不可变路由元数据、安装异常/middleware 以及绑定宿主提供的 resolver 和扩展。
数据库、Worker、OpenCode 等有副作用的资源只能在 lifespan 中装配。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fastapi import FastAPI

from backend.app_extensions import ApplicationExtension
from backend.database import ScopeContext
from backend.errors import ErrorBody
from backend.operation_policy import AllowAllOperationPolicy, OperationPolicyError
from backend.scope import LocalScopeResolver, ScopeResolutionError, ScopeServiceFactory


def _validate_resolver(scope_resolver: object) -> None:
    """校验应用装配所需的可信 resolver，不在缺失时安装 local fallback。"""
    if scope_resolver is None:
        raise ScopeResolutionError("应用必须显式配置 scope resolver")
    if not any(callable(getattr(scope_resolver, name, None)) for name in ("resolve", "resolve_scope")) and not callable(scope_resolver):
        raise ScopeResolutionError("scope resolver 不可调用")
    if isinstance(scope_resolver, LocalScopeResolver) and scope_resolver.scope.scope_id != "local":
        raise ScopeResolutionError("local resolver 只能绑定 local scope")


def _validate_operation_policy(operation_policy: object) -> None:
    """校验宿主 policy 的四阶段协议，避免启动后才暴露策略旁路。"""
    if operation_policy is not None and not all(callable(getattr(operation_policy, name, None)) for name in ("probe", "acquire", "commit", "release")):
        raise OperationPolicyError("operation_policy_unavailable")


def create_application(
    *,
    route_template: FastAPI,
    lifespan: Callable[[FastAPI], Any],
    scope_resolver: object,
    service_factory: ScopeServiceFactory | None = None,
    operation_policy: object | None = None,
    callback_issuer: object | None = None,
    callback_verifier: object | None = None,
    agent_input_provider: Callable[[ScopeContext, Any], str | Any] | None = None,
    workspace_provider: object | None = None,
    extensions: Sequence[ApplicationExtension] | None = None,
) -> FastAPI:
    """创建一个绑定可信依赖的公共应用。

    参数输入是宿主装配时的不可变依赖；返回值包含与路由模板同顺序的路由和
    middleware，但不会在 import/factory 阶段创建数据库、文件、Worker 或外部
    客户端。真正资源初始化由传入的 lifespan 负责。
    """
    _validate_resolver(scope_resolver)
    _validate_operation_policy(operation_policy)
    configured_extensions = tuple(extensions or ())
    created = FastAPI(
        title=route_template.title,
        version=route_template.version,
        description=route_template.description,
        lifespan=lifespan,
        responses={
            400: {"model": ErrorBody},
            403: {"model": ErrorBody},
            404: {"model": ErrorBody},
            409: {"model": ErrorBody},
            422: {"model": ErrorBody},
            503: {"model": ErrorBody},
        },
    )
    # APIRoute/StaticFiles 只保存不可变路由元数据；复制引用不会共享请求 scope。
    created.router.routes.extend(route_template.router.routes)
    created.exception_handlers.update(route_template.exception_handlers)
    for middleware in reversed(route_template.user_middleware):
        created.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)
    created.state.extensions = configured_extensions
    created.state.expose_scope = True
    for extension in configured_extensions:
        register_routes = getattr(extension, "register_routes", None)
        if callable(register_routes):
            register_routes(created)
    created.state.scope_resolver = scope_resolver
    # policy、callback、workspace 和 resolver 属于不同信任边界，均只从同一个
    # keyword-only 工厂进入应用，不使用模块级可变依赖。
    created.state.operation_policy = operation_policy if operation_policy is not None else (AllowAllOperationPolicy() if isinstance(scope_resolver, LocalScopeResolver) else None)
    created.state.callback_issuer = callback_issuer
    created.state.callback_verifier = callback_verifier
    if service_factory is not None:
        created.state.service_factory = service_factory
        created.state._scope_factory_managed = False
    if agent_input_provider is not None:
        created.state.agent_input_provider = agent_input_provider
    if workspace_provider is not None:
        created.state.workspace_provider = workspace_provider
    return created


__all__ = ["create_application"]
