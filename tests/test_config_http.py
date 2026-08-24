"""公共核心 `/config` HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

import api
import backend.config_http as config_http


def test_config_http_boundary_preserves_aliases_and_one_way_dependency() -> None:
    """新模块与旧入口共享摘要 aliases，且不反向依赖 api。"""
    assert api.STORAGE_PREFLIGHT_BLOCKING_KEYS is config_http.STORAGE_PREFLIGHT_BLOCKING_KEYS
    assert api._storage_preflight_summary is config_http._storage_preflight_summary
    assert api.config_status.__name__ == "config_status"

    source = Path(config_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "api" not in imported_modules
    assert "server_api" not in imported_modules


def test_config_route_snapshot_keeps_public_metadata_and_position() -> None:
    """`/config` 继续是单个公开 GET system 路由，并位于 health 之后。"""
    routes = [route for route in api.app.routes if getattr(route, "path", None) in {"/health", "/config"}]
    assert [route.path for route in routes] == ["/health", "/config"]
    config_route = routes[1]
    assert config_route.methods == {"GET"}
    assert config_route.name == "config_status"
    assert config_route.tags == ["system"]
    assert sum(getattr(route, "path", None) == "/config" for route in api.app.routes) == 1

    created = api.create_app(scope_resolver=api.LocalScopeResolver())
    created_routes = [route for route in created.routes if getattr(route, "path", None) == "/config"]
    assert len(created_routes) == 1
    assert created_routes[0].methods == {"GET"}
    assert created_routes[0].name == "config_status"


def test_config_projection_uses_callbacks_and_redacts_runtime_storage_details() -> None:
    """配置投影复用注入 callback，并只返回固定 runtime/storage 字段。"""
    runtime = SimpleNamespace(
        runtime_probe=lambda: {
            "mode": "host-runtime-slot-lock",
            "verified": True,
            "executor_running": False,
            "container_name": "must-not-leak",
            "diagnostic": "/srv/private/diagnostic",
        }
    )
    reverse = SimpleNamespace(available=True)
    app_stub = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(status=lambda: {"embedding_api_key_configured": False}),
            opencode=runtime,
            search_engine=SimpleNamespace(has_cache=lambda: True),
            reverse_image=reverse,
            expose_scope=False,
            storage_preflight={
                "orphan_files": ["/srv/private/orphan.png"],
                "missing_files": ["meme-1"],
                "non_flat_keys": ["nested"],
            },
        )
    )
    request = Request({"type": "http", "method": "GET", "path": "/config", "raw_path": b"/config", "query_string": b"", "headers": [], "app": app_stub})
    calls: list[tuple[str, str]] = []

    def request_scope(_request: Request) -> SimpleNamespace:
        """返回当前请求 scope，供投影测试确认 callback 注入。"""
        calls.append(("scope", "config"))
        return SimpleNamespace(scope_id="scope-secret")

    def service(_request: Request, name: str) -> object:
        """返回 callback 选择的 scope service。"""
        calls.append(("service", name))
        return reverse

    payload = asyncio.run(config_http.config_status(request, request_scope=request_scope, service=service))

    assert "scope_id" not in payload
    assert payload["embedding_cache_ready"] is True
    assert payload["reverse_image_available"] is True
    assert payload["runtime_ready"] is True
    assert payload["agent_runtime"] == {"mode": "host-runtime-slot-lock", "verified": True, "executor_running": False}
    assert payload["storage_preflight"] == {"status": "warning", "orphan_files": 1, "blocking_errors": {"non_flat_keys": 1, "missing_files": 1}}
    assert calls == [("service", "reverse_image")]
    assert "must-not-leak" not in repr(payload)
    assert "/srv/private" not in repr(payload)

    app_stub.state.expose_scope = True
    visible_payload = asyncio.run(config_http.config_status(request, request_scope=request_scope, service=service))
    assert visible_payload["scope_id"] == "scope-secret"
    assert calls == [("service", "reverse_image"), ("scope", "config"), ("service", "reverse_image")]
