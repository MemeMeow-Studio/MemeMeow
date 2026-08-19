"""应用级 scope 解析与按 scope 创建轻量业务服务。

该模块位于 FastAPI 应用装配层和 PostgreSQL 领域服务之间。它只负责把宿主
提供的可信 scope 转换为不可变 ``ScopeContext``，以及为一次请求或一次后台
任务创建绑定该 scope 的服务视图；调用方身份和访问策略仍由适配宿主负责。
"""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Protocol, TYPE_CHECKING

from sqlalchemy import select

from backend.database import ScopeContext, Task, utcnow

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查，避免运行时循环导入
    from backend.config import Settings
    from backend.database import DatabaseResources


class ScopeResolutionError(RuntimeError):
    """scope 解析失败时使用的稳定边界错误。"""

    code = "scope_resolution_failed"
    status_code = 503

    def __init__(self, message: str = "请求 scope 无法解析") -> None:
        super().__init__(message)


class ScopeServicesError(ScopeResolutionError):
    """scope 服务视图或其子服务绑定不一致时的稳定错误。"""

    code = "scope_service_mismatch"


def _bound_scope(value: object) -> ScopeContext | None:
    """读取服务对象声明的 scope；不从任意业务字段猜测归属。"""
    declared = getattr(value, "scope", None)
    if isinstance(declared, ScopeContext):
        return declared
    if isinstance(declared, str):
        try:
            return ScopeContext(declared)
        except (TypeError, ValueError):
            return None
    if declared is not None:
        nested = getattr(declared, "scope_id", None)
        if isinstance(nested, str):
            try:
                return ScopeContext(nested)
            except (TypeError, ValueError):
                return None
    return None


def validate_scope_services(expected_scope: ScopeContext | str, services: object) -> "ScopeServices":
    """校验外层和所有 scope-bound 子服务的一致性。

    请求 middleware、任务 callback、Worker claim 后的 resolver 都必须经过同一
    入口。校验失败时不访问数据库、文件或业务 service，也不安装 local fallback。
    """
    expected = expected_scope if isinstance(expected_scope, ScopeContext) else ScopeContext(expected_scope)
    if not isinstance(services, ScopeServices):
        raise ScopeServicesError("scope service factory 返回了无效服务")
    outer = _bound_scope(services)
    if outer is None or outer != expected:
        raise ScopeServicesError("scope service 外层 scope 不一致")
    for name in ("metadata", "search", "tasks", "reverse_image", "visual_search"):
        child = getattr(services, name, None)
        child_scope = _bound_scope(child)
        if child_scope is None or child_scope != expected:
            raise ScopeServicesError(f"scope service 子服务 {name} 绑定不一致")
        # BlobStore 是 metadata 的文件副作用边界；若暴露 scope，则也必须一致。
        blob_store = getattr(child, "blob_store", None)
        if blob_store is not None:
            blob_scope = _bound_scope(blob_store)
            if blob_scope is not None and blob_scope != expected:
                raise ScopeServicesError("scope service blob store 绑定不一致")
    return services


class ScopeResolver(Protocol):
    """宿主注入的可信 scope 解析协议。

    实现只能从可信请求上下文返回已经验证的 scope；公共核心不会读取客户端
    的 ``scope_id``、``user_id``、路径或普通业务 payload 来推导 scope。
    """

    def resolve(self, request: Any) -> ScopeContext | str:
        """根据请求返回已经验证的 scope 上下文。"""

    def resolve_scope(self, request: Any) -> ScopeContext | str:
        """兼容宿主适配器使用的显式方法命名。"""


class LocalScopeResolver:
    """开源单实例适配器，将所有请求显式绑定到固定 local scope。"""

    def __init__(self, scope_id: str = "local") -> None:
        try:
            self._scope = ScopeContext(scope_id)
        except (TypeError, ValueError) as exc:
            raise ScopeResolutionError("local scope 配置无效") from exc

    @property
    def scope(self) -> ScopeContext:
        """返回不可变的固定 scope 上下文。"""
        return self._scope

    def resolve(self, request: Any) -> ScopeContext:
        """忽略客户端字段并返回模块级入口配置的固定 scope。"""
        del request
        return self._scope

    def __call__(self, request: Any) -> ScopeContext:
        """兼容将 resolver 作为普通 callable 注入的宿主适配器。"""
        return self.resolve(request)


def _validate_scope(value: object) -> ScopeContext:
    """将 resolver 结果收敛为不可变上下文，拒绝空值和非可信类型。"""
    if isinstance(value, ScopeContext):
        return value
    if isinstance(value, str):
        try:
            return ScopeContext(value)
        except (TypeError, ValueError) as exc:
            raise ScopeResolutionError("resolver 返回了无效 scope") from exc
    raise ScopeResolutionError("resolver 未返回有效 scope")


