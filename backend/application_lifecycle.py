"""公共 HTTP 应用的资源生命周期编排。

该模块把 Settings、数据库、scope service factory、后台 Worker、OpenCode、视觉
客户端、callback、workspace 以及关闭顺序放在同一 ownership 边界内。它只依赖
``backend`` canonical 模块和 FastAPI ``app.state``，不得反向导入 ``api``，任务
领域的具体 handler 通过显式回调注入。
"""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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


@dataclass
class _PreparationOwnership:
    """记录 prepare 阶段已创建的外部资源，供中途失败时回收。"""

    engine: Any | None = None
    opencode: Any | None = None


@dataclass
class _ScopeBuildOwnership:
    """记录 scope runtime 构造期间已取得的后台资源。"""

    executor: ThreadPoolExecutor | None = None
    worker_manager: Any | None = None
    local_services: ScopeServices | None = None
    runtime_factory: Any | None = None
    image_workers: list[Any] = field(default_factory=list)
    factory_started: bool = False


def _close_resource(resource: Any | None, method_name: str, errors: list[tuple[str, BaseException]], phase: str) -> None:
    """调用一个资源的关闭方法并记录异常，保证同阶段后续资源仍能继续收束。"""
    method = getattr(resource, method_name, None)
    if not callable(method):
        return
    try:
        method()
    except BaseException as exc:  # noqa: BLE001 - 关闭阶段必须继续收束其它资源
        errors.append((f"{phase}.{method_name}", exc))


def _add_cleanup_notes(primary: BaseException, errors: Sequence[tuple[str, BaseException]]) -> None:
    """把关闭失败的稳定类型和错误码附加到原始异常，不泄露路径或凭据。"""
    for phase, error in errors:
        code = getattr(error, "code", None) or type(error).__name__
        primary.add_note(f"生命周期资源收束失败: {phase} ({code})")


def _clear_lifecycle_state(app: FastAPI, *, managed_factory: bool) -> None:
    """删除本轮已关闭的运行时 state，避免下一轮 lifespan 复用失效资源。"""
    _clear_scope_runtime_state(app, managed_factory=managed_factory)
    for name in (
        "database",
        "settings",
        "resolver",
        "storage_preflight",
        "opencode",
        "agent_activity",
        "visual_inference",
        "operation_policy_gateway",
        "operation_grants",
    ):
        if hasattr(app.state, name):
            delattr(app.state, name)


def _clear_scope_runtime_state(app: FastAPI, *, managed_factory: bool) -> None:
    """只删除 scope runtime 状态，保留 prepare 阶段资源供外层继续关闭。"""
    if managed_factory and hasattr(app.state, "service_factory"):
        delattr(app.state, "service_factory")
    for name in (
        "tasks",
        "metadata",
        "search_engine",
        "reverse_image",
        "visual_search",
        "task_scope_diagnostics",
        "image_processing_task_handlers",
        "image_processing_workers",
        "image_processing_workers_lock",
    ):
        if hasattr(app.state, name):
            delattr(app.state, name)


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
    """按阶段创建公共运行时，并在任何中途失败时释放已取得的资源。"""
    ownership = _PreparationOwnership()
    try:
        return _prepare_lifecycle(
            app,
            skill_root=skill_root,
            settings_loader=settings_loader,
            engine_factory=engine_factory,
            database_checker=database_checker,
            database_resources_factory=database_resources_factory,
            opencode_factory=opencode_factory,
            activity_factory=activity_factory,
            visual_factory=visual_factory,
            ownership=ownership,
        )
    except BaseException as primary:
        errors: list[tuple[str, BaseException]] = []
        _close_resource(ownership.opencode, "shutdown", errors, "prepare.opencode")
        _close_resource(ownership.engine, "dispose", errors, "prepare.engine")
        _clear_lifecycle_state(app, managed_factory=bool(getattr(app.state, "_scope_factory_managed", False)))
        _add_cleanup_notes(primary, errors)
        raise


