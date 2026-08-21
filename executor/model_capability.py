"""Agent 模型 broker 的公共连接协议。

该模块随 executor 镜像发布，位于 Agent executor 与宿主适配层之间，只校验
broker 地址和短期 capability 的传输格式。长期 provider 凭据不属于本协议，
也不应出现在 executor 请求、OpenCode 子进程环境或任务结果中。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


MODEL_BROKER_URL_ENV = "MEMEMEOW_MODEL_BROKER_URL"
MODEL_CAPABILITY_ENV = "MEMEMEOW_MODEL_CAPABILITY"
MODEL_CAPABILITY_FIELD = "model_capability"
MODEL_CAPABILITY_MAX_BYTES = 8192
MODEL_CAPABILITY_MIN_BYTES = 16
MODEL_BROKER_URL_MAX_BYTES = 2048
_MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class ModelCapabilityError(ValueError):
    """模型 broker 连接或任务 capability 不符合公共协议时的稳定错误。"""


def validate_model_broker_url(value: object) -> str:
    """校验固定 broker endpoint 并返回去除首尾空白的 URL。

    输入来自受信部署配置，输出只允许 HTTP(S) 且不含用户信息、查询参数或
    片段的 endpoint；executor 在启动和每次子进程装配前调用，避免 token 被
    重定向到配置之外的目标。
    """
    if not isinstance(value, str):
        raise ModelCapabilityError("model_broker_endpoint_invalid")
    endpoint = value.strip().rstrip("/")
    if not endpoint or len(endpoint.encode("utf-8")) > MODEL_BROKER_URL_MAX_BYTES:
        raise ModelCapabilityError("model_broker_endpoint_invalid")
    try:
        parsed = urlsplit(endpoint)
    except ValueError as exc:
        raise ModelCapabilityError("model_broker_endpoint_invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ModelCapabilityError("model_broker_endpoint_invalid")
    if parsed.query or parsed.fragment:
        raise ModelCapabilityError("model_broker_endpoint_invalid")
    try:
        parsed.port
    except ValueError as exc:
        raise ModelCapabilityError("model_broker_endpoint_invalid") from exc
    return endpoint


def validate_model_capability(value: object) -> str:
    """校验短期 capability 的传输边界，不解析或扩展其 claims。

    capability 是由 Server broker 签发的 opaque 值；executor 只负责把它原样
    传给 broker，并拒绝空值、控制字符和超长输入，避免把客户端数据当作长期
    provider 密钥或环境变量语法。
    """
    if not isinstance(value, str):
        raise ModelCapabilityError("model_capability_invalid")
    capability = value.strip()
    size = len(capability.encode("utf-8"))
    if size < MODEL_CAPABILITY_MIN_BYTES or size > MODEL_CAPABILITY_MAX_BYTES:
        raise ModelCapabilityError("model_capability_invalid")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in capability):
        raise ModelCapabilityError("model_capability_invalid")
    return capability


def validate_model_name(value: object) -> str:
    """校验部署固定的 OpenCode model 标识，拒绝命令参数注入。"""
    if not isinstance(value, str):
        raise ModelCapabilityError("model_name_invalid")
    model = value.strip()
    if not _MODEL_NAME_RE.fullmatch(model):
        raise ModelCapabilityError("model_name_invalid")
    return model


__all__ = [
    "MODEL_BROKER_URL_ENV",
    "MODEL_CAPABILITY_ENV",
    "MODEL_CAPABILITY_FIELD",
    "MODEL_CAPABILITY_MAX_BYTES",
    "MODEL_CAPABILITY_MIN_BYTES",
    "ModelCapabilityError",
    "validate_model_broker_url",
    "validate_model_capability",
    "validate_model_name",
]
