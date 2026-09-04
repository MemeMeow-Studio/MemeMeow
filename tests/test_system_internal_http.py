"""公共 HTTP 系统与内部能力职责域契约测试。"""

from __future__ import annotations

import ast
import asyncio
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

import api
import backend.internal_capability_http as internal_capability_http
import backend.system_http as system_http
from backend.operation_policy import Operations, OperationPolicyError


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试错误。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def test_system_internal_route_snapshot_and_legacy_aliases() -> None:
    """系统和内部路径只注册一次，顺序、标签和旧导出保持稳定。"""
    paths = {
        "/",
        "/health",
        "/config",
        "/operations/availability",
        "/internal/reverse-image/search",
    }
    routes = [route for route in api.app.routes if getattr(route, "path", None) in paths]
    assert [route.path for route in routes] == [
        "/operations/availability",
        "/",
        "/health",
        "/config",
        "/internal/reverse-image/search",
    ]
    assert [(route.path, route.methods, route.tags) for route in routes] == [
        ("/operations/availability", {"GET"}, ["capabilities"]),
        ("/", {"GET"}, ["system"]),
        ("/health", {"GET"}, ["system"]),
        ("/config", {"GET"}, ["system"]),
        ("/internal/reverse-image/search", {"POST"}, ["internal"]),
    ]
    assert api.root is system_http.root
    assert api.health is system_http.health
    assert api.bind_request_scope is system_http.bind_request_scope
    assert api._error is system_http.error
    assert sum(route.path == "/operations/availability" for route in api.app.routes) == 1
    assert sum(route.path == "/internal/reverse-image/search" for route in api.app.routes) == 1


def test_canonical_system_modules_do_not_import_entrypoints() -> None:
    """canonical 模块不能反向导入公共或 Server 入口。"""
    for module in (system_http, internal_capability_http):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "api" not in imported
        assert "server_api" not in imported


def test_internal_reverse_image_rejects_binding_before_upload_read() -> None:
    """缺少或跨 task binding 时 callback body reader 不得执行。"""
    calls: list[str] = []

    class Image:
        """记录 UploadFile.read 是否被调用的替身。"""

        filename = "image.png"

        async def read(self) -> bytes:
            """记录一次 body read 并返回测试内容。"""
            calls.append("read")
            return b"image"

    request = SimpleNamespace(
        state=SimpleNamespace(callback_binding=None),
        app=SimpleNamespace(state=SimpleNamespace()),
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            internal_capability_http.internal_reverse_image_search(
                request,
                task_id="task-1",
                image=Image(),
                request_id=None,
                input_digest=None,
                search_type="all",
                language="zh-cn",
                country=None,
                query=None,
                auto_crop=False,
                refresh=False,
                binding=lambda received: received.state.callback_binding,
                registration=lambda _received: SimpleNamespace(max_body_bytes=1024),
                database=lambda _received: None,
                scope_services=lambda _received, _scope: None,
                error=_error,
            )
        )
    assert caught.value.status_code == 401
    assert calls == []


def test_operation_availability_uses_probe_only_and_preserves_reason() -> None:
    """能力查询只调用 probe，不建立或修改计量 grant。"""
    operation = sorted(Operations.ALL)[0]
    calls: list[str] = []

    class Decision:
        """返回稳定受限结果的 policy decision 替身。"""

        allowed = False
        reason = "operation_forbidden"
        retry_at = None

    class Gateway:
        """只允许测试观察 request/probe 的 gateway 替身。"""

        def request(self, scope, name: str, key: str):
            """记录 scope、operation 和 probe key。"""
            calls.append(f"request:{scope}:{name}:{key}")
            return object()

        def probe(self, request):
            """记录 probe 调用并返回受限 decision。"""
            calls.append("probe")
            return Decision()

        def acquire(self, *_args, **_kwargs):
            """能力查询不应调用 reservation acquire。"""
            raise AssertionError("availability must not acquire")

    request = SimpleNamespace()
    result = asyncio.run(
        internal_capability_http.operation_availability(
            request,
            operation,
            request_scope=lambda _request: "scope-a",
            operation_gateway=lambda _request: Gateway(),
            error=_error,
        )
    )
    assert result == {"items": [{"operation": operation, "available": False, "reason": "operation_forbidden"}]}
    assert calls == [f"request:scope-a:{operation}:probe:{operation}", "probe"]


def test_operation_availability_rejects_unknown_operation_without_gateway() -> None:
    """未知 operation 在访问 policy gateway 前返回稳定 400。"""
    called: list[bool] = []
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            internal_capability_http.operation_availability(
                SimpleNamespace(),
                "unknown-operation",
                request_scope=lambda _request: "scope-a",
                operation_gateway=lambda _request: called.append(True),
                error=_error,
            )
        )
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "operation_unknown"
    assert called == []


def test_health_projection_does_not_expose_visual_diagnostic_or_storage_paths() -> None:
    """健康响应只保留固定布尔/计数字段。"""
    request = SimpleNamespace(
        app=SimpleNamespace(
            version="2.0.0",
            state=SimpleNamespace(
                service_factory=object(),
                settings=SimpleNamespace(agent_resume_enabled=True),
                visual_inference=SimpleNamespace(health=lambda: {"available": False, "error": "/srv/private/weights.bin"}),
                storage_preflight={"orphan_files": ["/srv/private/orphan.png"], "missing_files": ["meme-1"]},
            )
        )
    )
    payload = asyncio.run(system_http.health(request))
    assert payload == {
        "status": "ok",
        "visual_available": False,
        "agent_resume_enabled": True,
        "storage_preflight": {"status": "warning", "orphan_files": 1, "blocking_errors": {"missing_files": 1}},
    }
    assert "/srv/private" not in repr(payload)
