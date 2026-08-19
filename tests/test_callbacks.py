"""Agent callback 凭据、请求绑定和 ASGI 读取边界测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware

from api import bind_request_scope
from backend.callbacks import (
    AGENT_CALLBACK_TOKEN_TTL_SECONDS,
    CALLBACK_METRICS,
    DEFAULT_CALLBACK_REGISTRY,
    CallbackBinding,
    CallbackError,
    HMACCallbackCredentials,
    CallbackRegistration,
    install_body_guard,
    validate_binding_task,
    validate_callback_headers,
    validate_input_digest,
    validate_request_id,
    validate_request_binding,
)


def _binding(*, key_id: str = "active", expires_in: int = 30) -> CallbackBinding:
    """构造一份短期、非零 claim 的 callback 执行绑定。"""
    return CallbackBinding(
        task_id="task-a",
        scope_id="scope-a",
        claim_generation=4,
        owner="worker-a",
        attempt=2,
        operation="analysis.reverse_image_search,analysis.visual_search",
        target_sha256="a" * 64,
        issuer="mememeow",
        audience="mememeow-internal",
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        key_id=key_id,
        nonce="request-nonce",
    )


def test_callback_hmac_fails_closed_and_accepts_only_explicit_rotation_keys() -> None:
    """空/短根 secret 拒绝，轮换窗口只允许声明过的旧 key 验证。"""
    with pytest.raises(CallbackError) as missing:
        HMACCallbackCredentials(None)
    assert missing.value.code == "agent_callback_unavailable"
    with pytest.raises(CallbackError):
        HMACCallbackCredentials("too-short")

    old = "old-callback-secret-1234"
    active = "active-callback-secret-5678"
    verifier = HMACCallbackCredentials(active, key_id="active", verification_keys={"old": old})
    current_token = verifier.issue(_binding())
    assert verifier.verify(current_token, path="/internal/reverse-image/search").claim_generation == 4

    old_issuer = HMACCallbackCredentials(old, key_id="old", ttl_seconds=120)
    old_token = old_issuer.issue(_binding(key_id="old"))
    assert verifier.verify(old_token).key_id == "old"

    unknown_issuer = HMACCallbackCredentials("unknown-callback-secret-9999", key_id="unknown")
    with pytest.raises(CallbackError):
        verifier.verify(unknown_issuer.issue(_binding(key_id="unknown")))


def test_callback_binding_rejects_expired_and_wrong_path_tokens() -> None:
    """过期 token 和非注册 callback 路径不能被当作服务身份。"""
    credentials = HMACCallbackCredentials("active-callback-secret-5678", key_id="active")
    with pytest.raises(CallbackError):
        credentials.issue(_binding(expires_in=-1))
    token = credentials.issue(_binding())
    with pytest.raises(CallbackError, match="agent_callback"):
        credentials.verify(token, path="/internal/unknown")


def test_callback_hmac_default_lifetime_is_two_hours() -> None:
    """默认签发器接受两小时窗口，并拒绝越过统一上限的绑定。"""
    credentials = HMACCallbackCredentials("active-callback-secret-5678", key_id="active")
    token = credentials.issue(_binding(expires_in=AGENT_CALLBACK_TOKEN_TTL_SECONDS - 1))
    verified = credentials.verify(token)
    assert verified.expires_at > datetime.now(UTC) + timedelta(hours=1, minutes=59)

    with pytest.raises(CallbackError) as error:
        credentials.issue(_binding(expires_in=AGENT_CALLBACK_TOKEN_TTL_SECONDS + 1))
    assert error.value.code == "agent_callback_invalid_execution"


def test_callback_hmac_verifier_rejects_expiration_beyond_two_hours() -> None:
    """验证端独立拒绝有效期超过两小时的正确签名凭据。"""
    secret = b"active-callback-secret-5678"
    credentials = HMACCallbackCredentials(secret, key_id="active")
    binding = _binding(expires_in=AGENT_CALLBACK_TOKEN_TTL_SECONDS + 60)
    header = credentials._encode({"alg": "HS256", "typ": "MMCB", "kid": "active"})
    body = credentials._encode(binding.claims())
    signature = hmac.new(secret, f"{header}.{body}".encode(), hashlib.sha256).digest()
    token = f"{header}.{body}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    with pytest.raises(CallbackError) as error:
        credentials.verify(token)
    assert error.value.code == "agent_callback_unauthorized"


def test_callback_header_and_request_id_binding_is_strict() -> None:
    """Header 只能重复声明签名事实，request id 格式和冲突必须拒绝。"""
    binding = _binding()
    assert validate_callback_headers(
        {
            "x-mememeow-task-id": "task-a",
            "x-mememeow-operation": "analysis.reverse_image_search",
            "x-mememeow-request-id": "request-1",
        },
        binding,
    ) == "request-1"
    assert validate_request_id(None) is None
    with pytest.raises(CallbackError):
        validate_callback_headers({"x-mememeow-task-id": "task-b"}, binding)
    with pytest.raises(CallbackError):
        validate_callback_headers({"x-mememeow-operation": "image.delete"}, binding)
    with pytest.raises(CallbackError):
        validate_request_id("request with spaces")


def test_callback_request_binding_and_metrics_are_restricted() -> None:
    """request id/摘要只接受稳定格式，指标与日志不保存任务或 secret。"""
    binding = _binding()
    digest = "a" * 64
    assert validate_request_binding("request-1", binding, input_digest=digest) == ("request-1", digest)
    assert validate_input_digest(None) is None
    with pytest.raises(CallbackError):
        validate_input_digest("not-a-digest")
    CALLBACK_METRICS.reset()
    error = CallbackError("agent_callback_invalid_execution")
    from backend.callbacks import log_callback_rejection

    log_callback_rejection("/internal/reverse-image/search", error, binding=binding)
    snapshot = CALLBACK_METRICS.snapshot()
    assert snapshot["/internal/reverse-image/search|agent_callback_invalid_execution"] == 1
    assert binding.task_id not in str(snapshot)
    assert binding.scope_id not in str(snapshot)


def test_callback_body_guard_stops_chunked_body_without_content_length() -> None:
    """无 Content-Length 时按 ASGI chunk 累计，超限后不再继续读取。"""
    messages = iter(
        (
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": True},
            {"type": "http.request", "body": b"never-read", "more_body": False},
        )
    )

    async def receive():
        """返回测试 ASGI body 分块。"""
        return next(messages)

    request = SimpleNamespace(_receive=receive)
    install_body_guard(request, limit=3)
    assert asyncio.run(request._receive())["body"] == b"ab"
    with pytest.raises(CallbackError) as error:
        asyncio.run(request._receive())
    assert error.value.code == "agent_callback_body_too_large"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("id", "task-b"),
        ("scope_id", "scope-b"),
        ("status", "succeeded"),
        ("claim_generation", 3),
        ("lease_owner", "worker-b"),
        ("attempt_count", 1),
        ("payload", {"image_sha256": "b" * 64}),
    ),
)
def test_validate_binding_task_rejects_claim_and_target_rebinding(field: str, value: object) -> None:
    """Task claim、attempt 或图片目标任一改绑都必须统一拒绝。"""
    binding = _binding()
    task = SimpleNamespace(
        id=binding.task_id,
        scope_id=binding.scope_id,
        task_type="meme_context_generation",
        status="running",
        claim_generation=binding.claim_generation,
        lease_owner=binding.owner,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        attempt_count=binding.attempt,
        payload={"image_sha256": binding.target_sha256},
    )
    setattr(task, field, value)
    registration = CallbackRegistration(
        "/internal/reverse-image/search",
        frozenset({"meme_context_generation"}),
        frozenset({"analysis.reverse_image_search"}),
    )
    with pytest.raises(CallbackError) as error:
        validate_binding_task(binding, task, registration)
    assert error.value.code == "agent_callback_invalid_execution"


def test_validate_binding_task_accepts_current_claim_only() -> None:
    """当前运行 claim、attempt 和目标 SHA 一致时才返回可信 scope。"""
    binding = _binding()
    task = SimpleNamespace(
        id=binding.task_id,
        scope_id=binding.scope_id,
        task_type="meme_context_generation",
        status="running",
        claim_generation=binding.claim_generation,
        lease_owner=binding.owner,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        attempt_count=binding.attempt,
        payload={"image_sha256": binding.target_sha256},
    )
    registration = CallbackRegistration(
        "/internal/reverse-image/search",
        frozenset({"meme_context_generation"}),
        frozenset({"analysis.reverse_image_search"}),
    )
    assert validate_binding_task(binding, task, registration).scope_id == binding.scope_id


def test_validate_binding_task_accepts_long_token_while_claim_lease_is_current() -> None:
    """两小时 token 可越过本次 lease，但调用时的当前 claim 必须仍然有效。"""
    binding = _binding(expires_in=AGENT_CALLBACK_TOKEN_TTL_SECONDS - 1)
    task = SimpleNamespace(
        id=binding.task_id,
        scope_id=binding.scope_id,
        task_type="meme_context_generation",
        status="running",
        claim_generation=binding.claim_generation,
        lease_owner=binding.owner,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=5),
        attempt_count=binding.attempt,
        payload={"image_sha256": binding.target_sha256},
    )
    registration = CallbackRegistration(
        "/internal/reverse-image/search",
        frozenset({"meme_context_generation"}),
        frozenset({"analysis.reverse_image_search"}),
    )
    assert validate_binding_task(binding, task, registration).scope_id == binding.scope_id

    task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(CallbackError) as error:
        validate_binding_task(binding, task, registration)
    assert error.value.code == "agent_callback_invalid_execution"


def test_callback_asgi_authenticates_before_body_and_scope_factory() -> None:
    """真实 ASGI 链路在认证前不读取 body，也不查询 Task 或装配 scope。"""
    application = FastAPI()
    application.add_middleware(BaseHTTPMiddleware, dispatch=bind_request_scope)
    credentials = HMACCallbackCredentials("active-callback-secret-5678", key_id="active")
    token = credentials.issue(_binding())
    application.state.callback_verifier = credentials
    application.state.callback_registry = DEFAULT_CALLBACK_REGISTRY
    calls = {"body": 0, "factory": 0}

    class Factory:
        """记录 callback 通过认证后才会发生的 scope 装配。"""

        def for_task(self, task_id: str):
            """记录调用；本测试的超限请求不应进入这里。"""
            calls["factory"] += 1
            return task_id

    application.state.service_factory = Factory()

    @application.post("/internal/reverse-image/search")
    async def endpoint(request: Request):
        """记录业务层是否读到请求正文。"""
        calls["body"] += 1
        await request.body()
        return {"ok": True}

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        """发送未认证和已认证超限请求。"""
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthenticated = await client.post("/internal/reverse-image/search", content=b"x" * 600_000)
            authenticated = await client.post(
                "/internal/reverse-image/search",
                content=b"x" * 600_000,
                headers={"X-MemeMeow-Callback": token},
            )
        return unauthenticated, authenticated

    unauthenticated, authenticated = asyncio.run(exercise())
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"] == "agent_callback_unauthorized"
    assert authenticated.status_code == 413
    assert authenticated.json()["error"] == "agent_callback_body_too_large"
    assert calls == {"body": 0, "factory": 0}