def _prepare_lifecycle(
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
    ownership: _PreparationOwnership,
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

    def local_candidate_materializer(context: Any, resolved: Any) -> None:
        """调用当前生命周期的 scope-bound 数据资源物化 local 候选。"""
        resources = getattr(app.state, "database", None)
        if resources is None:
            raise RuntimeError("visual_candidate_materialization_failed")
        from backend.visual_candidates import materialize_local_candidates

        materialize_local_candidates(resources, context, resolved)

    if local_mode:
        if configured_workspace_provider is None:
            configured_workspace_provider = LocalWorkspaceProvider(
                settings.opencode_runtime_root,
                image_root=settings.image_root,
                skill_root=skill_root or Path(__file__).resolve().parents[1] / "skills" / "research-meme-context",
                candidate_materializer=local_candidate_materializer,
            )
    elif configured_workspace_provider is None:
        # non-local 允许完成无业务副作用的装配；真实任务执行时由占位 provider 拒绝。
        configured_workspace_provider = MissingWorkspaceProvider()
    app.state.workspace_provider = configured_workspace_provider
    engine = (engine_factory or create_engine_for_settings)(settings)
    ownership.engine = engine
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
    ownership.opencode = app.state.opencode
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


def _cleanup_scope_build(ownership: _ScopeBuildOwnership, setup: LifecycleSetup) -> list[tuple[str, BaseException]]:
    """收束 scope runtime 构造失败时已经创建的 Worker、factory 和线程池。"""
    errors: list[tuple[str, BaseException]] = []
    for image_worker in reversed(ownership.image_workers):
        _close_resource(image_worker, "shutdown", errors, "build.image_worker")
    if ownership.runtime_factory is not None and (not setup.custom_factory or ownership.factory_started):
        _close_resource(ownership.runtime_factory, "shutdown", errors, "build.factory")
    _close_resource(ownership.worker_manager, "shutdown", errors, "build.worker_manager")
    _close_resource(ownership.executor, "shutdown", errors, "build.executor")
    # prepare 阶段的 OpenCode、Database 和 Engine 仍由外层 lifespan 持有，不能
    # 在这里提前删除，否则 shutdown_lifecycle 无法再找到并关闭它们。
    _clear_scope_runtime_state(setup.app, managed_factory=setup.configured_factory is None)
    return errors


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
    """创建 scope runtime；失败时继续释放已取得的后台资源并保留原始错误。"""
    ownership = _ScopeBuildOwnership()
    try:
        return _build_scope_runtime(
            setup,
            register_handlers=register_handlers,
            start_services=start_services,
            task_handlers=task_handlers,
            worker_manager_factory=worker_manager_factory,
            metadata_factory=metadata_factory,
            search_factory=search_factory,
            task_service_factory=task_service_factory,
            reverse_image_factory=reverse_image_factory,
            visual_search_factory=visual_search_factory,
            ownership=ownership,
        )
    except BaseException as primary:
        _add_cleanup_notes(primary, _cleanup_scope_build(ownership, setup))
        raise


def _build_scope_runtime(
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
    ownership: _ScopeBuildOwnership,
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

    def prepare_visual_candidates(claim: Any, payload: Mapping[str, object], snapshot: Mapping[str, object]) -> Any:
        """在任务 grant 前委托 Runner/provider 物化当前 claim 的候选视图。"""
        runner = setup.opencode
        prepare = getattr(runner, "prepare_candidates_for_task", None)
        if not callable(prepare):
            raise RuntimeError("visual_candidate_materialization_failed")
        generation = getattr(claim, "claim_generation", None)
        attempt = getattr(claim, "attempt_count", None)
        if not isinstance(generation, int) or generation < 1 or not isinstance(attempt, int) or attempt < 1:
            raise RuntimeError("visual_candidate_materialization_failed")
        return prepare(
            task_id=str(claim.id),
            attempt_id=f"claim-{generation}-{attempt}",
            scope_id=str(claim.scope_id),
            selector=payload.get("_workspace_selector") if isinstance(payload.get("_workspace_selector"), str) else None,
            session_id=payload.get("_resume_session_id") if isinstance(payload.get("_resume_session_id"), str) else None,
            resume_of_attempt_id=payload.get("_resume_of_attempt_id") if isinstance(payload.get("_resume_of_attempt_id"), str) else None,
            image_relative_path=payload.get("image_relative_path") if isinstance(payload.get("image_relative_path"), str) else None,
            snapshot=snapshot,
        )

    if factory is not None:
        required_methods = ("for_scope", "for_task", "start_all", "shutdown")
        if any(not callable(getattr(factory, name, None)) for name in required_methods):
            raise DatabaseError("scope_service_factory_invalid")
    else:
        shared_worker_executor = ThreadPoolExecutor(
            max_workers=max(2, settings.opencode_concurrency + 1),
            thread_name_prefix="mememeow-scope-worker",
        )
        ownership.executor = shared_worker_executor
        worker_manager = (worker_manager_factory or PostgresTaskWorkerManager)(
            app.state.database,
            agent_concurrency=settings.opencode_concurrency,
            scope_concurrency=getattr(settings, "agent_scope_concurrency", 1),
            settings_version=settings.settings_version,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            executor=shared_worker_executor,
        )
        ownership.worker_manager = worker_manager
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
                settings_version=settings.settings_version,
                lease_seconds=settings.worker_lease_seconds,
                max_attempts=settings.worker_max_attempts,
                resume_enabled=bool(getattr(settings, "agent_resume_enabled", False)),
                resume_max_attempts=int(getattr(settings, "agent_resume_max_attempts", 2)),
                resume_backoff_seconds=int(getattr(settings, "agent_resume_backoff_seconds", 2)),
                resume_max_backoff_seconds=int(getattr(settings, "agent_resume_max_backoff_seconds", 60)),
                resume_timeout_seconds=int(getattr(settings, "agent_resume_timeout_seconds", 900)),
                visual_snapshot_preparer=getattr(local_visual_search, "precompute_snapshot", None),
                visual_candidate_preparer=prepare_visual_candidates,
                worker_manager=worker_manager,
                operation_policy=app.state.operation_policy_gateway,
                grant_store=app.state.operation_grants,
            )
            from backend.services.thumbnails import DerivedThumbnailService

            thumbnail_store = getattr(app.state.database, "thumbnail_store_for_scope", None)
            local_thumbnails = (
                DerivedThumbnailService(
                    app.state.database,
                    settings,
                    scope_id=local_scope,
                    task_service=local_tasks,
                )
                if callable(thumbnail_store)
                else None
            )
            local_services = ScopeServices(
                local_scope,
                local_metadata,
                local_search,
                local_tasks,
                local_reverse_image,
                local_visual_search,
                local_thumbnails,
            )
            ownership.local_services = local_services
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
                "visual_candidate_preparer": prepare_visual_candidates,
                "register_handlers": register_handlers,
                "start_services": start_scope_services,
            },
            preloaded={"local": local_services} if local_services is not None else None,
            worker_manager=worker_manager,
        )
    else:
        runtime_factory = factory
    ownership.runtime_factory = runtime_factory
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
            max_workers=validate_agent_concurrency(getattr(settings, "opencode_concurrency", 1)),
            task_handlers=dict(task_handlers),
        )
        ownership.image_workers.append(app.state.image_processing_workers[local_services.scope.scope_id])
        if callable(getattr(app.state.database, "factory", None)):
            app.state.image_processing_workers[local_services.scope.scope_id].start()
    tasks = local_services.tasks if local_services is not None else None
    if tasks is not None:
        app.state.tasks = tasks
    ownership.factory_started = True
    app.state.task_scope_diagnostics = runtime_factory.start_all()
    setup.factory = runtime_factory
    setup.worker_manager = worker_manager
    setup.shared_worker_executor = shared_worker_executor
    return ScopeRuntime(runtime_factory, shared_worker_executor, worker_manager, local_services, tasks)


