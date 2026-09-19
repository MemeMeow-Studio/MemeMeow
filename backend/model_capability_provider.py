"""模型 broker 短期 capability 的宿主扩展接口。

公共运行器只组装当前执行 attempt 的可信事实。具体签发、有效期和 broker 计量
由宿主提供，客户端任务数据不能提供或替换 capability。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Protocol

from executor.model_capability import ModelCapabilityError, validate_model_capability


@dataclass(frozen=True)
class ModelCapabilityRequest:
    """保存 capability 签发所需的当前 attempt 事实，供 Runner 调用宿主。"""

    task_id: str
    attempt_id: str
    scope_id: str
    model: str
    variant: str
    session_id: str | None
    resume_of_attempt_id: str | None
    analysis_policy: Mapping[str, object] | None
    observed_cost: str | None


class ModelCapabilityProvider(Protocol):
    """宿主实现的短期模型 capability 签发接口。"""

    def capability(self, request: ModelCapabilityRequest) -> str:
        """按当前 attempt、模型及恢复金额签发 opaque capability。"""


class ModelCapabilityProviderError(RuntimeError):
    """保存 provider 可公开的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """初始化 provider 错误；消息不得包含签名材料或 capability。"""
        self.code = code
        super().__init__(message or code)


def normalize_observed_cost(value: object) -> str | None:
    """校验恢复金额并返回十进制定点文本，供 capability claims 使用。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ModelCapabilityProviderError("agent_analysis_usage_unavailable", "恢复金额无效")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ModelCapabilityProviderError("agent_analysis_usage_unavailable", "恢复金额无效") from exc
    if not amount.is_finite() or amount < 0:
        raise ModelCapabilityProviderError("agent_analysis_usage_unavailable", "恢复金额无效")
    return format(amount, "f")


def capability_for_model_provider(provider: object, request: ModelCapabilityRequest) -> str:
    """调用宿主 provider 并校验返回值的公共传输边界。"""
    capability = getattr(provider, "capability", None)
    if not callable(capability):
        raise ModelCapabilityProviderError("model_capability_unavailable", "模型 capability provider 无效")
    try:
        value = capability(request)
        return validate_model_capability(value)
    except ModelCapabilityProviderError:
        raise
    except ModelCapabilityError as exc:
        raise ModelCapabilityProviderError(str(exc), "模型 capability 无效") from exc
    except Exception as exc:
        raise ModelCapabilityProviderError("model_capability_unavailable", "模型 capability 签发失败") from exc


__all__ = [
    "ModelCapabilityProvider",
    "ModelCapabilityProviderError",
    "ModelCapabilityRequest",
    "capability_for_model_provider",
    "normalize_observed_cost",
]
