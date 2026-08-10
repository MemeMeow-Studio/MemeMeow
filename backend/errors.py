"""统一 API 错误结构和异常处理。"""

from pydantic import BaseModel, ConfigDict


class ErrorBody(BaseModel):
    """客户端可识别的错误标识与人类可读消息。"""

    model_config = ConfigDict(extra="forbid")
    error: str
    message: str
