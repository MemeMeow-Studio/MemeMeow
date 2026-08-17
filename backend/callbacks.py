"""Agent 内部 callback 的服务身份和当前 claim 绑定。

callback 凭据与用户认证、operation grant、executor token 和 provider secret 分离。
本模块只验证最小执行声明；业务路由仍必须从 PostgreSQL Task 记录恢复 scope、目标
SHA 和当前租约，并在副作用前完成二次校验。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Any, Mapping, Protocol

from backend.database import ScopeContext


logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class CallbackError(RuntimeError):
    """callback 边界稳定错误，不泄露任务存在性或 claim 原因。"""

    def __init__(self, code: str = "agent_callback_unauthorized") -> None:
        self.code = code if code in {"agent_callback_unauthorized", "agent_callback_invalid_execution", "agent_callback_unavailable", "agent_callback_body_too_large"} else "agent_callback_unauthorized"
        super().__init__(self.code)


class CallbackMetrics:
    """记录 callback 边界的低基数计数器。

    指标只使用固定路由和稳定错误码作为标签，不保存 task、scope、token、图片或
    provider 字段；宿主可以周期性读取 ``snapshot`` 后转发到自己的指标系统。
    """

    def __init__(self) -> None:
        """创建线程安全的 callback 计数器。"""
        self._lock = RLock()
        self._rejections: dict[tuple[str, str], int] = {}

    def record_rejection(self, path: str, code: str) -> None:
        """记录一次边界拒绝，只接受固定格式的路由和稳定错误码。"""
        if not isinstance(path, str) or not path.startswith("/internal/"):
            path = "/internal/unknown"
        if not isinstance(code, str) or not code.startswith("agent_callback_"):
            code = "agent_callback_unauthorized"
        with self._lock:
            key = (path, code)
            self._rejections[key] = self._rejections.get(key, 0) + 1

    def snapshot(self) -> dict[str, int]:
        """返回脱敏、稳定排序的计数快照。"""
        with self._lock:
            return {
                f"{path}|{code}": count
                for (path, code), count in sorted(self._rejections.items())
            }

    def reset(self) -> None:
        """清空测试或进程重新装配时的计数。"""
        with self._lock:
            self._rejections.clear()


CALLBACK_METRICS = CallbackMetrics()


@dataclass(frozen=True, slots=True)
class CallbackBinding:
    """绑定一个 Task 当前执行 claim 的最小声明。"""

    task_id: str
    scope_id: str
    claim_generation: int
    owner: str
    attempt: int
    operation: str
    target_sha256: str
    issuer: str
    audience: str
    expires_at: datetime
    key_id: str = "default"
    protocol_version: str = "1"
    nonce: str = ""

    def __post_init__(self) -> None:
        """拒绝空绑定、零 generation 和非法 SHA。"""
        ScopeContext(self.scope_id)
        if not self.task_id or not self.owner or self.claim_generation < 1 or self.attempt < 1 or not self.operation or len(self.target_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in self.target_sha256) or not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise CallbackError("agent_callback_invalid_execution")
        if not self.nonce:
            object.__setattr__(self, "nonce", secrets.token_urlsafe(12))

    def claims(self) -> dict[str, object]:
        """返回签名载荷，不包含用户或 grant 信息。"""
        return {
            "v": self.protocol_version,
            "iss": self.issuer,
            "aud": self.audience,
            "kid": self.key_id,
            "task_id": self.task_id,
            "scope_id": self.scope_id,
            "claim_generation": self.claim_generation,
            "owner": self.owner,
            "attempt": self.attempt,
            "operation": self.operation,
            "target_sha256": self.target_sha256,
            "exp": int(self.expires_at.timestamp()),
            "nonce": self.nonce,
        }

    def allows(self, operation: str) -> bool:
        """判断当前执行凭据是否声明了指定 callback operation。"""
        return operation in {item.strip() for item in self.operation.split(",") if item.strip()}

    def allows_any(self, operations: frozenset[str]) -> bool:
        """判断当前执行凭据是否覆盖 callback 注册表声明的任一 operation。"""
        return bool({item.strip() for item in self.operation.split(",") if item.strip()} & set(operations))


class CallbackIssuer(Protocol):
    """callback 凭据签发接口。"""

    def issue(self, binding: CallbackBinding) -> str:
        """签发只绑定当前执行的短期凭据。"""


class CallbackVerifier(Protocol):
    """callback 凭据验证接口。"""

    def verify(self, token: str, *, path: str | None = None) -> CallbackBinding:
        """验证服务身份、受众、过期和签名。"""


class HMACCallbackCredentials:
    """开源和测试使用的显式 HMAC issuer/verifier。

    secret 为空或过短时构造失败，避免把未配置误当作内网可信；token 采用简单
    base64url 三段结构，便于不同 Runner 传递但不把原始 secret 暴露给 Agent。
    """

    def __init__(
        self,
        secret: str | bytes | None,
        *,
        issuer: str = "mememeow",
        audience: str = "mememeow-internal",
        key_id: str = "default",
        verification_keys: Mapping[str, str | bytes] | None = None,
        ttl_seconds: int = 120,
    ) -> None:
        """创建显式 HMAC 密钥环，``verification_keys`` 用于轮换窗口。

        ``secret`` 是当前签发密钥；旧密钥只能通过验证密钥环进入，不能被新的
        callback 凭据签发。空值、控制字符和过短密钥都视为未配置并 fail-closed。
        """
        if not issuer or not audience or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", key_id) or ttl_seconds <= 0:
            raise CallbackError("agent_callback_unavailable")
        keys: dict[str, bytes] = {}
        for candidate_id, candidate in (verification_keys or {}).items():
            if not isinstance(candidate_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", candidate_id):
                raise CallbackError("agent_callback_unavailable")
            keys[candidate_id] = self._normalize_secret(candidate)
        if secret is not None:
            keys[key_id] = self._normalize_secret(secret)
        if not keys or key_id not in keys:
            raise CallbackError("agent_callback_unavailable")
        self._verification_keys = keys
        self.issuer = issuer
        self.audience = audience
        self.key_id = key_id
        self.ttl_seconds = min(int(ttl_seconds), 3600)

    @staticmethod
    def _normalize_secret(secret: str | bytes | None) -> bytes:
        """校验密钥格式并返回不可变字节串，不保留可记录的原文。"""
        if not isinstance(secret, (str, bytes)):
            raise CallbackError("agent_callback_unavailable")
        value = secret.strip().encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(value) < 16 or any(byte < 0x20 or byte == 0x7F for byte in value):
            raise CallbackError("agent_callback_unavailable")
        return value

    @staticmethod
    def _encode(value: Mapping[str, object]) -> str:
        """将无秘密 JSON 载荷编码为 base64url。"""
        return base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=").decode()

    @staticmethod
    def _decode(value: str) -> dict[str, object]:
        """解码并验证 JSON 对象形状。"""
        try:
            padded = value + "=" * (-len(value) % 4)
            result = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (binascii.Error, ValueError, TypeError, UnicodeError, json.JSONDecodeError, OverflowError) as exc:
            raise CallbackError() from exc
        if not isinstance(result, dict):
            raise CallbackError()
        return result

    def issue(self, binding: CallbackBinding) -> str:
        """签发当前 claim 绑定的短期 HMAC token。"""
        if binding.issuer != self.issuer or binding.audience != self.audience or binding.key_id != self.key_id:
            raise CallbackError("agent_callback_invalid_execution")
        if binding.expires_at <= datetime.now(UTC) or binding.expires_at > datetime.now(UTC) + timedelta(seconds=self.ttl_seconds):
            raise CallbackError("agent_callback_invalid_execution")
        claims = binding.claims()
        header = self._encode({"alg": "HS256", "typ": "MMCB", "kid": binding.key_id})
        body = self._encode(claims)
        signature = hmac.new(self._verification_keys[self.key_id], f"{header}.{body}".encode(), hashlib.sha256).digest()
        return f"{header}.{body}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str, *, path: str | None = None) -> CallbackBinding:
        """验证 token 签名、issuer/audience/过期和最小 claim 字段。"""
        if not isinstance(token, str) or len(token) > 4096:
            raise CallbackError()
        parts = token.split(".")
        if len(parts) != 3:
            raise CallbackError()
        header_text, body_text, signature_text = parts
        try:
            header = self._decode(header_text)
            key_id = header.get("kid")
            if not isinstance(key_id, str) or key_id not in self._verification_keys:
                raise CallbackError()
            expected = hmac.new(self._verification_keys[key_id], f"{header_text}.{body_text}".encode(), hashlib.sha256).digest()
            supplied = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        except (binascii.Error, ValueError, TypeError, CallbackError):
            raise CallbackError()
        if not hmac.compare_digest(expected, supplied):
            raise CallbackError()
        claims = self._decode(body_text)
        now = datetime.now(UTC)
        if header.get("alg") != "HS256" or header.get("typ") != "MMCB" or claims.get("kid") != key_id or claims.get("v") != "1" or claims.get("iss") != self.issuer or claims.get("aud") != self.audience:
            raise CallbackError()
        try:
            expires = datetime.fromtimestamp(int(claims["exp"]), UTC)
            binding = CallbackBinding(
                task_id=str(claims["task_id"]),
                scope_id=str(claims["scope_id"]),
                claim_generation=int(claims["claim_generation"]),
                owner=str(claims["owner"]),
                attempt=int(claims["attempt"]),
                operation=str(claims["operation"]),
                target_sha256=str(claims["target_sha256"]),
                issuer=str(claims["iss"]),
                audience=str(claims["aud"]),
                expires_at=expires,
                key_id=str(claims["kid"]),
                protocol_version=str(claims["v"]),
                nonce=str(claims["nonce"]),
            )
        except (KeyError, TypeError, ValueError, CallbackError) as exc:
            raise CallbackError() from exc
        if expires <= now:
            raise CallbackError()
        if path is not None and path not in {"/internal/reverse-image/search", "/internal/visual-search/match"}:
            raise CallbackError("agent_callback_invalid_execution")
        return binding


@dataclass(frozen=True, slots=True)
class CallbackRegistration:
    """一个 callback 路由的安全能力声明。"""

    path: str
    task_types: frozenset[str]
    operations: frozenset[str]
    max_body_bytes: int = 512 * 1024
    side_effect: str = "read_only"
    target_validator: str = "task"


class CallbackRegistry:
    """集中声明 callback 路由，未注册入口不得启用。"""

    def __init__(self) -> None:
        self._values: dict[str, CallbackRegistration] = {}

    def register(self, registration: CallbackRegistration) -> None:
        """保存不可变路由约束。"""
        if (
            not registration.path.startswith("/internal/")
            or not registration.task_types
            or not registration.operations
            or registration.max_body_bytes <= 0
            or registration.side_effect not in {"read_only", "provider_and_usage"}
            or not registration.target_validator
        ):
            raise CallbackError("agent_callback_unavailable")
        self._values[registration.path] = registration

    def get(self, path: str) -> CallbackRegistration | None:
        """读取路由声明。"""
        return self._values.get(path)


DEFAULT_CALLBACK_REGISTRY = CallbackRegistry()
DEFAULT_CALLBACK_REGISTRY.register(CallbackRegistration("/internal/reverse-image/search", frozenset({"meme_context_generation"}), frozenset({"analysis.reverse_image_search"}), side_effect="provider_and_usage", target_validator="task_image_sha256"))
DEFAULT_CALLBACK_REGISTRY.register(CallbackRegistration("/internal/visual-search/match", frozenset({"meme_context_generation"}), frozenset({"analysis.visual_search"}), target_validator="task_visual_embedding"))


def verify_content_length(headers: Mapping[str, str], *, limit: int) -> None:
    """在读取 request body 前拒绝声明超限的 callback 请求。"""
    raw = headers.get("content-length") or headers.get("Content-Length")
    if raw is None:
        return
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CallbackError("agent_callback_body_too_large") from exc
    if value < 0 or value > limit:
        raise CallbackError("agent_callback_body_too_large")


def binding_input_digest(*values: object) -> str:
    """生成受限输入摘要，用于 callback request id 幂等关联。"""
    return hashlib.sha256("|".join(str(value) for value in values).encode("utf-8")).hexdigest()


def validate_request_id(value: str | None) -> str | None:
    """校验 callback request id 的公开格式，拒绝空白和控制字符。"""
    if value is None:
        return None
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise CallbackError("agent_callback_invalid_execution")
    return value


def validate_input_digest(value: str | None) -> str | None:
    """校验 callback 规范化输入摘要，避免请求事实被模糊字符串改绑。"""
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise CallbackError("agent_callback_invalid_execution")
    return value


def validate_request_binding(
    request_id: str | None,
    binding: CallbackBinding,
    *,
    input_digest: str | None,
) -> tuple[str | None, str | None]:
    """校验 request id 与当前 claim 的摘要形状，返回规范化值。

    数据库 repository 负责比较已存在的完整绑定；本函数负责在进入 repository
    前拒绝空白 request id、非十六进制摘要和不完整 callback 声明。
    """
    del binding
    return validate_request_id(request_id), validate_input_digest(input_digest)


def validate_callback_headers(headers: Mapping[str, str], binding: CallbackBinding) -> str | None:
    """验证可选 Header 与签名 task/operation/request 声明一致。"""
    header_task = headers.get("x-mememeow-task-id") or headers.get("X-MemeMeow-Task-Id")
    if header_task is not None and not hmac.compare_digest(header_task, binding.task_id):
        raise CallbackError("agent_callback_invalid_execution")
    header_operation = headers.get("x-mememeow-operation") or headers.get("X-MemeMeow-Operation")
    if header_operation is not None and not binding.allows(header_operation.strip()):
        raise CallbackError("agent_callback_invalid_execution")
    header_request = headers.get("x-mememeow-request-id") or headers.get("X-MemeMeow-Request-Id")
    return validate_request_id(header_request)


def install_body_guard(request: Any, *, limit: int) -> None:
    """给已认证 callback 安装 ASGI receive 累计上限，不缓冲无限请求体。"""
    original_receive = request._receive
    total = 0

    async def guarded_receive() -> dict[str, Any]:
        """转发单个 ASGI body 消息并在累计超限时停止读取。"""
        nonlocal total
        message = await original_receive()
        if message.get("type") == "http.request":
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                raise CallbackError("agent_callback_body_too_large")
        return message

    request._receive = guarded_receive


def log_callback_rejection(path: str, error: CallbackError, *, binding: CallbackBinding | None = None) -> None:
    """记录低基数脱敏拒绝日志，不记录 token、任务、用户、scope 或图片内容。"""
    del binding
    CALLBACK_METRICS.record_rejection(path, error.code)
    logger.info("agent_callback_rejected path=%s code=%s", path, error.code)


def validate_binding_task(binding: CallbackBinding, task: object, registration: CallbackRegistration | None = None) -> ScopeContext:
    """将签名声明与当前 PostgreSQL Task claim 比较，失败不泄露具体原因。"""
    try:
        if registration is not None and getattr(task, "task_type", None) not in registration.task_types:
            raise CallbackError("agent_callback_invalid_execution")
        if registration is not None and not binding.allows_any(registration.operations):
            raise CallbackError("agent_callback_invalid_execution")
        if getattr(task, "id", None) != binding.task_id or getattr(task, "scope_id", None) != binding.scope_id:
            raise CallbackError("agent_callback_invalid_execution")
        if getattr(task, "status", None) != "running" or int(getattr(task, "claim_generation", 0)) != binding.claim_generation or getattr(task, "lease_owner", None) != binding.owner:
            raise CallbackError("agent_callback_invalid_execution")
        expires = getattr(task, "lease_expires_at", None)
        if expires is None or expires <= datetime.now(UTC) or binding.expires_at <= datetime.now(UTC) or binding.expires_at > expires:
            raise CallbackError("agent_callback_invalid_execution")
        if int(getattr(task, "attempt_count", 0)) != binding.attempt:
            raise CallbackError("agent_callback_invalid_execution")
        payload = getattr(task, "payload", None) or {}
        target = payload.get("image_sha256")
        if not isinstance(target, str) or not hmac.compare_digest(target, binding.target_sha256):
            raise CallbackError("agent_callback_invalid_execution")
    except CallbackError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一隐藏任务存在性和字段原因
        raise CallbackError("agent_callback_invalid_execution") from exc
    return ScopeContext(binding.scope_id)
