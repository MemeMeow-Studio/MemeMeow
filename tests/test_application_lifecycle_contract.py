"""公共 HTTP 应用装配与生命周期 canonical 边界合同测试。"""

from __future__ import annotations

import asyncio
import ast
from types import SimpleNamespace
from pathlib import Path

import api
import pytest
from fastapi import FastAPI

import backend.application_lifecycle as lifecycle
from backend.application import create_application
from backend.application_lifecycle import LifecycleSetup, ScopeRuntime, build_scope_runtime, prepare_lifecycle, shutdown_lifecycle
from backend.database import DatabaseError
from backend.scope import LocalScopeResolver, ScopeResolutionError


def _route_snapshot(application) -> list[tuple[str, tuple[str, ...], str, bool]]:
    """记录路由顺序和公开 schema 标志，避免装配 delegate 改变 HTTP 合同。"""
    return [
        (
            route.path,
            tuple(sorted(route.methods or ())),
            route.name,
            bool(getattr(route, "include_in_schema", False)),
        )
        for route in application.routes
        if hasattr(route, "path") and hasattr(route, "methods")
    ]


def test_public_factory_preserves_route_template_and_middleware() -> None:
    """公共 create_app 与模块级入口保持同一路由和 middleware 顺序。"""
    created = api.create_app(scope_resolver=LocalScopeResolver("local"))

    assert _route_snapshot(created) == _route_snapshot(api.app)
    assert [middleware.cls for middleware in created.user_middleware] == [middleware.cls for middleware in api.app.user_middleware]
    assert created.state.scope_resolver.scope.scope_id == "local"
    assert create_application.__module__ == "backend.application"


def test_public_factory_rejects_invalid_resolver_before_lifecycle() -> None:
    """非法 resolver 在应用工厂阶段失败，不能触碰数据库或外部资源。"""
    for value in (None, object(), LocalScopeResolver("other")):
        try:
            api.create_app(scope_resolver=value)
        except ScopeResolutionError:
            continue
        raise AssertionError("非法 resolver 未被拒绝")


