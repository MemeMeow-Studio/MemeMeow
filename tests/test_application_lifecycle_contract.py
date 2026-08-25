"""公共 HTTP 应用装配与生命周期 canonical 边界合同测试。"""

from __future__ import annotations

import ast
from pathlib import Path

import api
from backend.application import create_application
from backend.application_lifecycle import prepare_lifecycle
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
