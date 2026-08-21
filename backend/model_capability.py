"""服务端对公共模型 capability 传输常量的导出适配。

Server 和公共 executor 共享同一组字段名与基础格式校验；长期 provider 凭据
只能由服务端 broker 读取，不通过本模块向 Agent 暴露。
"""

from executor.model_capability import (
    MODEL_BROKER_URL_ENV,
    MODEL_CAPABILITY_ENV,
    MODEL_CAPABILITY_FIELD,
    MODEL_CAPABILITY_MAX_BYTES,
    MODEL_CAPABILITY_MIN_BYTES,
    ModelCapabilityError,
    validate_model_broker_url,
    validate_model_capability,
    validate_model_name,
)

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
