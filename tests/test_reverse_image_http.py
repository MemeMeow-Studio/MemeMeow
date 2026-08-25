"""公共反向图片 callback HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PIL import Image

import api
import backend.reverse_image_http as reverse_image_http
from backend.callbacks import DEFAULT_CALLBACK_REGISTRY, CallbackBinding
from backend.database import ScopeContext
from backend.reverse_image import ReverseImageRequest


CALLBACK_PATH = "/internal/reverse-image/search"
CALLBACK_OPERATION = "analysis.reverse_image_search"


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _image_bytes(color: str = "red") -> bytes:
    """生成可供受控裁剪使用的最小 PNG 图片。"""
    output = io.BytesIO()
    Image.new("RGB", (4, 2), color=color).save(output, format="PNG")
    return output.getvalue()


def _binding(target_sha256: str, **overrides: object) -> CallbackBinding:
    """构造一份当前 reverse-image callback claim。"""
    values: dict[str, object] = {
        "task_id": "task-reverse",
        "scope_id": "scope-a",
        "claim_generation": 4,
        "owner": "worker-a",
        "attempt": 2,
        "operation": CALLBACK_OPERATION,
        "target_sha256": target_sha256,
        "issuer": "mememeow",
        "audience": "mememeow-internal",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "key_id": "active",
        "nonce": "nonce-a",
    }
    values.update(overrides)
    return CallbackBinding(**values)


def _task(binding: CallbackBinding, **overrides: object) -> SimpleNamespace:
    """构造与 callback binding 一致的持久运行中任务事实。"""
    values: dict[str, object] = {
        "id": binding.task_id,
        "scope_id": binding.scope_id,
        "task_type": "meme_context_generation",
        "status": "running",
        "claim_generation": binding.claim_generation,
        "lease_owner": binding.owner,
        "lease_expires_at": datetime.now(UTC) + timedelta(minutes=2),
        "attempt_count": binding.attempt,
        "payload": {"meme_id": "meme-1", "image_sha256": binding.target_sha256},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Environment:
    """提供 task/Meme 读取的最小 scope-bound 数据库夹具。"""

    def __init__(self, task: object, target: object | None) -> None:
        self.tasks = SimpleNamespace(get=lambda task_id: task if getattr(task, "id", None) == task_id else None)
        self.memes = SimpleNamespace(get=lambda meme_id: target if meme_id == "meme-1" else None)

    def __enter__(self) -> "_Environment":
        """返回当前测试数据库环境。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束测试数据库环境，不吞掉异常。"""
        del exc_type, exc, traceback


class _Database:
    """记录 handler 使用的 token scope，防止测试夹具隐式改绑。"""

    def __init__(self, environment: _Environment, scope: ScopeContext) -> None:
        self.environment_value = environment
        self.scope = scope
        self.scopes: list[ScopeContext] = []

    def environment(self, scope: ScopeContext) -> _Environment:
        """记录并校验数据库 scope。"""
        self.scopes.append(scope)
        assert scope == self.scope
        return self.environment_value


def _request(binding: CallbackBinding | None, *, header_request_id: str | None = None, registration: object | None = DEFAULT_CALLBACK_REGISTRY.get(CALLBACK_PATH)) -> SimpleNamespace:
    """构造不启动 lifespan 的 callback 请求对象。"""
    state = SimpleNamespace(callback_binding=binding)
    if header_request_id is not None:
        state.callback_header_request_id = header_request_id
    app_state = SimpleNamespace(callback_registry=SimpleNamespace(get=lambda _path: registration))
    return SimpleNamespace(app=SimpleNamespace(state=app_state), state=state, url=SimpleNamespace(path=CALLBACK_PATH))


def _call(request: SimpleNamespace, binding: CallbackBinding | None, content: bytes, database: _Database, service: object, **kwargs: object) -> dict[str, object]:
    """调用新模块并注入全部宿主依赖。"""
    return asyncio.run(
        reverse_image_http.internal_reverse_image_search(
            request,
            task_id=kwargs.pop("task_id", binding.task_id if binding is not None else "task-reverse"),
            content=content,
            filename="meme.png",
            request_id=kwargs.pop("request_id", None),
            input_digest=kwargs.pop("input_digest", None),
            search_type="all",
            language="zh-cn",
            country=None,
            query=None,
            auto_crop=kwargs.pop("auto_crop", False),
            refresh=False,
            binding=lambda received: received.state.callback_binding,
            registration=lambda received: received.app.state.callback_registry.get(received.url.path),
            database=lambda _received: database,
            scope_services=lambda _received, _scope: SimpleNamespace(reverse_image=service),
            error=_error,
        )
    )


def _harness(content: bytes | None = None, *, binding: CallbackBinding | None = None, target: object | None = None, task: object | None = None, header_request_id: str | None = None, registration: object | None = DEFAULT_CALLBACK_REGISTRY.get(CALLBACK_PATH)) -> tuple[SimpleNamespace, CallbackBinding, bytes, _Database, object]:
    """组装正常 callback、任务目标和可观测 service。"""
    content = content or _image_bytes()
    binding = binding or _binding(hashlib.sha256(content).hexdigest())
    task = task or _task(binding)
    target = target or SimpleNamespace(sha256=hashlib.sha256(content).hexdigest())
    database = _Database(_Environment(task, target), ScopeContext(binding.scope_id))
    request = _request(binding, header_request_id=header_request_id, registration=registration)
    calls: list[ReverseImageRequest] = []

    class _Service:
        """保存 service 请求并返回稳定结果的夹具。"""

        def search(self, payload: ReverseImageRequest) -> dict[str, object]:
            """记录规范请求并返回其 callback 绑定信息。"""
            calls.append(payload)
            return {"ok": True}

    service = _Service()
    service.calls = calls  # type: ignore[attr-defined]
    return request, binding, content, database, service


def test_reverse_image_route_and_legacy_handler_remain_available() -> None:
    """canonical route metadata 与旧 handler 名称保持兼容。"""
    routes = [route for route in api.app.routes if getattr(route, "path", None) == CALLBACK_PATH]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == {"POST"}
    assert route.tags == ["internal"]
    assert route.name == "internal_reverse_image_search"
    assert api.internal_reverse_image_search.__name__ == "internal_reverse_image_search"


def test_reverse_image_module_keeps_one_way_dependency() -> None:
    """公共 callback 模块不得反向导入入口模块。"""
    source = Path(reverse_image_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported


@pytest.mark.parametrize("case", ["missing", "mismatch", "registration", "stale", "target"])
def test_reverse_image_rejects_invalid_binding_before_service(case: str) -> None:
    """绑定、任务 claim 或目标 SHA 失败时 reverse-image service 不得执行。"""
    request, binding, content, database, service = _harness()
    if case == "missing":
        request.state.callback_binding = None
    elif case == "mismatch":
        request, binding, content, database, service = _harness()
    elif case == "registration":
        request = _request(binding, registration=None)
    elif case == "stale":
        task = _task(binding, claim_generation=binding.claim_generation + 1)
        request, binding, content, database, service = _harness(task=task, binding=binding)
    else:
        content = _image_bytes("blue")
    call_kwargs = {"task_id": "task-other"} if case == "mismatch" else {}
    with pytest.raises(HTTPException) as caught:
        _call(request, binding, content, database, service, **call_kwargs)
    assert caught.value.status_code == (413 if case == "registration" else 401)
    expected_codes = {"agent_callback_body_too_large"} if case == "registration" else {"agent_callback_unauthorized", "agent_callback_invalid_execution"}
    assert caught.value.detail["error"] in expected_codes
    assert service.calls == []  # type: ignore[attr-defined]


def test_reverse_image_rejects_request_id_header_conflict_without_service() -> None:
    """body/header request id 冲突不能改绑内部调用。"""
    request, binding, content, database, service = _harness(header_request_id="header-id")
    with pytest.raises(HTTPException) as caught:
        _call(request, binding, content, database, service, request_id="body-id")
    assert caught.value.status_code == 401
    assert caught.value.detail["error"] == "agent_callback_invalid_execution"
    assert service.calls == []  # type: ignore[attr-defined]


def test_reverse_image_rejects_malformed_input_digest_without_service() -> None:
    """非十六进制 callback 摘要不能进入 reverse-image service。"""
    request, binding, content, database, service = _harness()
    with pytest.raises(HTTPException) as caught:
        _call(request, binding, content, database, service, input_digest="invalid-digest")
    assert caught.value.status_code == 401
    assert caught.value.detail["error"] == "agent_callback_invalid_execution"
    assert service.calls == []  # type: ignore[attr-defined]


def test_reverse_image_forwards_valid_target_and_controlled_crop() -> None:
    """目标整图验证后才执行受控裁剪，并将绑定事实转发到 service。"""
    request, binding, content, database, service = _harness()
    result = _call(request, binding, content, database, service, auto_crop=True)
    assert result == {"ok": True}
    payload = service.calls[0]  # type: ignore[attr-defined]
    assert payload.task_id == binding.task_id
    assert payload.source_image_sha256 == binding.target_sha256
    assert payload.callback_binding == binding
    assert payload.image != content


def test_reverse_image_database_error_keeps_stable_projection() -> None:
    """service 数据库冲突只投影稳定 code 与 status，不暴露内部正文。"""
    request, binding, content, database, _service = _harness()

    class _FailingService:
        """抛出稳定数据库错误的 service 夹具。"""

        def search(self, _payload: ReverseImageRequest) -> dict[str, object]:
            """模拟 callback request 冲突。"""
            raise __import__("backend.database", fromlist=["DatabaseError"]).DatabaseError("callback_request_conflict", "private")

    with pytest.raises(HTTPException) as caught:
        _call(request, binding, content, database, _FailingService())
    assert caught.value.status_code == 409
    assert caught.value.detail == {"error": "callback_request_conflict", "message": "反向图片请求无法完成"}
