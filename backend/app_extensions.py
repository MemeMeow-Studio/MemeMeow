"""公共应用扩展契约。

该模块位于 FastAPI 应用装配边界，供不同部署宿主注册附加路由、生命周期钩子
以及请求授权检查。契约只描述通用应用行为，不携带具体身份、会话或业务产品
语义，便于公共核心和适配宿主保持单向同步。
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅为适配器提供静态类型提示
    from fastapi import FastAPI
    from backend.database import ScopeContext
    from backend.scope import ScopeServices


class ApplicationExtension(Protocol):
    """应用宿主可选的最小扩展协议。

    实现可以只提供实际需要的钩子；未提供的钩子由应用装配层忽略。请求授权
    钩子在可信 scope 和 scope-bound services 都已建立后调用，返回值不参与
    scope 选择，失败时由应用统一收敛为拒绝响应。
    """

    def register_routes(self, app: "FastAPI") -> None:
        """向宿主应用注册附加路由，不复制公共业务路由。"""

    def scope_exempt_paths(self) -> Collection[str]:
        """返回不需要业务 scope 的精确路径或以 ``/*`` 结尾的路径前缀。"""

    async def on_startup(self, app: "FastAPI") -> None:
        """在公共数据库、factory 和运行时服务完成装配后执行启动钩子。"""

    async def on_shutdown(self, app: "FastAPI") -> None:
        """在公共运行时资源关闭前执行清理钩子。"""

    async def authorize_request(self, request: Any, scope: "ScopeContext", services: "ScopeServices") -> None:
        """在业务 handler 前对已解析 scope 的请求执行宿主授权检查。"""

    async def authorize_exempt_request(self, request: Any) -> None:
        """在 scope 豁免路径进入 handler 前执行独立请求授权检查。"""


def extension_paths(extension: ApplicationExtension) -> tuple[str, ...]:
    """读取扩展声明的 scope 豁免路径并过滤空值。"""
    value = getattr(extension, "scope_exempt_paths", ())
    value = value() if callable(value) else value
    if isinstance(value, str):
        value = (value,)
    if value is None:
        return ()
    return tuple(path.strip() for path in value if isinstance(path, str) and path.strip())


def path_is_exempt(path: str, paths: Collection[str]) -> bool:
    """判断请求路径是否命中精确豁免或显式 ``/*`` 前缀豁免。"""
    normalized = path.rstrip("/") or "/"
    for item in paths:
        pattern = item.rstrip("/") or "/"
        if pattern.endswith("/*"):
            prefix = pattern[:-2].rstrip("/") or "/"
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        elif normalized == pattern:
            return True
    return False
