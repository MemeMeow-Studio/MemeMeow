"""公共 HTTP 应用的资源生命周期编排。

该模块把 Settings、数据库、scope service factory、后台 Worker、OpenCode、视觉
客户端、callback、workspace 以及关闭顺序放在同一 ownership 边界内。它只依赖
``backend`` canonical 模块和 FastAPI ``app.state``，不得反向导入 ``api``，任务
领域的具体 handler 通过显式回调注入。
"""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Callable, Mapping, Sequence

from fastapi import FastAPI

from backend.app_extensions import ApplicationExtension
from backend.callbacks import (
    CallbackError,
    DEFAULT_CALLBACK_REGISTRY,
    HMACCallbackCredentials,
)
from backend.config import Settings
from backend.config_http import STORAGE_PREFLIGHT_BLOCKING_KEYS
from backend.database import DatabaseError, DatabaseResources, ScopeContext, check_database, create_engine_for_settings
from backend.opencode import OpenCodeRunner
from backend.opencode_activity import OpenCodeActivityReader
from backend.opencode_workspace import LocalWorkspaceProvider, MissingWorkspaceProvider
from backend.operation_policy import (
    AllowAllOperationPolicy,
    GrantAssociationStore,
    OperationPolicyGateway,
    PersistentGrantAssociationStore,
    UnavailableOperationPolicy,
)
from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService, PostgresTaskWorkerManager
from backend.paths import PathResolver
from backend.reverse_image import ReverseImageService
from backend.scope import LocalScopeResolver, ScopeServiceFactory, ScopeServices
from backend.visual import VisualInferenceClient, VisualSearchService


@dataclass
class LifecycleSetup:
    """一次 lifespan 已取得的公共资源及其关闭 ownership。

    ``configured_factory`` 是宿主注入的 factory；``custom_factory`` 用于区分
    是否可以在退出时删除本轮默认 factory。其余字段只在对应阶段成功后写入，
    便于启动异常时准确收束已创建资源。
    """

    app: FastAPI
    settings: Settings
    local_mode: bool
    configured_factory: Any | None
    custom_factory: bool
    configured_agent_input_provider: Callable[..., Any] | None
    engine: Any
    shared_worker_executor: ThreadPoolExecutor | None = None
    worker_manager: PostgresTaskWorkerManager | None = None
    opencode: OpenCodeRunner | None = None
    factory: Any | None = None


@dataclass
class ScopeRuntime:
    """scope services、factory 和后台 Worker 的统一 ownership 记录。"""

    factory: Any
    shared_worker_executor: ThreadPoolExecutor | None
    worker_manager: PostgresTaskWorkerManager | None
    local_services: ScopeServices | None
    tasks: Any | None


def extension_list(app: FastAPI) -> tuple[ApplicationExtension, ...]:
    """读取应用创建时冻结的扩展列表；缺失时返回空元组。"""
    value = getattr(app.state, "extensions", ())
    return tuple(value) if isinstance(value, (tuple, list)) else ()


