"""缩略图 reconciliation 的公共 HTTP 边界。

本模块位于 FastAPI 路由模板与 scope-bound 缩略图服务之间，只处理管理凭据、请求
参数和稳定错误投影；当前 scope、服务装配和路由错误工厂均由入口显式注入。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from backend.persistence.engine import DatabaseError
from backend.services.thumbnails import ThumbnailError


ThumbnailServiceProvider = Callable[[Request], Any]
AuthorizeProvider = Callable[[Request, str | None], None]
ErrorFactory = Callable[[int, str, str], HTTPException]


def _reconcile_error(exc: ThumbnailError, *, error: ErrorFactory) -> HTTPException:
    """把 reconciliation 的领域异常压缩成稳定 HTTP 错误。"""
    if exc.code == "thumbnail_backpressure":
        return error(429, "thumbnail_backpressure", "缩略图任务队列已满，请稍后重试")
    if exc.code in {"thumbnail_task_unavailable", "thumbnail_generation_unavailable"}:
        return error(503, "thumbnail_reconcile_unavailable", "缩略图回填服务当前不可用")
    return error(500, "thumbnail_reconcile_failed", "缩略图回填失败")


async def reconcile_thumbnails(
    request: Request,
    *,
    page: int,
    page_size: int | None,
    limit: int | None,
    token: str | None,
    thumbnail_service: ThumbnailServiceProvider,
    authorize: AuthorizeProvider,
    error: ErrorFactory,
) -> dict[str, int]:
    """在当前可信 scope 内分页提交缩略图回填任务。

    输入是已由 FastAPI 校验的分页参数和 Settings 管理 token；输出是服务返回的
    扫描、提交、可用和失败计数。调用场景是受保护的存量图片 reconciliation 入口，
    不接受客户端 scope、路径或内部派生 key。
    """
    authorize(request, token)
    unknown = set(request.query_params) - {"page", "page_size", "limit"}
    if unknown:
        raise error(400, "invalid_request", "缩略图回填只接受 page、page_size 和 limit")
    service = thumbnail_service(request)
    if service is None or not callable(getattr(service, "reconcile", None)):
        raise error(503, "thumbnail_reconcile_unavailable", "缩略图回填服务当前不可用")
    try:
        result = service.reconcile(page=page, page_size=page_size, limit=limit)
    except ThumbnailError as exc:
        raise _reconcile_error(exc, error=error) from exc
    except (DatabaseError, RuntimeError, ValueError) as exc:
        raise error(503, "thumbnail_reconcile_unavailable", "缩略图回填服务当前不可用") from exc
    if not isinstance(result, dict) or not {"scanned", "submitted", "available", "failed"} <= set(result):
        raise error(503, "thumbnail_reconcile_unavailable", "缩略图回填服务返回了无效结果")
    counts: dict[str, int] = {}
    for key in ("scanned", "submitted", "available", "failed"):
        value = result[key]
        if type(value) is not int or value < 0:
            raise error(503, "thumbnail_reconcile_unavailable", "缩略图回填服务返回了无效结果")
        counts[key] = value
    return counts


__all__ = ["reconcile_thumbnails"]