async def start_extensions(app: FastAPI, started: list[ApplicationExtension] | None = None) -> list[ApplicationExtension]:
    """在公共数据库、factory 和 Worker 就绪后按顺序启动扩展。

    ``started`` 由调用方提供时会原地记录当前启动项，连同正在失败的扩展一起
    保留，确保外层 lifespan 在部分启动失败时仍能执行完整 shutdown hook。
    """
    started = started if started is not None else []
    for extension in extension_list(app):
        # hook 可能在中途创建后台任务后才抛错，因此失败项也必须进入 cleanup 集合。
        started.append(extension)
        await invoke_extension_hook(extension, "on_startup", app)
    return started


async def shutdown_lifecycle(
    setup: LifecycleSetup | None,
    runtime: ScopeRuntime | None,
    started_extensions: Sequence[ApplicationExtension] = (),
    *,
    primary_error: BaseException | None = None,
) -> None:
    """按依赖顺序关闭资源，并在单步失败时继续收束后续资源。"""
    if setup is None:
        return
    app = setup.app
    errors: list[tuple[str, BaseException]] = []
    for extension in reversed(tuple(started_extensions)):
        try:
            await invoke_extension_hook(extension, "on_shutdown", app)
        except BaseException as exc:  # noqa: BLE001 - 扩展失败不能跳过核心资源关闭
            errors.append(("shutdown.extension", exc))
    opencode = getattr(app.state, "opencode", None)
    _close_resource(opencode, "shutdown", errors, "shutdown.opencode")
    for image_worker in reversed(list(getattr(app.state, "image_processing_workers", {}).values())):
        _close_resource(image_worker, "shutdown", errors, "shutdown.image_worker")
    if runtime is not None:
        _close_resource(runtime.factory, "shutdown", errors, "shutdown.factory")
        _close_resource(runtime.worker_manager, "shutdown", errors, "shutdown.worker_manager")
        if runtime.shared_worker_executor is not None:
            try:
                runtime.shared_worker_executor.shutdown(wait=True, cancel_futures=True)
            except BaseException as exc:  # noqa: BLE001 - 线程池失败不能阻止 Engine 收束
                errors.append(("shutdown.executor", exc))
    _close_resource(setup.engine, "dispose", errors, "shutdown.engine")
    _clear_lifecycle_state(app, managed_factory=not setup.custom_factory)
    if primary_error is not None:
        _add_cleanup_notes(primary_error, errors)
        return
    if errors:
        _add_cleanup_notes(errors[0][1], errors[1:])
        raise errors[0][1]


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