def test_lifecycle_canonical_modules_do_not_import_entrypoints() -> None:
    """canonical 装配和生命周期模块保持单向依赖，避免入口循环导入。"""
    for module in (create_application, prepare_lifecycle):
        tree = ast.parse(Path(module.__code__.co_filename).read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert "api" not in imports
        assert "server_api" not in imports


def _lifecycle_settings() -> SimpleNamespace:
    """返回不触碰文件系统的生命周期测试配置。"""
    return SimpleNamespace(
        opencode_runtime_root=Path("/tmp/mememeow-test-runtime"),
        image_root=Path("/tmp/mememeow-test-images"),
        data_root=Path("/tmp/mememeow-test-data"),
        expected_database_revision="revision",
        opencode_concurrency=1,
        agent_backpressure=1,
        settings_version="test",
        worker_lease_seconds=120,
        worker_max_attempts=3,
        ensure_directories=lambda: None,
    )


def test_prepare_failure_disposes_engine_before_schema_resources_escape() -> None:
    """schema 门禁失败时，已创建 Engine 必须在原始错误返回前释放。"""
    events: list[str] = []

    class Engine:
        """记录 dispose 的 Engine 测试替身。"""

        def dispose(self) -> None:
            """记录连接池关闭。"""
            events.append("engine.dispose")

    app = FastAPI()
    app.state.scope_resolver = LocalScopeResolver("local")
    settings = _lifecycle_settings()

    def fail_schema(*_args, **_kwargs):
        """模拟 schema revision 门禁失败。"""
        raise DatabaseError("schema_revision_mismatch")

    with pytest.raises(DatabaseError, match="schema_revision_mismatch"):
        prepare_lifecycle(
            app,
            settings_loader=lambda: settings,
            engine_factory=lambda _settings: Engine(),
            database_checker=fail_schema,
        )

    assert events == ["engine.dispose"]
    assert not hasattr(app.state, "database")


def test_prepare_failure_disposes_opencode_and_engine() -> None:
    """视觉客户端构造失败时，先前取得的 OpenCode 与 Engine 仍全部收束。"""
    events: list[str] = []

    class Resource:
        """记录 shutdown 的 OpenCode 测试替身。"""

        def shutdown(self) -> None:
            """记录外部运行时关闭。"""
            events.append("opencode.shutdown")

    class Engine:
        """记录 dispose 的 Engine 测试替身。"""

        def dispose(self) -> None:
            """记录连接池关闭。"""
            events.append("engine.dispose")

    app = FastAPI()
    app.state.scope_resolver = object()
    settings = _lifecycle_settings()
    runner = Resource()

    def fail_visual(_settings):
        """模拟视觉客户端初始化失败。"""
        raise RuntimeError("visual_init_failed")

    with pytest.raises(RuntimeError, match="visual_init_failed"):
        prepare_lifecycle(
            app,
            settings_loader=lambda: settings,
            engine_factory=lambda _settings: Engine(),
            database_checker=lambda *_args, **_kwargs: None,
            database_resources_factory=lambda *_args, **_kwargs: object(),
            opencode_factory=lambda _settings: runner,
            activity_factory=lambda _root: object(),
            visual_factory=fail_visual,
        )

    assert events == ["opencode.shutdown", "engine.dispose"]


def test_scope_runtime_failure_closes_factory_worker_and_executor(monkeypatch) -> None:
    """factory.start_all 失败时，runtime 构造期间取得的后台资源必须全部关闭。"""
    events: list[str] = []

    class Executor:
        """记录线程池关闭的测试替身。"""

        def shutdown(self, *args, **kwargs) -> None:
            """记录线程池关闭参数。"""
            events.append("executor.shutdown")

    class WorkerManager:
        """记录 Worker manager 生命周期的测试替身。"""

        def __init__(self, *_args, **_kwargs) -> None:
            events.append("worker.init")

        def shutdown(self) -> None:
            """记录 manager 关闭。"""
            events.append("worker.shutdown")

    class Factory:
        """在启动扫描阶段失败的 scope factory 测试替身。"""

        def __init__(self, *_args, **_kwargs) -> None:
            events.append("factory.init")

        def start_all(self):
            """模拟恢复队列失败。"""
            events.append("factory.start")
            raise RuntimeError("worker_start_failed")

        def shutdown(self) -> None:
            """记录 factory 关闭。"""
            events.append("factory.shutdown")

    monkeypatch.setattr(lifecycle, "ThreadPoolExecutor", lambda *args, **kwargs: Executor())
    monkeypatch.setattr(lifecycle, "ScopeServiceFactory", Factory)
    app = FastAPI()
    app.state.database = object()
    app.state.operation_policy_gateway = object()
    app.state.operation_grants = object()
    settings = _lifecycle_settings()
    setup = LifecycleSetup(
        app=app,
        settings=settings,
        local_mode=False,
        configured_factory=None,
        custom_factory=False,
        configured_agent_input_provider=None,
        engine=object(),
    )

    with pytest.raises(RuntimeError, match="worker_start_failed"):
        build_scope_runtime(
            setup,
            register_handlers=lambda **_kwargs: None,
            start_services=lambda _services: None,
            task_handlers={},
            worker_manager_factory=WorkerManager,
        )

    assert events == [
        "worker.init",
        "factory.init",
        "factory.start",
        "factory.shutdown",
        "worker.shutdown",
        "executor.shutdown",
    ]
    assert not hasattr(app.state, "service_factory")


def test_shutdown_continues_after_extension_error() -> None:
    """任一扩展关闭失败时，后续扩展、Worker、线程池和 Engine 仍继续执行。"""
    events: list[str] = []

    class Extension:
        """记录扩展关闭顺序并可注入失败。"""

        def __init__(self, name: str, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def on_shutdown(self, _app) -> None:
            """记录关闭并按配置失败。"""
            events.append(self.name)
            if self.fail:
                raise RuntimeError("extension_shutdown_failed")

    class Resource:
        """记录同步关闭的运行时测试替身。"""

        def __init__(self, name: str) -> None:
            self.name = name

        def shutdown(self, *args, **kwargs) -> None:
            """记录资源关闭。"""
            events.append(self.name)

    class Engine:
        """记录 Engine dispose 的测试替身。"""

        def dispose(self) -> None:
            """记录连接池关闭。"""
            events.append("engine")

    app = FastAPI()
    app.state.opencode = Resource("opencode")
    app.state.image_processing_workers = {"local": Resource("image")}
    factory = Resource("factory")
    runtime = ScopeRuntime(factory, Resource("executor"), Resource("worker"), None, None)
    setup = LifecycleSetup(
        app=app,
        settings=_lifecycle_settings(),
        local_mode=False,
        configured_factory=None,
        custom_factory=False,
        configured_agent_input_provider=None,
        engine=Engine(),
    )
    app.state.service_factory = factory

    async def exercise() -> None:
        """执行一次关闭并让断言检查顺序。"""
        with pytest.raises(RuntimeError, match="extension_shutdown_failed"):
            await shutdown_lifecycle(
                setup,
                runtime,
                (Extension("first"), Extension("second", fail=True)),
            )

    asyncio.run(exercise())
    assert events == ["second", "first", "opencode", "image", "factory", "worker", "executor", "engine"]
