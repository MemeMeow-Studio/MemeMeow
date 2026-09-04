"""应用 scope 解析边界和显式 FastAPI 装配测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api as api_module
from api import bind_request_scope, create_app
from backend.database import ScopeContext
from backend.scope import (
    LocalScopeResolver,
    ScopeResolutionError,
    ScopeServiceFactory,
    ScopeServices,
    ScopeServicesError,
    resolve_scope,
    validate_scope_services,
)
from backend.tasks import TaskRecord


def _request(resolver: object | None = None):
    """构造不依赖 ASGI 生命周期的最小 resolver 请求。"""
    state = SimpleNamespace()
    if resolver is not None:
        state.scope_resolver = resolver
    return SimpleNamespace(app=SimpleNamespace(state=state))


class _ClosedScopeResolver:
    """模拟适配宿主注入的非 local 可信 scope resolver。"""

    def resolve(self, request):
        """返回宿主认证后绑定的 scope，不读取客户端请求字段。"""
        del request
        return "scope-a"


def _install_lifespan_doubles(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    """为生命周期测试注入无数据库副作用的资源、Worker 和 scope facade 替身。"""

    class FakeEngine:
        """记录共享 Engine 是否在生命周期结束时释放。"""

        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            """记录 dispose 调用。"""
            self.disposed = True

    class FakeSettings:
        """提供默认 factory 装配所需的最小只读配置。"""

        image_root = tmp_path / "images"
        data_root = tmp_path / "data"
        opencode_runtime_root = tmp_path / "runtime"
        database_url = "postgresql+psycopg://host/test"
        expected_database_revision = "test-revision"
        opencode_concurrency = 1
        agent_backpressure = 32
        settings_version = "test-settings"
        worker_lease_seconds = 120
        worker_max_attempts = 3

        def ensure_directories(self) -> None:
            """创建测试运行目录，不触碰数据库 scope。"""
            self.data_root.mkdir(parents=True, exist_ok=True)

    class FakeResources:
        """记录 local scope 开关并拒绝任何未预期的 preflight。"""

        def __init__(self, engine, **kwargs) -> None:
            self.engine = engine
            self.require_local_scope = kwargs["require_local_scope"]
            self.preflight_calls: list[object] = []

        def flat_preflight(self, scope) -> dict[str, object]:
            """记录 local 预检调用，供 OSS local 路径断言。"""
            self.preflight_calls.append(scope)
            return {}

    class FakeRunner:
        """隔离生命周期测试中的外部 Agent runner。"""

        def shutdown(self) -> None:
            """满足应用关闭协议。"""

    class FakeWorkerManager:
        """记录进程级 manager 的 handler、启动和关闭动作。"""

        last_instance = None

        def __init__(self, resources, **kwargs) -> None:
            del resources
            type(self).last_instance = self
            self.executor = kwargs["executor"]
            self.owner = "test-worker"
            self.registered: dict[str, object] = {}
            self.started = False
            self.stopped = False
            self.task_resolver = None
            self.scope_resolver = None

        @property
        def worker_count(self) -> int:
            """返回测试中唯一的进程级 manager 数量。"""
            return 0 if self.stopped else 1

        def set_service_resolvers(self, task_resolver, scope_resolver) -> None:
            """记录 factory 安装的持久任务和 scope facade 解析器。"""
            self.task_resolver = task_resolver
            self.scope_resolver = scope_resolver

        def register(self, task_type, handler) -> None:
            """记录全局 handler 注册，不创建任何 scope facade。"""
            self.registered[task_type] = handler

        def start(self) -> dict[str, list[str]]:
            """记录 manager 启动并返回标准诊断结构。"""
            self.started = True
            return {"started": [self.owner], "invalid_tasks": []}

        def shutdown(self) -> None:
            """记录 manager 关闭。"""
            self.stopped = True

    class DummyMetadata:
        """scope-bound 元数据 facade 替身。"""

        def __init__(self, _resources, *, scope_id):
            self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
            service_scope_calls.append(self.scope.scope_id)
            self.blob_store = SimpleNamespace()
            self.recovered = False

        def recover_storage(self, *, limit: int) -> dict[str, int]:
            """记录 local 启动时的存储恢复调用。"""
            del limit
            self.recovered = True
            return {}

    class DummySearch:
        """scope-bound 搜索 facade 替身。"""

        def __init__(self, _settings, _resources, _metadata, *, scope_id):
            self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
            service_scope_calls.append(self.scope.scope_id)

    class DummyTasks:
        """只记录共享 manager，不创建 scope 私有线程池。"""

        def __init__(self, _resources, *, scope_id, worker_manager, **_kwargs):
            self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
            service_scope_calls.append(self.scope.scope_id)
            self.worker_manager = worker_manager
            self.registered: dict[str, object] = {}
            self.finalizer = None

        def register(self, task_type, handler) -> None:
            """记录 facade 转发到进程级 registry 的 handler。"""
            self.registered[task_type] = handler
            self.worker_manager.register(task_type, handler)

        def set_batch_finalizer(self, callback) -> None:
            """记录 scope 专属批次收束回调。"""
            self.finalizer = callback

    class DummyReverse:
        """scope-bound 反向图片 facade 替身。"""

        def __init__(self, _settings, _resources, *, scope_id, provider=None):
            del provider
            self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
            service_scope_calls.append(self.scope.scope_id)

    class DummyVisual:
        """scope-bound 视觉 facade 替身。"""

        def __init__(self, _settings, _resources, *, scope_id):
            self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
            service_scope_calls.append(self.scope.scope_id)

    engine = FakeEngine()
    settings = FakeSettings()
    resources: list[FakeResources] = []
    check_calls: list[dict[str, object]] = []
    service_scope_calls: list[str] = []

    def make_resources(engine_arg, **kwargs):
        """保存生命周期实际传入的 local-scope 配置。"""
        value = FakeResources(engine_arg, **kwargs)
        resources.append(value)
        return value

    def check_database(_engine, **kwargs):
        """记录数据库启动门禁参数，不连接真实 PostgreSQL。"""
        check_calls.append(kwargs)
        return {"revision": "test", "pgvector": True}

    monkeypatch.setattr(api_module.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(api_module, "create_engine_for_settings", lambda _settings: engine)
    monkeypatch.setattr(api_module, "check_database", check_database)
    monkeypatch.setattr(api_module, "DatabaseResources", make_resources)
    monkeypatch.setattr(api_module, "OpenCodeRunner", lambda _settings: FakeRunner())
    monkeypatch.setattr(api_module, "OpenCodeActivityReader", lambda _root: object())
    monkeypatch.setattr(api_module, "VisualInferenceClient", lambda _settings: object())
    monkeypatch.setattr(api_module, "PostgresTaskWorkerManager", FakeWorkerManager)
    monkeypatch.setattr(api_module, "PostgresMetadataService", DummyMetadata)
    monkeypatch.setattr(api_module, "PostgresSearchService", DummySearch)
    monkeypatch.setattr(api_module, "PostgresTaskService", DummyTasks)
    monkeypatch.setattr(api_module, "ReverseImageService", DummyReverse)
    monkeypatch.setattr(api_module, "VisualSearchService", DummyVisual)
    return SimpleNamespace(engine=engine, settings=settings, resources=resources, check_calls=check_calls, service_scope_calls=service_scope_calls, worker_class=FakeWorkerManager)


def test_local_scope_resolver_ignores_client_fields_and_is_immutable() -> None:
    """local 适配器不读取伪造字段，ScopeContext 不能被原地修改。"""
    request = _request(LocalScopeResolver("local"))
    request.query_params = {"scope_id": "scope-b", "user_id": "attacker"}
    assert resolve_scope(request) == ScopeContext("local")
    with pytest.raises((AttributeError, TypeError)):
        resolve_scope(request).scope_id = "scope-b"  # type: ignore[misc]


def test_missing_or_invalid_resolver_fails_closed() -> None:
    """缺失 resolver、异常 resolver 和空结果都不能回退 local。"""
    with pytest.raises(ScopeResolutionError, match="未配置"):
        resolve_scope(_request())

    class BrokenResolver:
        """模拟宿主认证上下文异常。"""

        def resolve(self, request):
            """返回无效结果以验证稳定失败。"""
            del request
            return None

    with pytest.raises(ScopeResolutionError):
        resolve_scope(_request(BrokenResolver()))


def test_create_app_requires_explicit_resolver() -> None:
    """应用工厂漏配 resolver 时直接 fail-closed。"""
    with pytest.raises(ScopeResolutionError):
        create_app(scope_resolver=None)
    application = create_app(scope_resolver=LocalScopeResolver("local"))
    assert application.state.scope_resolver.scope.scope_id == "local"


def test_scope_resolver_supports_explicit_scope_method_and_rejects_delimiter_controls() -> None:
    """宿主只实现 resolve_scope 时仍可注入，DEL 等控制字符不能进入 scope。"""

    class ScopeMethodResolver:
        """模拟适配宿主的显式 resolver 方法。"""

        resolve = "not-callable"

        def resolve_scope(self, request):
            """返回可信 scope，证明备用方法不会被同名属性遮蔽。"""
            del request
            return "scope-a"

    assert resolve_scope(_request(ScopeMethodResolver())) == ScopeContext("scope-a")
    with pytest.raises(ValueError, match="scope_invalid"):
        ScopeContext("scope-\x7f-a")


def test_create_app_keeps_host_factory_injection() -> None:
    """应用装配应保留宿主 factory，不能在构造阶段替换成隐式 local 服务。"""
    factory = object()
    provider = object()
    application = create_app(scope_resolver=LocalScopeResolver("local"), service_factory=factory, agent_input_provider=provider)
    assert application.state.service_factory is factory
    assert application.state.agent_input_provider is provider


def test_application_extension_registers_routes_once() -> None:
    """宿主扩展可以附加路由，且公共工厂不会复制业务路由。"""
    from fastapi import FastAPI

    class RouteExtension:
        """只注册一个测试路由的最小扩展。"""

        def register_routes(self, app: FastAPI) -> None:
            """向应用注册扩展路由。"""
            app.add_api_route("/extension-probe", lambda: {"ok": True}, methods=["GET"])

    application = create_app(scope_resolver=LocalScopeResolver("local"), extensions=(RouteExtension(),))
    paths = [getattr(route, "path", None) for route in application.routes]
    assert paths.count("/extension-probe") == 1


def test_application_extension_lifecycle_runs_after_factory_and_before_shutdown(monkeypatch, tmp_path: Path) -> None:
    """扩展启动发生在公共服务就绪后，关闭发生在公共资源释放前。"""
    doubles = _install_lifespan_doubles(monkeypatch, tmp_path)
    events: list[tuple[str, bool]] = []

    class LifecycleExtension:
        """记录扩展生命周期相对公共工厂的顺序。"""

        async def on_startup(self, app) -> None:
            """记录启动时公共 factory 已完成装配。"""
            events.append(("startup", callable(getattr(app.state.service_factory, "start_all", None))))

        async def on_shutdown(self, app) -> None:
            """记录关闭时公共运行时仍可访问。"""
            events.append(("shutdown", hasattr(app.state, "opencode")))

    application = create_app(scope_resolver=_ClosedScopeResolver(), extensions=(LifecycleExtension(),))

    async def exercise() -> None:
        """运行一次生命周期以收集扩展事件。"""
        async with api_module.lifespan(application):
            events.append(("body", doubles.worker_class.last_instance is not None))

    asyncio.run(exercise())
    assert events == [("startup", True), ("body", True), ("shutdown", True)]


def test_scope_exempt_extension_skips_resolver_and_authorizes_path() -> None:
    """scope 豁免路径只执行扩展的独立授权，不调用业务 resolver。"""
    events: list[str] = []

    class BrokenResolver:
        """一旦被调用就失败，证明豁免路径未解析 scope。"""

        def resolve(self, _request):
            """返回不应到达的分支。"""
            raise AssertionError("scope 豁免路径不应调用 resolver")

    class ExemptExtension:
        """声明健康检查路径为 scope 豁免并记录授权。"""

        def scope_exempt_paths(self):
            """返回精确豁免路径。"""
            return ("/health",)

        async def authorize_exempt_request(self, _request) -> None:
            """记录豁免路径已通过独立授权。"""
            events.append("exempt-authorized")

    app = SimpleNamespace(state=SimpleNamespace(scope_resolver=BrokenResolver(), extensions=(ExemptExtension(),)))
    request = SimpleNamespace(
        url=SimpleNamespace(path="/health"),
        app=app,
        query_params={},
        headers={},
        state=SimpleNamespace(),
    )

    async def call_next(_request):
        """记录豁免 handler 继续执行。"""
        events.append("handler")
        return "ok"

    assert asyncio.run(bind_request_scope(request, call_next)) == "ok"
    assert events == ["exempt-authorized", "handler"]


def test_scope_authorization_hook_runs_after_services_and_before_handler() -> None:
    """scope-bound 授权钩子收到完整服务视图，并先于业务 handler 执行。"""
    events: list[str] = []

    class Resolver:
        """返回固定可信 scope 的测试 resolver。"""

        def resolve(self, _request):
            """记录 scope 解析并返回 scope-a。"""
            events.append("resolver")
            return "scope-a"

    class Factory:
        """返回与 scope 一致的最小服务视图。"""

        def for_scope(self, scope):
            """记录服务装配后返回绑定视图。"""
            events.append("factory")
            return _scope_services(scope.scope_id)

    class Extension:
        """记录 scope-bound 授权钩子的参数。"""

        async def authorize_request(self, _request, scope, services) -> None:
            """确认钩子收到解析后的 scope 和服务。"""
            assert scope == ScopeContext("scope-a")
            assert validate_scope_services(scope, services) is services
            events.append("authorize")

    app = SimpleNamespace(state=SimpleNamespace(scope_resolver=Resolver(), service_factory=Factory(), extensions=(Extension(),)))
    request = SimpleNamespace(
        url=SimpleNamespace(path="/images"),
        app=app,
        query_params={},
        headers={},
        state=SimpleNamespace(),
    )

    async def call_next(_request):
        """记录业务 handler 执行。"""
        events.append("handler")
        return "ok"

    assert asyncio.run(bind_request_scope(request, call_next)) == "ok"
    assert events == ["resolver", "factory", "authorize", "handler"]
    assert request.state.scope == ScopeContext("scope-a")


def test_scope_authorization_hook_rejection_skips_handler() -> None:
    """scope-bound 授权拒绝时返回稳定响应且不进入业务 handler。"""
    called: list[str] = []

    class Extension:
        """拒绝所有 scope-bound 请求的测试扩展。"""

        async def authorize_request(self, _request, _scope, _services) -> None:
            """返回明确的 HTTP 拒绝。"""
            raise HTTPException(status_code=403, detail={"error": "extension_forbidden", "message": "请求未获授权"})

    app = SimpleNamespace(state=SimpleNamespace(scope_resolver=lambda _request: "scope-a", service_factory=SimpleNamespace(for_scope=lambda scope: _scope_services(scope.scope_id)), extensions=(Extension(),)))
    request = SimpleNamespace(
        url=SimpleNamespace(path="/images"),
        app=app,
        query_params={},
        headers={},
        state=SimpleNamespace(),
    )

    async def call_next(_request):
        """该 handler 不应被授权失败路径调用。"""
        called.append("handler")
        raise AssertionError("授权失败后不应执行业务 handler")

    response = asyncio.run(bind_request_scope(request, call_next))
    assert response.status_code == 403
    assert called == []


def test_host_lifespan_never_requires_local_scope_or_preflight(monkeypatch, tmp_path: Path) -> None:
    """宿主 factory 启动不调用 local 装配，也允许数据库没有 local namespace。"""
    class FakeEngine:
        """记录生命周期是否正确释放共享 Engine。"""

        def __init__(self) -> None:
            self.disposed = False

        def dispose(self) -> None:
            """记录 dispose 调用。"""
            self.disposed = True

    class FakeSettings:
        """提供 host lifespan 所需的最小只读配置。"""

        image_root = tmp_path / "host-images"
        data_root = tmp_path / "host-data"
        opencode_runtime_root = tmp_path / "host-runtime"
        database_url = "postgresql+psycopg://host/test"
        expected_database_revision = "test-revision"

        def ensure_directories(self) -> None:
            """host 仍需要创建运行时目录，但不创建 local scope。"""
            self.data_root.mkdir(parents=True, exist_ok=True)

    class FakeResources:
        """拒绝任何未授权的 local preflight 调用。"""

        def __init__(self, engine, **kwargs) -> None:
            self.require_local_scope = kwargs["require_local_scope"]
            self.preflight_calls: list[object] = []

        def flat_preflight(self, scope) -> dict[str, object]:
            """记录调用，测试应断言该方法从未被调用。"""
            self.preflight_calls.append(scope)
            raise AssertionError("host lifespan 不应执行 local preflight")

    class FakeRunner:
        """隔离测试中的外部 Agent runner。"""

        def shutdown(self) -> None:
            """满足 lifespan 关闭协议。"""

    class HostFactory:
        """只提供 scope-aware factory 协议，不提供 local scope。"""

        def __init__(self) -> None:
            self.for_scope_calls: list[object] = []
            self.started = False
            self.stopped = False

        def for_scope(self, scope):
            """任何 local 调用都说明 host 启动仍有隐式 fallback。"""
            self.for_scope_calls.append(scope)
            if getattr(scope, "scope_id", scope) == "local":
                raise AssertionError("host factory 不应装配 local")
            raise AssertionError("测试不应在 lifespan 内创建业务 scope")

        def for_task(self, task_id):
            """提供内部 callback 所需的协议占位。"""
            del task_id
            return None

        def start_all(self):
            """host 自己负责全局 Worker 启动。"""
            self.started = True
            return {"started": ["host-worker"], "invalid_tasks": []}

        def shutdown(self) -> None:
            """记录 host factory 关闭。"""
            self.stopped = True

    engine = FakeEngine()
    settings = FakeSettings()
    resources: list[FakeResources] = []
    factory = HostFactory()
    check_calls: list[dict[str, object]] = []

    def make_resources(engine_arg, **kwargs):
        """保存实际传入的 host local-scope 开关。"""
        value = FakeResources(engine_arg, **kwargs)
        resources.append(value)
        return value

    monkeypatch.setattr(api_module.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(api_module, "create_engine_for_settings", lambda _settings: engine)
    monkeypatch.setattr(api_module, "check_database", lambda _engine, **kwargs: (check_calls.append(kwargs) or {"revision": "test", "pgvector": True}))
    monkeypatch.setattr(api_module, "DatabaseResources", make_resources)
    monkeypatch.setattr(api_module, "OpenCodeRunner", lambda _settings: FakeRunner())
    monkeypatch.setattr(api_module, "OpenCodeActivityReader", lambda _root: object())
    monkeypatch.setattr(api_module, "VisualInferenceClient", lambda _settings: object())

    application = create_app(scope_resolver=_ClosedScopeResolver(), service_factory=factory)
    asyncio.run(_exercise_lifespan(application, factory, resources, engine))
    assert check_calls[0]["require_local_installation"] is False


async def _exercise_lifespan(application, factory, resources, engine) -> None:
    """运行 host lifespan 并断言 local 资源从未被访问。"""
    async with api_module.lifespan(application):
        assert resources[0].require_local_scope is False
        assert resources[0].preflight_calls == []
        assert factory.for_scope_calls == []
        assert factory.started is True
        assert not hasattr(application.state, "metadata")
        assert not hasattr(application.state, "tasks")
    assert factory.stopped is True
    assert engine.disposed is True


def test_nonlocal_resolver_with_default_factory_never_preloads_local(monkeypatch, tmp_path: Path) -> None:
    """非 local resolver 使用核心默认 factory 时不创建、预检或缓存 local facade。"""
    doubles = _install_lifespan_doubles(monkeypatch, tmp_path)
    application = create_app(scope_resolver=_ClosedScopeResolver())

    async def exercise() -> None:
        """运行默认 factory 生命周期并检查启动前后的 scope 边界。"""
        async with api_module.lifespan(application):
            manager = doubles.worker_class.last_instance
            assert manager is not None
            assert manager.started is True
            assert set(manager.registered) == {"cache_generation", "metadata_repair", "derived_thumbnail_generation", "visual_embedding_generation", "meme_context_generation", "image_auto_rename"}
            assert doubles.resources[0].require_local_scope is False
            assert doubles.resources[0].preflight_calls == []
            assert doubles.check_calls[0]["require_local_installation"] is False
            assert not hasattr(application.state, "resolver")
            assert not hasattr(application.state, "metadata")
            assert not hasattr(application.state, "tasks")
            assert doubles.service_scope_calls == []
            assert isinstance(application.state.service_factory, ScopeServiceFactory)
            assert application.state.service_factory._services == {}

    asyncio.run(exercise())
    manager = doubles.worker_class.last_instance
    assert manager is not None and manager.stopped is True
    assert doubles.engine.disposed is True


def test_explicit_oss_local_resolver_keeps_local_preflight_and_facade(monkeypatch, tmp_path: Path) -> None:
    """只有显式 OSS LocalScopeResolver("local") 才保留 local 预检和默认 facade。"""
    assert isinstance(api_module.app.state.scope_resolver, LocalScopeResolver)
    assert api_module.app.state.scope_resolver.scope.scope_id == "local"
    doubles = _install_lifespan_doubles(monkeypatch, tmp_path)
    application = create_app(scope_resolver=LocalScopeResolver("local"))

    async def exercise() -> None:
        """运行 OSS local 生命周期并检查 local 资源确实被显式装配。"""
        async with api_module.lifespan(application):
            manager = doubles.worker_class.last_instance
            assert manager is not None
            assert manager.started is True
            assert doubles.resources[0].require_local_scope is True
            assert doubles.resources[0].preflight_calls == [ScopeContext("local")]
            assert doubles.check_calls[0]["require_local_installation"] is True
            assert hasattr(application.state, "resolver")
            assert hasattr(application.state, "metadata")
            assert hasattr(application.state, "tasks")
            assert doubles.service_scope_calls == ["local"] * 5
            assert set(application.state.service_factory._services) == {"local"}
            assert application.state.service_factory._services["local"].metadata.recovered is True

    asyncio.run(exercise())
    manager = doubles.worker_class.last_instance
    assert manager is not None and manager.stopped is True
    assert doubles.engine.disposed is True


def test_many_scope_facades_share_one_process_worker_manager(monkeypatch) -> None:
    """大量 scope 只创建轻量 facade，不复制 Worker、线程池或 owner。"""
    import backend.pg_services as pg_services
    import backend.reverse_image as reverse_image
    import backend.visual as visual

    class DummyMetadata:
        """scope facade 测试替身。"""

        def __init__(self, _resources, *, scope_id):
            self.scope = scope_id
            self.blob_store = SimpleNamespace()

    class DummySearch:
        """搜索 facade 测试替身。"""

        def __init__(self, _settings, _resources, _metadata, *, scope_id):
            self.scope = scope_id

    class DummyTasks:
        """只记录共享 manager，不创建自己的 executor。"""

        def __init__(self, _resources, *, scope_id, worker_manager, **_kwargs):
            self.scope = ScopeContext(scope_id)
            self.worker_manager = worker_manager

        def register(self, _task_type, _handler):
            """兼容 factory 的 handler 注册钩子。"""

        def set_batch_finalizer(self, _callback):
            """兼容批次 finalizer 注册钩子。"""

    class DummyReverse:
        """反向图片 facade 测试替身。"""

        def __init__(self, _settings, _resources, *, scope_id, provider=None):
            self.scope = scope_id

    class DummyVisual:
        """视觉 facade 测试替身。"""

        def __init__(self, _settings, _resources, *, scope_id):
            self.scope = scope_id

    monkeypatch.setattr(pg_services, "PostgresMetadataService", DummyMetadata)
    monkeypatch.setattr(pg_services, "PostgresSearchService", DummySearch)
    monkeypatch.setattr(pg_services, "PostgresTaskService", DummyTasks)
    monkeypatch.setattr(reverse_image, "ReverseImageService", DummyReverse)
    monkeypatch.setattr(visual, "VisualSearchService", DummyVisual)

    class EmptySession:
        """让 manager 关闭路径无需真实数据库即可验证 executor 生命周期。"""

        def __enter__(self):
            """返回空 session。"""
            return self

        def __exit__(self, *_args):
            """结束空 session。"""

        def scalars(self, _statement):
            """关闭测试没有 running 任务。"""
            return []

        def commit(self) -> None:
            """兼容 manager 的事务提交。"""

    settings = SimpleNamespace(opencode_concurrency=2, agent_backpressure=32, settings_version="test", worker_lease_seconds=120, worker_max_attempts=3)
    manager_resources = SimpleNamespace(factory=lambda: EmptySession())
    factory = ScopeServiceFactory(manager_resources, settings)
    try:
        services = [factory.for_scope(f"scope-{index}") for index in range(100)]
        assert factory._services == {}
        assert len({id(service.tasks.worker_manager) for service in services}) == 1
        assert factory._worker_manager.worker_count == 1
        assert factory._worker_manager.owner == services[0].tasks.worker_manager.owner
    finally:
        factory.shutdown()


def test_internal_callback_does_not_require_user_request_scope() -> None:
    """内部 callback 由 task 控制面恢复 scope，不应被公共 resolver 提前拦截。"""
    state = SimpleNamespace(service_factory=SimpleNamespace(for_task=lambda _task_id: None))
    request = SimpleNamespace(url=SimpleNamespace(path="/internal/reverse-image/search"), app=SimpleNamespace(state=state))
    called: list[bool] = []

    async def call_next(_request):
        """记录 callback 已进入路由层。"""
        called.append(True)
        return "ok"

    assert asyncio.run(bind_request_scope(request, call_next)) == "ok"
    assert called == [True]


def test_public_scope_middleware_fails_closed_without_resolver() -> None:
    """公共请求缺少 resolver 时在业务路由前返回稳定 503。"""
    state = SimpleNamespace(service_factory=SimpleNamespace(for_scope=lambda _scope: None))
    request = SimpleNamespace(url=SimpleNamespace(path="/images"), app=SimpleNamespace(state=state))

    async def call_next(_request):
        """该回调不应在 scope 解析失败时被调用。"""
        raise AssertionError("业务路由不应执行")

    response = asyncio.run(bind_request_scope(request, call_next))
    assert response.status_code == 503


def test_task_scope_is_internal_and_never_serialized_as_public_field() -> None:
    """任务快照可以内部保存 scope，但公开结构不能把授权事实回显给客户端。"""
    record = TaskRecord(task_id="task-1", task_type="cache_generation", scope_id="scope-a")
    assert record.scope_id == "scope-a"
    assert "scope_id" not in record.as_dict()


def _scope_services(scope_id: str, *, child_scope_id: str | None = None) -> ScopeServices:
    """构造只含 scope 声明的最小服务视图，供一致性测试使用。"""
    child_scope = ScopeContext(child_scope_id or scope_id)
    child = SimpleNamespace(scope=child_scope, blob_store=SimpleNamespace(scope=child_scope))
    return ScopeServices(
        ScopeContext(scope_id),
        child,
        child,
        child,
        child,
        child,
    )


def test_scope_service_validation_rejects_outer_and_child_scope_mismatch() -> None:
    """factory 返回错误外层或子服务 scope 时必须在业务访问前失败。"""
    with pytest.raises(ScopeServicesError, match="外层"):
        validate_scope_services("scope-a", _scope_services("scope-b"))
    with pytest.raises(ScopeServicesError, match="子服务"):
        validate_scope_services("scope-a", _scope_services("scope-a", child_scope_id="scope-b"))


def test_worker_factory_mismatch_settles_only_the_complete_claim() -> None:
    """Worker 装配返回错误 scope 时只用完整 claim fencing 收束自身任务。"""
    from backend.pg_services import PostgresTaskWorkerManager

    manager = PostgresTaskWorkerManager.__new__(PostgresTaskWorkerManager)
    claim = SimpleNamespace(
        id="task-a",
        scope_id="scope-a",
        claim_generation=7,
        lease_owner="worker-a",
        lease_expires_at=object(),
    )
    settled: list[object] = []
    finished: list[tuple[str, bool]] = []
    manager._scope_service_resolver = lambda _scope: _scope_services("scope-b")
    manager._claim_for_task = lambda _task_id: claim
    manager._fail_unresolvable = lambda value: settled.append(value)
    manager._task_finished = lambda task_id, *, claimed: finished.append((task_id, claimed))
    manager._run("task-a")
    assert settled == [claim]
    assert finished == [("task-a", True)]


def test_worker_reclaim_after_delayed_factory_failure_does_not_settle_new_claim() -> None:
    """装配异常延迟到重新认领后，旧 claim 的收束不能覆盖新 generation。"""
    from backend.pg_services import PostgresTaskWorkerManager

    manager = PostgresTaskWorkerManager.__new__(PostgresTaskWorkerManager)
    first = SimpleNamespace(id="task-a", scope_id="scope-a", claim_generation=7)
    second = SimpleNamespace(id="task-a", scope_id="scope-a", claim_generation=8)
    claims = iter((first, second))
    settled: list[object] = []
    finished: list[tuple[str, bool]] = []
    executed: list[tuple[str, object]] = []

    class TaskFacade:
        """记录第二次有效 claim 的执行，避免触碰数据库。"""

        scope = ScopeContext("scope-a")

        def _run(self, task_id, *, preclaimed):
            """记录有效 claim，证明旧 claim 未被复用。"""
            executed.append((task_id, preclaimed))

    valid = _scope_services("scope-a")
    valid = ScopeServices(valid.scope, valid.metadata, valid.search, TaskFacade(), valid.reverse_image, valid.visual_search)
    manager._claim_for_task = lambda _task_id: next(claims)
    resolver_calls = 0

    def resolve_services(_scope):
        """第一次模拟延迟装配异常，第二次返回重新认领后的有效视图。"""
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls == 1:
            raise RuntimeError("late assembly")
        return valid

    manager._scope_service_resolver = resolve_services
    manager._fail_unresolvable = lambda value: settled.append(value)
    manager._task_finished = lambda task_id, *, claimed: finished.append((task_id, claimed))
    manager._run("task-a")
    manager._run("task-a")
    assert settled == [first]
    assert executed == [("task-a", second)]
    # 第二次有效 claim 的收束由 scope task facade 自己负责，manager 不能再次
    # 把它当作装配失败 claim 终止。
    assert finished == [("task-a", True)]


def test_worker_successful_claim_settlement_releases_only_matching_lane_slot() -> None:
    """有效 claim 的装配失败会释放自身槽位，fencing 不匹配时保留他人槽位。"""
    from backend.pg_services import PostgresTaskWorkerManager

    claim = SimpleNamespace(id="task-a", scope_id="scope-a", claim_generation=7)
    task = SimpleNamespace(
        id="task-a",
        scope_id="scope-a",
        status="running",
        completed_at=None,
        lease_owner="worker-a",
        lease_expires_at=object(),
        message=None,
        error=None,
    )
    slot = SimpleNamespace(
        task_scope_id="scope-a",
        task_id="task-a",
        lease_owner="worker-a",
        claim_generation=7,
        lease_expires_at=object(),
    )

    class Session:
        """按查询顺序返回当前 claim 行和其 lane 槽位。"""

        def __init__(self):
            self.calls = 0
            self.committed = False

        def scalar(self, _statement):
            """提供锁定查询的最小替身。"""
            self.calls += 1
            return task if self.calls == 1 else slot

        def commit(self):
            """记录事务收束。"""
            self.committed = True

    class Resources:
        """提供 manager 所需的短事务上下文。"""

        def __init__(self, session):
            self.session = session

        def factory(self):
            """返回同一个 fake session。"""
            class Context:
                def __enter__(inner_self):
                    return self.session

                def __exit__(inner_self, *_args):
                    return None

            return Context()

    manager = PostgresTaskWorkerManager.__new__(PostgresTaskWorkerManager)
    manager.owner = "worker-a"
    session = Session()
    manager.resources = Resources(session)
    manager._fail_unresolvable(claim)
    assert task.status == "failed"
    assert task.lease_owner is None
    assert task.lease_expires_at is None
    assert slot.task_id is None
    assert slot.lease_owner is None
    assert session.committed is True

    foreign_slot = SimpleNamespace(
        task_scope_id="scope-a",
        task_id="task-a",
        lease_owner="worker-b",
        claim_generation=8,
        lease_expires_at=object(),
    )

    class SlotSession:
        """只返回另一个 Worker 的槽位，验证 fencing 拒绝不会清空它。"""

        def scalar(self, _statement):
            """返回被保护的槽位。"""
            return foreign_slot

    assert PostgresTaskWorkerManager._release_slot(SlotSession(), "scope-a", "task-a", owner="worker-a", claim_generation=7) is False
    assert foreign_slot.task_id == "task-a"
    assert foreign_slot.lease_owner == "worker-b"