def _resolver_from_request(request: Any) -> ScopeResolver | Callable[[Any], object]:
    """读取应用装配时保存的 resolver，不允许业务边界自行安装 fallback。"""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    resolver = getattr(state, "scope_resolver", None)
    if resolver is None:
        raise ScopeResolutionError("应用未配置 scope resolver")
    return resolver


def _resolver_method(resolver: object) -> Callable[[Any], object] | None:
    """返回 resolver 的第一个可调用显式方法，避免非 callable 属性遮蔽备用方法。"""
    for name in ("resolve", "resolve_scope"):
        method = getattr(resolver, name, None)
        if callable(method):
            return method
    return resolver if callable(resolver) else None


def _invoke_resolver(resolver: object, request: Any) -> object:
    """调用同步 resolver；异步 resolver 由 ``resolve_scope_async`` 处理。"""
    method = _resolver_method(resolver)
    if method is None:
        raise ScopeResolutionError("scope resolver 不可调用")
    value = method(request)
    if inspect.isawaitable(value):
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise ScopeResolutionError("异步 resolver 必须通过异步 scope 入口调用")
    return value


def resolve_scope(request: Any) -> ScopeContext:
    """在请求边界解析一次可信 scope，并返回不可变上下文。

    该函数不会读取请求参数，也不会在 resolver 缺失或异常时回退到 ``local``。
    FastAPI 中间件使用它将结果写入 ``request.state.scope``；路由和服务不得再次
    从客户端字段解析 scope。
    """
    resolver = _resolver_from_request(request)
    try:
        return _validate_scope(_invoke_resolver(resolver, request))
    except ScopeResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - 宿主异常统一收敛为稳定错误
        if getattr(exc, "status_code", None) in {401, 503} and getattr(exc, "code", None):
            raise
        raise ScopeResolutionError() from exc


async def resolve_scope_async(request: Any) -> ScopeContext:
    """解析支持 ``async resolve`` 的宿主 resolver，供异步中间件使用。"""
    resolver = _resolver_from_request(request)
    try:
        method = _resolver_method(resolver)
        if method is None:
            raise ScopeResolutionError("scope resolver 不可调用")
        value = method(request)
        if inspect.isawaitable(value):
            value = await value
        return _validate_scope(value)
    except ScopeResolutionError:
        raise
    except Exception as exc:  # noqa: BLE001 - 宿主异常统一收敛为稳定错误
        if getattr(exc, "status_code", None) in {401, 503} and getattr(exc, "code", None):
            raise
        raise ScopeResolutionError() from exc


if TYPE_CHECKING:
    from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService
    from backend.reverse_image import ReverseImageService
    from backend.visual import VisualSearchService


@dataclass(frozen=True)
class ScopeServices:
    """一次请求或任务专属的不可变服务视图。

    ``metadata``、``search``、``tasks``、``reverse_image`` 和 ``visual_search`` 都
    在构造时绑定 ``scope``，视图本身不允许切换 scope；数据库 Engine、连接池、
    模型客户端和运行时配置由工厂共享。
    """

    scope: ScopeContext
    metadata: "PostgresMetadataService"
    search: "PostgresSearchService"
    tasks: "PostgresTaskService"
    reverse_image: "ReverseImageService"
    visual_search: "VisualSearchService"

    @property
    def search_engine(self) -> "PostgresSearchService":
        """返回兼容旧调用方命名的 scope-bound 搜索服务。"""
        return self.search

    @property
    def visual(self) -> "VisualSearchService":
        """返回兼容内部回调命名的 scope-bound 视觉查询服务。"""
        return self.visual_search