async def invoke_extension_hook(extension: ApplicationExtension, name: str, *args: Any) -> Any:
    """调用可选扩展 hook，并兼容同步和异步实现。"""
    hook = getattr(extension, name, None)
    if not callable(hook):
        return None
    result = hook(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def callback_verification_keys(settings: Settings) -> dict[str, str] | None:
    """解析可选的 ``kid=secret`` 轮换验证窗口，格式错误时整体拒绝。"""
    raw = getattr(settings, "agent_callback_verification_keys", None)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise CallbackError("agent_callback_unavailable")
    values: dict[str, str] = {}
    for item in raw.split(","):
        key_id, separator, secret = item.partition("=")
        if not separator or not key_id.strip() or not secret.strip() or key_id.strip() in values:
            raise CallbackError("agent_callback_unavailable")
        values[key_id.strip()] = secret.strip()
    return values


def prepare_lifecycle(
    app: FastAPI,
    *,
    skill_root: Path | None = None,
    settings_loader: Callable[[], Settings] | None = None,
    engine_factory: Callable[[Any], Any] | None = None,
    database_checker: Callable[..., Any] | None = None,
    database_resources_factory: Callable[..., DatabaseResources] | None = None,
    opencode_factory: Callable[[Settings], OpenCodeRunner] | None = None,
    activity_factory: Callable[[Path], Any] | None = None,
    visual_factory: Callable[[Settings], Any] | None = None,
) -> LifecycleSetup:
    """按 Settings、策略、凭据、数据库和客户端阶段初始化公共资源。

    该函数只在 lifespan 内调用；resolver、policy、callback 和 workspace 均从
    app state/宿主 factory 读取，不接受客户端字段。返回的 setup 记录负责后续
    scope factory/Worker 构造和失败关闭。
    """
    managed_factory = bool(getattr(app.state, "_scope_factory_managed", False))
    configured_factory = None if managed_factory else getattr(app.state, "service_factory", None)
    custom_factory = configured_factory is not None
    configured_resolver = getattr(app.state, "scope_resolver", None)
    local_mode = isinstance(configured_resolver, LocalScopeResolver) and configured_resolver.scope.scope_id == "local"
    settings = (settings_loader or Settings.from_env)()
    configured_policy = getattr(app.state, "operation_policy", None)
    if configured_policy is None and configured_factory is not None:
        configured_policy = getattr(configured_factory, "operation_policy", None)
    if configured_policy is None:
        configured_policy = AllowAllOperationPolicy() if local_mode else UnavailableOperationPolicy()
    if not all(callable(getattr(configured_policy, name, None)) for name in ("probe", "acquire", "commit", "release")):
        raise DatabaseError("operation_policy_unavailable")
    app.state.operation_policy = configured_policy
    app.state.operation_policy_gateway = OperationPolicyGateway(configured_policy, allow_all=isinstance(configured_policy, AllowAllOperationPolicy))
    app.state.operation_grants = GrantAssociationStore()
    callback_verifier = getattr(app.state, "callback_verifier", None)
    callback_issuer = getattr(app.state, "callback_issuer", None)
    if callback_verifier is None and configured_factory is not None:
        callback_verifier = getattr(configured_factory, "callback_verifier", None)
    if callback_issuer is None and configured_factory is not None:
        callback_issuer = getattr(configured_factory, "callback_issuer", None)
    if local_mode and callback_verifier is None and callback_issuer is None:
        try:
            # callback 根 secret 必须由部署显式提供；随机凭据会让重启后的验证边界不可预测。
            callback_credentials = HMACCallbackCredentials(
                getattr(settings, "agent_callback_secret", None),
                verification_keys=callback_verification_keys(settings),
            )
        except CallbackError:
            callback_credentials = None
        if callback_credentials is not None:
            callback_verifier = callback_credentials
            callback_issuer = callback_credentials
    app.state.callback_verifier = callback_verifier
    app.state.callback_issuer = callback_issuer
    app.state.callback_registry = getattr(app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
    configured_agent_input_provider = getattr(app.state, "agent_input_provider", None)
    if configured_agent_input_provider is None and configured_factory is not None:
        configured_agent_input_provider = getattr(configured_factory, "agent_input_provider", None)
    settings.ensure_directories()
    configured_workspace_provider = getattr(app.state, "workspace_provider", None)
    if configured_workspace_provider is None and configured_factory is not None:
        configured_workspace_provider = getattr(configured_factory, "workspace_provider", None)
    if local_mode:
        if configured_workspace_provider is None:
            configured_workspace_provider = LocalWorkspaceProvider(
                settings.opencode_runtime_root,
                image_root=settings.image_root,
                skill_root=skill_root or Path(__file__).resolve().parents[1] / "skills" / "research-meme-context",
            )
    elif configured_workspace_provider is None:
        # non-local 允许完成无业务副作用的装配；真实任务执行时由占位 provider 拒绝。
        configured_workspace_provider = MissingWorkspaceProvider()
    app.state.workspace_provider = configured_workspace_provider
    engine = (engine_factory or create_engine_for_settings)(settings)
    expected_revision = getattr(app.state, "expected_schema_revision", settings.expected_database_revision)
    # 启动门禁拒绝任何可能回退到旧 JSON 的业务请求。
    (database_checker or check_database)(engine, expected_revision=expected_revision, require_local_installation=local_mode)
    app.state.settings = settings
    app.state.database = (database_resources_factory or DatabaseResources)(
        engine,
        image_root=settings.image_root,
        data_root=settings.data_root,
        settings=settings,
        require_local_scope=local_mode,
    )
    # grant 关联以 PostgreSQL 为跨进程事实来源，内存层只做同进程热点缓存。
    app.state.operation_grants = PersistentGrantAssociationStore(app.state.database)
    if local_mode:
        app.state.resolver = PathResolver(settings.image_root)
        preflight = app.state.database.flat_preflight(ScopeContext("local"))
        app.state.storage_preflight = preflight
        if any(preflight.get(key) for key in STORAGE_PREFLIGHT_BLOCKING_KEYS):
            raise DatabaseError("flat_meme_storage_preflight_failed")
    else:
        # 宿主由自己的 resolver/factory 管理 scope，不探测或创建 local namespace。
        app.state.storage_preflight = None
    app.state.opencode = (opencode_factory or OpenCodeRunner)(settings)
    app.state.opencode.workspace_provider = configured_workspace_provider
    try:
        app.state.agent_activity = (activity_factory or OpenCodeActivityReader)(settings.opencode_runtime_root)
    except Exception:  # noqa: BLE001 - 可选观测配置异常不能阻止任务服务启动
        app.state.agent_activity = None
    # 视觉模型本体位于独立 CPU 容器，后端只保存推理客户端。
    app.state.visual_inference = (visual_factory or VisualInferenceClient)(settings)
    return LifecycleSetup(
        app=app,
        settings=settings,
        local_mode=local_mode,
        configured_factory=configured_factory,
        custom_factory=custom_factory,
        configured_agent_input_provider=configured_agent_input_provider,
        engine=engine,
        opencode=app.state.opencode,
    )


def build_scope_runtime(
    setup: LifecycleSetup,
    *,
    register_handlers: Callable[..., Any],
    start_services: Callable[[ScopeServices], Any],
    task_handlers: Mapping[str, Callable[..., Any]],
    worker_manager_factory: Callable[..., PostgresTaskWorkerManager] | None = None,
    metadata_factory: Callable[..., Any] | None = None,
    search_factory: Callable[..., Any] | None = None,
    task_service_factory: Callable[..., Any] | None = None,
    reverse_image_factory: Callable[..., Any] | None = None,
    visual_search_factory: Callable[..., Any] | None = None,
) -> ScopeRuntime:
    """创建 scope service factory、进程级 Worker 和 local 图片 Worker。

    ``register_handlers`` 和 ``start_services`` 是任务 handler 的显式接线点，避免
    canonical 生命周期模块反向依赖 HTTP 入口私有函数。
    """
    app = setup.app
    settings = setup.settings
    factory: Any | None = setup.configured_factory
    shared_worker_executor: ThreadPoolExecutor | None = None
    worker_manager: PostgresTaskWorkerManager | None = None
    local_services: ScopeServices | None = None
    if factory is not None:
        required_methods = ("for_scope", "for_task", "start_all", "shutdown")
        if any(not callable(getattr(factory, name, None)) for name in required_methods):
            raise DatabaseError("scope_service_factory_invalid")
    else:
        shared_worker_executor = ThreadPoolExecutor(
            max_workers=max(2, settings.opencode_concurrency + 1),
            thread_name_prefix="mememeow-scope-worker",
        )
        worker_manager = (worker_manager_factory or PostgresTaskWorkerManager)(
            app.state.database,
            agent_concurrency=settings.opencode_concurrency,
            scope_concurrency=getattr(settings, "agent_scope_concurrency", 1),
            agent_backpressure=settings.agent_backpressure,
            settings_version=settings.settings_version,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            executor=shared_worker_executor,
        )
        if setup.local_mode:
            local_scope = ScopeContext("local")
            local_metadata = (metadata_factory or PostgresMetadataService)(app.state.database, scope_id=local_scope)
            local_search = (search_factory or PostgresSearchService)(settings, app.state.database, local_metadata, scope_id=local_scope)
            try:
                local_reverse_image = (reverse_image_factory or ReverseImageService)(
                    settings,
                    app.state.database,
                    scope_id=local_scope,
                    operation_policy=app.state.operation_policy_gateway,
                    grant_store=app.state.operation_grants,
                )
            except TypeError as exc:
                if "operation_policy" not in str(exc) and "grant_store" not in str(exc):
                    raise
                # 兼容尚未升级的轻量 facade 夹具；真实服务支持 policy 参数。
                local_reverse_image = (reverse_image_factory or ReverseImageService)(settings, app.state.database, scope_id=local_scope)
            local_visual_search = (visual_search_factory or VisualSearchService)(settings, app.state.database, scope_id=local_scope)
            local_tasks = (task_service_factory or PostgresTaskService)(
                app.state.database,
                scope_id=local_scope,
                agent_concurrency=settings.opencode_concurrency,
                scope_concurrency=getattr(settings, "agent_scope_concurrency", 1),
                agent_backpressure=settings.agent_backpressure,
                settings_version=settings.settings_version,
                lease_seconds=settings.worker_lease_seconds,
                max_attempts=settings.worker_max_attempts,
                resume_enabled=bool(getattr(settings, "agent_resume_enabled", False)),
                resume_max_attempts=int(getattr(settings, "agent_resume_max_attempts", 2)),
                resume_backoff_seconds=int(getattr(settings, "agent_resume_backoff_seconds", 2)),
                resume_max_backoff_seconds=int(getattr(settings, "agent_resume_max_backoff_seconds", 60)),
                resume_timeout_seconds=int(getattr(settings, "agent_resume_timeout_seconds", 900)),
                worker_manager=worker_manager,
                operation_policy=app.state.operation_policy_gateway,
                grant_store=app.state.operation_grants,
            )
            local_services = ScopeServices(
                local_scope,
                local_metadata,
                local_search,
                local_tasks,
                local_reverse_image,
                local_visual_search,
            )
    runtime_factory: Any
    if factory is None:
        # 回调本身只由公共 scope factory 调用，不保存 HTTP 入口模块引用。
        def start_scope_services(services: ScopeServices) -> Any:
            return start_services(services)

        runtime_factory = ScopeServiceFactory(
            app.state.database,
            settings,
            task_config={
                "agent_concurrency": settings.opencode_concurrency,
                "scope_concurrency": getattr(settings, "agent_scope_concurrency", 1),
                "agent_backpressure": settings.agent_backpressure,
                "settings_version": settings.settings_version,
                "lease_seconds": settings.worker_lease_seconds,
                "max_attempts": settings.worker_max_attempts,
                "resume_enabled": bool(getattr(settings, "agent_resume_enabled", False)),
                "resume_max_attempts": int(getattr(settings, "agent_resume_max_attempts", 2)),
                "resume_backoff_seconds": int(getattr(settings, "agent_resume_backoff_seconds", 2)),
                "resume_max_backoff_seconds": int(getattr(settings, "agent_resume_max_backoff_seconds", 60)),
                "resume_timeout_seconds": int(getattr(settings, "agent_resume_timeout_seconds", 900)),
                "executor": shared_worker_executor,
                "operation_policy": app.state.operation_policy_gateway,
                "grant_store": app.state.operation_grants,
                "register_handlers": register_handlers,
                "start_services": start_scope_services,
            },
            preloaded={"local": local_services} if local_services is not None else None,
            worker_manager=worker_manager,
        )
    else:
        runtime_factory = factory
    if worker_manager is not None:
        # 非 local 默认 factory 不预加载 scope；先注册全局 handler 再恢复队列。
        register_handlers(manager=worker_manager)
    if local_services is not None:
        app.state.metadata = local_services.metadata
        app.state.search_engine = local_services.search
        app.state.reverse_image = local_services.reverse_image
        app.state.visual_search = local_services.visual_search
        register_handlers(local_services)
        local_services.metadata.recover_storage(limit=500)
    app.state.service_factory = runtime_factory
    app.state._scope_factory_managed = setup.configured_factory is None
    app.state.agent_input_provider = setup.configured_agent_input_provider
    app.state.image_processing_task_handlers = dict(task_handlers)
    app.state.image_processing_workers = {}
    app.state.image_processing_workers_lock = RLock()
    if local_services is not None:
        from backend.image_processing import ImageProcessingWorker
        from backend.config import validate_agent_concurrency

        app.state.image_processing_workers[local_services.scope.scope_id] = ImageProcessingWorker(
            app.state.database,
            scope_id=local_services.scope,
            task_service=local_services.tasks,
            policy=app.state.operation_policy_gateway,
            grant_store=app.state.operation_grants,
            max_workers=validate_agent_concurrency(
                getattr(settings, "opencode_concurrency", 1),
                backpressure=getattr(settings, "agent_backpressure", None),
            ),
            task_handlers=dict(task_handlers),
        )
        if callable(getattr(app.state.database, "factory", None)):
            app.state.image_processing_workers[local_services.scope.scope_id].start()
    tasks = local_services.tasks if local_services is not None else None
    if tasks is not None:
        app.state.tasks = tasks
    app.state.task_scope_diagnostics = runtime_factory.start_all()
    setup.factory = runtime_factory
    setup.worker_manager = worker_manager
    setup.shared_worker_executor = shared_worker_executor
    return ScopeRuntime(runtime_factory, shared_worker_executor, worker_manager, local_services, tasks)


async def start_extensions(app: FastAPI) -> list[ApplicationExtension]:
    """在公共数据库、factory 和 Worker 就绪后按顺序启动扩展。"""
    started: list[ApplicationExtension] = []
    for extension in extension_list(app):
        await invoke_extension_hook(extension, "on_startup", app)
        started.append(extension)
    return started


async def shutdown_lifecycle(setup: LifecycleSetup | None, runtime: ScopeRuntime | None, started_extensions: Sequence[ApplicationExtension] = ()) -> None:
    """按依赖顺序关闭扩展、后台 Worker、线程池和数据库 Engine。"""
    if setup is None:
        return
    app = setup.app
    for extension in reversed(tuple(started_extensions)):
        await invoke_extension_hook(extension, "on_shutdown", app)
    opencode = getattr(app.state, "opencode", None)
    if callable(getattr(opencode, "shutdown", None)):
        opencode.shutdown()
    for image_worker in list(getattr(app.state, "image_processing_workers", {}).values()):
        image_worker.shutdown()
    if runtime is not None:
        runtime.factory.shutdown()
        if runtime.worker_manager is not None:
            runtime.worker_manager.shutdown()
        if not setup.custom_factory and getattr(app.state, "service_factory", None) is runtime.factory:
            delattr(app.state, "service_factory")
        if runtime.shared_worker_executor is not None:
            runtime.shared_worker_executor.shutdown(wait=True, cancel_futures=True)
    if setup.engine is not None:
        setup.engine.dispose()


__all__ = [
    "LifecycleSetup",
    "ScopeRuntime",
    "build_scope_runtime",
    "callback_verification_keys",
    "extension_list",
    "invoke_extension_hook",
    "prepare_lifecycle",
    "shutdown_lifecycle",
    "start_extensions",
]
