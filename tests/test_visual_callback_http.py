"""公共视觉匹配 callback HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import api
import backend.visual_callback_http as visual_callback_http
from backend.callbacks import (
    DEFAULT_CALLBACK_REGISTRY,
    CallbackBinding,
    binding_input_digest,
)
from backend.database import (
    DatabaseError,
    InMemoryAgentCallbackRequestRepository,
    ScopeContext,
)
from backend.visual import VisualSearchError


TARGET_SHA256 = "a" * 64
CALLBACK_PATH = "/internal/visual-search/match"
CALLBACK_OPERATION = "analysis.visual_search"


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _binding(**overrides: object) -> CallbackBinding:
    """构造一份通过签名字段校验的视觉 callback 绑定。"""
    values: dict[str, object] = {
        "task_id": "task-visual",
        "scope_id": "scope-a",
        "claim_generation": 4,
        "owner": "worker-a",
        "attempt": 2,
        "operation": CALLBACK_OPERATION,
        "target_sha256": TARGET_SHA256,
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
        "payload": {"image_sha256": binding.target_sha256},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Session:
    """记录 handler 在调用视觉 service 前提交 started 事实的最小事务夹具。"""

    def __init__(self) -> None:
        self.commit_count = 0

    def commit(self) -> None:
        """记录一次事务提交。"""
        self.commit_count += 1


class _Environment:
    """复用同一 scope 内任务和 callback repository 的数据库环境夹具。"""

    def __init__(self, task: object, repository: InMemoryAgentCallbackRequestRepository) -> None:
        self.tasks = SimpleNamespace(get=lambda task_id: task if getattr(task, "id", None) == task_id else None)
        self.callback_requests = repository
        self.uow = SimpleNamespace(session=_Session())

    def __enter__(self) -> "_Environment":
        """返回当前 scope 环境。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束测试环境，不额外吞掉异常。"""
        del exc_type, exc, traceback


class _Database:
    """只允许 handler 以 token 声明的 scope 打开测试环境。"""

    def __init__(self, environment: _Environment, scope: ScopeContext) -> None:
        self.environment_value = environment
        self.scope = scope
        self.scopes: list[ScopeContext] = []

    def environment(self, scope: ScopeContext) -> _Environment:
        """记录 scope 解析并返回绑定的环境。"""
        self.scopes.append(scope)
        assert scope == self.scope
        return self.environment_value


def _request(
    binding: CallbackBinding | None,
    *,
    registration: object | None = DEFAULT_CALLBACK_REGISTRY.get(CALLBACK_PATH),
    header_request_id: str | None = None,
) -> SimpleNamespace:
    """构造不启动 lifespan 的最小 callback 请求对象。"""
    state = SimpleNamespace(callback_binding=binding)
    if header_request_id is not None:
        state.callback_header_request_id = header_request_id
    app_state = SimpleNamespace(callback_registry=SimpleNamespace(get=lambda _path: registration))
    return SimpleNamespace(
        app=SimpleNamespace(state=app_state),
        state=state,
        url=SimpleNamespace(path=CALLBACK_PATH),
    )


def _harness(
    binding: CallbackBinding | None = None,
    *,
    task: object | None = None,
    service: object | None = None,
    header_request_id: str | None = None,
    registration: object | None = DEFAULT_CALLBACK_REGISTRY.get(CALLBACK_PATH),
) -> tuple[SimpleNamespace, _Database, _Environment, object]:
    """组装一个可观测的 callback handler 测试边界。"""
    binding = binding or _binding()
    repository = InMemoryAgentCallbackRequestRepository(binding.scope_id)
    environment = _Environment(task or _task(binding), repository)
    database = _Database(environment, ScopeContext(binding.scope_id))
    request = _request(binding, registration=registration, header_request_id=header_request_id)
    visual_service = service or SimpleNamespace(match=lambda **_kwargs: {"results": []})
    return request, database, environment, visual_service


def _call(
    request: SimpleNamespace,
    payload: visual_callback_http.VisualMatchRequest,
    database: _Database,
    service: object,
) -> dict[str, object]:
    """调用新模块 handler 并注入当前测试边界的全部宿主依赖。"""
    return asyncio.run(
        visual_callback_http.internal_visual_search_match(
            request,
            payload,
            binding=lambda received: received.state.callback_binding,
            registration=lambda received: received.app.state.callback_registry.get(received.url.path),
            database=lambda _received: database,
            scope_services=lambda _received, _scope: SimpleNamespace(visual_search=service),
            error=_error,
        )
    )


def _digest(binding: CallbackBinding, payload: visual_callback_http.VisualMatchRequest) -> str:
    """按 handler 固定字段计算 callback fact 的输入摘要。"""
    return binding_input_digest(
        binding.task_id,
        binding.scope_id,
        binding.claim_generation,
        binding.attempt,
        CALLBACK_OPERATION,
        binding.target_sha256,
        payload.top_k,
        payload.exclude_self,
    )


def test_visual_callback_routes_and_legacy_names_remain_available() -> None:
    """canonical route 的方法、标签和旧模型/handler 名称保持兼容。"""
    routes = [route for route in api.app.routes if getattr(route, "path", None) == CALLBACK_PATH]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == {"POST"}
    assert route.tags == ["internal"]
    assert route.name == "internal_visual_search_match"
    assert api.VisualMatchRequest is visual_callback_http.VisualMatchRequest
    assert api.internal_visual_search_match.__name__ == "internal_visual_search_match"


def test_visual_callback_module_keeps_one_way_dependency_and_strict_request_model() -> None:
    """callback 模块不反向导入入口，模型拒绝 scope/path 等客户端事实。"""
    source = Path(visual_callback_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
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
    with pytest.raises(ValidationError):
        visual_callback_http.VisualMatchRequest.model_validate({"task_id": "task-visual", "scope_id": "scope-b"})
    with pytest.raises(ValidationError):
        visual_callback_http.VisualMatchRequest.model_validate({"task_id": "task-visual", "path": "/secret"})


@pytest.mark.parametrize("case", ["missing", "mismatch", "registration", "stale"])
def test_visual_callback_rejects_missing_or_stale_binding_before_service(case: str) -> None:
    """缺失、跨任务或旧 claim 在视觉 service 前统一 fail-closed。"""
    binding = _binding()
    if case == "missing":
        request, database, _environment, service = _harness(binding)
        request.state.callback_binding = None
        payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id)
    elif case == "mismatch":
        request, database, _environment, service = _harness(binding)
        payload = visual_callback_http.VisualMatchRequest(task_id="task-other")
    elif case == "registration":
        request, database, _environment, service = _harness(binding, registration=None)
        payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id)
    else:
        request, database, _environment, service = _harness(binding, task=_task(binding, claim_generation=binding.claim_generation + 1))
        payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id)

    called: list[object] = []
    service.match = lambda **_kwargs: called.append(True) or {"results": []}  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as caught:
        _call(request, payload, database, service)
    assert caught.value.status_code == 401
    assert caught.value.detail["error"] in {"agent_callback_unauthorized", "agent_callback_invalid_execution"}
    assert called == []
    if case != "stale":
        assert database.scopes == []


def test_visual_callback_rejects_header_body_request_id_conflict_without_service() -> None:
    """body 与已验证 header 声明不同的 request id 不能改绑 callback fact。"""
    binding = _binding()
    request, database, _environment, service = _harness(binding, header_request_id="header-id")
    called: list[object] = []
    service.match = lambda **_kwargs: called.append(True) or {"results": []}  # type: ignore[attr-defined]
    with pytest.raises(HTTPException) as caught:
        _call(request, visual_callback_http.VisualMatchRequest(task_id=binding.task_id, request_id="body-id"), database, service)
    assert caught.value.status_code == 401
    assert caught.value.detail["error"] == "agent_callback_invalid_execution"
    assert called == []
    assert database.scopes == []


def test_visual_callback_rejects_malformed_injected_binding_or_header() -> None:
    """绕过 middleware 的错误绑定或 header 也只能得到稳定 401。"""
    binding = _binding()
    request, database, _environment, service = _harness(binding)
    request.state.callback_binding = SimpleNamespace(task_id=binding.task_id)
    with pytest.raises(HTTPException) as binding_error:
        _call(request, visual_callback_http.VisualMatchRequest(task_id=binding.task_id), database, service)
    assert binding_error.value.status_code == 401
    assert binding_error.value.detail["error"] == "agent_callback_unauthorized"

    request, database, _environment, service = _harness(binding, header_request_id="invalid request id")
    with pytest.raises(HTTPException) as header_error:
        _call(request, visual_callback_http.VisualMatchRequest(task_id=binding.task_id), database, service)
    assert header_error.value.status_code == 401
    assert header_error.value.detail["error"] == "agent_callback_invalid_execution"


def test_visual_callback_completed_fact_replays_result_without_second_service_call() -> None:
    """已完成 callback fact 直接返回安全结果，视觉 service 不会二次执行。"""
    binding = _binding()
    request, database, environment, service = _harness(binding)
    payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id, request_id="request-1", top_k=7, exclude_self=False)
    digest = _digest(binding, payload)
    fact = environment.callback_requests.create(
        request_id=payload.request_id,
        task_id=binding.task_id,
        claim_generation=binding.claim_generation,
        attempt=binding.attempt,
        operation=CALLBACK_OPERATION,
        target_sha256=binding.target_sha256,
        input_digest=digest,
    )
    replay = {"query_meme_id": "meme-1", "results": [{"meme_id": "meme-2"}]}
    environment.callback_requests.finish(fact.request_id, state="completed", result=replay)
    calls: list[dict[str, object]] = []
    service.match = lambda **kwargs: calls.append(kwargs) or {"results": []}  # type: ignore[attr-defined]

    assert _call(request, payload, database, service) == replay
    assert calls == []
    assert environment.uow.session.commit_count == 0


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (VisualSearchError("query_embedding_not_ready", "not ready", status_code=409), 409, "query_embedding_not_ready"),
        (DatabaseError("meme_not_found"), 404, "meme_not_found"),
    ],
)
def test_visual_callback_failure_finishes_started_fact_before_projecting_error(
    raised: Exception,
    status: int,
    code: str,
) -> None:
    """视觉或数据库失败都在 started 提交后收束为 failed fact。"""
    binding = _binding()
    calls: list[dict[str, object]] = []

    class Service:
        """注入稳定业务错误并确认 started 事务已提交。"""

        def match(self, **kwargs: object) -> dict[str, object]:
            """记录匹配输入，然后抛出测试指定错误。"""
            calls.append(kwargs)
            raise raised

    request, database, environment, _service = _harness(binding, service=Service())
    payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id, request_id="failure-id")
    with pytest.raises(HTTPException) as caught:
        _call(request, payload, database, _service)
    assert caught.value.status_code == status
    assert caught.value.detail["error"] == code
    assert calls == [{"task_id": binding.task_id, "top_k": 20, "exclude_self": True}]
    fact = environment.callback_requests.get("failure-id")
    assert fact is not None
    assert fact.state == "failed"
    assert fact.completed_at is not None
    assert fact.error == {"error": code}
    assert environment.uow.session.commit_count == 1


def test_visual_callback_success_finishes_fact_and_calls_service_after_started_commit() -> None:
    """成功匹配只在 started 事实提交后调用 service，并持久 completed 结果。"""
    binding = _binding()
    calls: list[tuple[int, dict[str, object]]] = []

    class Service:
        """记录 service 调用时的事务状态。"""

        def match(self, **kwargs: object) -> dict[str, object]:
            """确认先提交 started，再返回匹配结果。"""
            calls.append((environment.uow.session.commit_count, kwargs))
            return {"query_meme_id": "meme-1", "results": []}

    request, database, environment, service = _harness(binding, service=Service())
    payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id, request_id="success-id", top_k=3, exclude_self=False)
    result = _call(request, payload, database, service)
    assert result == {"query_meme_id": "meme-1", "results": []}
    assert calls == [(1, {"task_id": binding.task_id, "top_k": 3, "exclude_self": False})]
    fact = environment.callback_requests.get("success-id")
    assert fact is not None
    assert fact.state == "completed"
    assert fact.result == result
    assert fact.completed_at is not None


def test_visual_callback_uses_canonical_id_and_persists_digest_when_request_id_is_omitted() -> None:
    """省略 request id 时使用绑定输入摘要生成确定性事实 ID。"""
    binding = _binding()
    request, database, environment, service = _harness(binding)
    payload = visual_callback_http.VisualMatchRequest(task_id=binding.task_id)
    result = _call(request, payload, database, service)
    digest = _digest(binding, payload)
    fact = environment.callback_requests.get(f"cb-{digest}")
    assert result == {"results": []}
    assert fact is not None
    assert fact.input_digest == digest
    assert fact.state == "completed"


def test_api_visual_callback_wrapper_forwards_all_injected_dependencies() -> None:
    """入口旧 handler 只负责保留路由并显式注入宿主依赖。"""
    request = SimpleNamespace()
    payload = visual_callback_http.VisualMatchRequest(task_id="task-visual")
    calls: list[dict[str, object]] = []

    async def delegated(*args: object, **kwargs: object) -> dict[str, object]:
        """记录 wrapper 的 callback 参数。"""
        assert args == (request, payload)
        calls.append(kwargs)
        return {"ok": True}

    original = api._internal_visual_search_match_http
    api._internal_visual_search_match_http = delegated
    try:
        result = asyncio.run(api.internal_visual_search_match(request, payload))
    finally:
        api._internal_visual_search_match_http = original
    assert result == {"ok": True}
    assert set(calls[0]) == {"binding", "registration", "database", "scope_services", "error"}
    assert all(callable(calls[0][name]) for name in calls[0])