class ScopeServiceFactory:
    """复用重资源并为每个 scope 创建轻量业务服务。

    工厂不保存可变的当前 scope。每次 ``for_scope`` 都创建新的服务视图，因而
    并发请求或后台任务无法通过原地修改共享 singleton 互相污染。
    """

    def __init__(
        self,
        resources: "DatabaseResources",
        settings: "Settings",
        *,
        task_config: dict[str, Any] | None = None,
        preloaded: dict[str, ScopeServices] | None = None,
        worker_manager: Any | None = None,
    ) -> None:
        self.resources = resources
        self.settings = settings
        self._task_config = dict(task_config or {})
        self._services: dict[str, ScopeServices] = dict(preloaded or {})
        self._initialized_scopes: set[str] = set()
        self._lock = RLock()
        configured_executor = self._task_config.get("executor")
        if configured_executor is not None and not isinstance(configured_executor, ThreadPoolExecutor):
            raise TypeError("scope_executor_invalid")
        if worker_manager is None:
            from backend.pg_services import PostgresTaskWorkerManager

            worker_manager = PostgresTaskWorkerManager(
                resources,
                agent_concurrency=self._task_config.get("agent_concurrency", getattr(settings, "opencode_concurrency", 1)),
                agent_backpressure=self._task_config.get("agent_backpressure", getattr(settings, "agent_backpressure", 32)),
                settings_version=self._task_config.get("settings_version", getattr(settings, "settings_version", None)),
                lease_seconds=self._task_config.get("lease_seconds", getattr(settings, "worker_lease_seconds", 120)),
                max_attempts=self._task_config.get("max_attempts", getattr(settings, "worker_max_attempts", 3)),
                executor=configured_executor,
            )
            self._owns_worker_manager = True
        else:
            self._owns_worker_manager = False
        self._worker_manager = worker_manager
        self._worker_manager.set_service_resolvers(self.for_task, self.for_scope)

    def for_scope(self, scope: ScopeContext | str) -> ScopeServices:
        """为指定 scope 创建独立的元数据、检索、任务和回调服务。"""
        context = scope if isinstance(scope, ScopeContext) else ScopeContext(scope)
        with self._lock:
            existing = self._services.get(context.scope_id)
            if existing is not None:
                return validate_scope_services(context, existing)
            initialize = context.scope_id not in self._initialized_scopes
            if initialize:
                self._initialized_scopes.add(context.scope_id)
        try:
            # 局部导入避免 database 与 pg_services 在启动期形成循环导入。
            from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService
            from backend.reverse_image import ReverseImageService
            from backend.visual import VisualSearchService

            metadata = PostgresMetadataService(self.resources, scope_id=context.scope_id)
            search = PostgresSearchService(self.settings, self.resources, metadata, scope_id=context.scope_id)
            reverse_kwargs: dict[str, Any] = {
                "scope_id": context.scope_id,
                "provider": self._task_config.get("reverse_provider"),
            }
            if self._task_config.get("operation_policy") is not None:
                reverse_kwargs["operation_policy"] = self._task_config["operation_policy"]
            if self._task_config.get("grant_store") is not None:
                reverse_kwargs["grant_store"] = self._task_config["grant_store"]
            reverse_image = ReverseImageService(self.settings, self.resources, **reverse_kwargs)
            visual_search = VisualSearchService(self.settings, self.resources, scope_id=context.scope_id)
            tasks = PostgresTaskService(
                self.resources,
                scope_id=context.scope_id,
                agent_concurrency=self._task_config.get("agent_concurrency", getattr(self.settings, "opencode_concurrency", 1)),
                agent_backpressure=self._task_config.get("agent_backpressure", getattr(self.settings, "agent_backpressure", 32)),
                settings_version=self._task_config.get("settings_version", getattr(self.settings, "settings_version", None)),
                lease_seconds=self._task_config.get("lease_seconds", getattr(self.settings, "worker_lease_seconds", 120)),
                max_attempts=self._task_config.get("max_attempts", getattr(self.settings, "worker_max_attempts", 3)),
                resume_enabled=self._task_config.get("resume_enabled", getattr(self.settings, "agent_resume_enabled", False)),
                resume_max_attempts=self._task_config.get("resume_max_attempts", getattr(self.settings, "agent_resume_max_attempts", 2)),
                resume_backoff_seconds=self._task_config.get("resume_backoff_seconds", getattr(self.settings, "agent_resume_backoff_seconds", 2)),
                resume_max_backoff_seconds=self._task_config.get("resume_max_backoff_seconds", getattr(self.settings, "agent_resume_max_backoff_seconds", 60)),
                resume_timeout_seconds=self._task_config.get("resume_timeout_seconds", getattr(self.settings, "agent_resume_timeout_seconds", 900)),
                worker_manager=self._worker_manager,
            )
            register = self._task_config.get("register_handlers")
            services = ScopeServices(context, metadata, search, tasks, reverse_image, visual_search)
            # 在注册 handler 或启动子服务前完成一致性校验，避免装配错误已经产生副作用。
            validate_scope_services(context, services)
            if callable(register):
                register(services)
            start = self._task_config.get("start_services") if initialize else None
            if callable(start):
                start(services)
        except Exception:
            # 构造器、scope 校验或启动回调任一步失败，都允许下一次请求重新尝试。
            with self._lock:
                if initialize:
                    self._initialized_scopes.discard(context.scope_id)
            raise
        return validate_scope_services(context, services)

    def for_task(self, task_id: str) -> ScopeServices:
        """从持久 Task.scope_id 恢复 callback 所需的服务环境。"""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ScopeResolutionError("任务标识无效")
        with self.resources.factory() as session:
            scope_id = session.scalar(select(Task.scope_id).where(Task.id == task_id.strip()))
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ScopeResolutionError("任务缺少有效 scope")
        try:
            context = ScopeContext(scope_id)
        except (TypeError, ValueError) as exc:
            raise ScopeResolutionError("任务 scope 无效") from exc
        return validate_scope_services(context, self.for_scope(context))

    def start_all(self) -> dict[str, list[str]]:
        """启动一次进程级 Worker manager，而不是为历史 scope 创建 Worker。"""
        return self._worker_manager.start()

    def shutdown(self) -> None:
        """关闭进程级 Worker manager，连接池由应用生命周期统一释放。"""
        if self._owns_worker_manager:
            self._worker_manager.shutdown()
