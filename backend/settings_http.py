"""公共核心 Settings HTTP 边界。

该模块位于 FastAPI 路由模板与配置领域之间，集中负责后端 Settings 的请求模型、
脱敏状态投影、管理凭据校验、dotenv 并发更新编排和 canonical/legacy 路由注册。
它不依赖应用入口 ``api.py``，以保持公共核心路由与应用装配之间的单向依赖。
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictInt

from backend.config import Settings, update_dotenv_concurrency
from backend.scope import ScopeServices


class _SettingsRequestModel(BaseModel):
    """Settings HTTP 请求模型基类，拒绝客户端提交未定义字段。"""

    model_config = ConfigDict(extra="forbid")


class ConcurrencyUpdateRequest(_SettingsRequestModel):
    """后端设置页唯一允许持久化的安全参数。"""

    # 公共请求模型只要求正整数；Agent 队列不设置有限容量门禁。
    opencode_concurrency: StrictInt = Field(
        ge=1,
        validation_alias=AliasChoices("opencode_concurrency", "agent_concurrency", "concurrency", "value"),
    )


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造 Settings HTTP 使用的统一错误异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _backend_settings_status(request: Request) -> dict[str, object]:
    """构造设置页脱敏状态，运行时探针仅返回布尔和固定标识。"""
    settings: Settings = request.app.state.settings
    runner: Any = request.app.state.opencode
    services = getattr(request.state, "services", None)
    engine = services.search if isinstance(services, ScopeServices) else getattr(request.app.state, "search_engine", None)
    status = settings.backend_status(
        cache_ready=bool(engine and engine.has_cache()),
        runtime_ready=bool(runner.runtime_probe().get("verified")),
    )
    visual_client = getattr(request.app.state, "visual_inference", None)
    if visual_client is not None:
        status.setdefault("readonly", {})["visual_available"] = bool(visual_client.health().get("available"))
        status.setdefault("read_only", {})["visual_available"] = bool(visual_client.health().get("available"))
    return status


def _settings_token(
    x_settings_admin_token: str | None,
    x_mememeow_settings_token: str | None,
    authorization: str | None,
) -> str | None:
    """按既有优先级提取 Settings 管理 token，兼容旧 Header 和 Bearer。"""
    token = x_settings_admin_token or x_mememeow_settings_token
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    return token


def _authorize_settings(request: Request, token: str | None) -> None:
    """验证设置管理凭据；未启用或错误凭据统一返回 403。"""
    configured = request.app.state.settings.settings_admin_token
    if not configured or not token or not secrets.compare_digest(str(token), str(configured)):
        raise _error(403, "settings_forbidden", "后端设置管理未授权")


async def _update_backend_settings(request: Request, payload: ConcurrencyUpdateRequest, token: str | None) -> dict[str, object]:
    """授权后原子更新 dotenv 的并发字段，当前进程只返回待重启状态。"""
    _authorize_settings(request, token)
    settings: Settings = request.app.state.settings
    if os.environ.get("MEMEMEOW_OPENCODE_CONCURRENCY") is not None:
        raise _error(409, "settings_environment_override", "并发数量由进程环境变量覆盖，不能写入 .env")
    try:
        update_dotenv_concurrency(
            settings.dotenv_path,
            payload.opencode_concurrency,
        )
    except ValueError as exc:
        raise _error(400, "settings_update_invalid", str(exc)) from exc
    except OSError as exc:
        raise _error(409, "settings_update_failed", "配置文件无法安全更新") from exc
    result = _backend_settings_status(request)
    result["saved"] = True
    result["restart_required"] = payload.opencode_concurrency != settings.opencode_concurrency
    result["pending"] = {"opencode_concurrency": payload.opencode_concurrency}
    return result


settings_router = APIRouter()


@settings_router.get("/backend/settings", tags=["system"])
@settings_router.get("/settings", tags=["system"], include_in_schema=False)
async def backend_settings(request: Request) -> dict[str, object]:
    """返回后端设置三类字段及当前/待重启配置。"""
    return _backend_settings_status(request)


@settings_router.patch("/backend/settings", tags=["system"])
@settings_router.patch("/settings", tags=["system"], include_in_schema=False)
async def update_backend_settings(
    request: Request,
    payload: ConcurrencyUpdateRequest,
    x_settings_admin_token: str | None = Header(default=None),
    x_mememeow_settings_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """受保护更新接口；只接受 Agent 并发数量。"""
    token = _settings_token(x_settings_admin_token, x_mememeow_settings_token, authorization)
    return await _update_backend_settings(request, payload, token)


@settings_router.post("/backend/settings", tags=["system"], include_in_schema=False)
@settings_router.post("/backend/settings/concurrency", tags=["system"])
async def update_backend_concurrency(
    request: Request,
    payload: ConcurrencyUpdateRequest,
    x_settings_admin_token: str | None = Header(default=None),
    x_mememeow_settings_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """兼容设置页显式并发路径的受保护更新接口。"""
    token = _settings_token(x_settings_admin_token, x_mememeow_settings_token, authorization)
    return await _update_backend_settings(request, payload, token)


__all__ = [
    "ConcurrencyUpdateRequest",
    "backend_settings",
    "settings_router",
    "update_backend_concurrency",
    "update_backend_settings",
]
