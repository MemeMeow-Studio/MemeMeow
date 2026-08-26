"""缩略图 reconciliation HTTP 边界、授权和错误投影测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
from backend.persistence.engine import DatabaseError
from backend.services.thumbnails import ThumbnailError
from backend.thumbnail_http import reconcile_thumbnails
from backend.settings_http import _settings_token


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造公共入口使用的稳定错误形状。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _request(query_params: dict[str, str] | None = None) -> SimpleNamespace:
    """构造只包含 reconciliation 所需 query 参数的请求替身。"""
    return SimpleNamespace(query_params=query_params or {}, state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))


class _ThumbnailService:
    """记录当前 scope 服务收到的回填参数并返回计数。"""

    def __init__(self, result: dict[str, object] | None = None) -> None:
        """初始化服务结果和调用记录。"""
        self.calls: list[dict[str, object]] = []
        self.result = result or {"scanned": 4, "submitted": 2, "available": 1, "failed": 1}

    def reconcile(self, **kwargs: object) -> dict[str, object]:
        """返回测试计数并记录入口传入的分页参数。"""
        self.calls.append(kwargs)
        return self.result


def _authorize(_request: object, token: str | None) -> None:
    """模拟受保护管理入口的固定 token 校验。"""
    if token != "thumbnail-admin-token":
        raise _error(403, "settings_forbidden", "后端设置管理未授权")


def _call(
    request: SimpleNamespace,
    service: object,
    *,
    token: str | None = "thumbnail-admin-token",
    authorize=_authorize,
) -> dict[str, int]:
    """调用公共 reconciliation handler 并注入 scope-bound 依赖。"""
    return asyncio.run(
        reconcile_thumbnails(
            request,
            page=2,
            page_size=7,
            limit=3,
            token=token,
            thumbnail_service=lambda _request: service,
            authorize=authorize,
            error=_error,
        )
    )


def test_reconcile_route_is_single_protected_post_boundary() -> None:
    """reconciliation 只暴露一个 POST 路由，并归入图片任务标签。"""
    routes = [route for route in api.app.routes if getattr(route, "path", None) == "/images/thumbnails/reconcile"]
    assert len(routes) == 1
    assert routes[0].methods == {"POST"}
    assert routes[0].tags == ["images", "tasks"]


def test_reconcile_uses_injected_scope_service_and_returns_counts() -> None:
    """入口只把当前请求和分页参数交给注入服务，不接受客户端 scope。"""
    request = _request({"page": "2", "page_size": "7", "limit": "3"})
    request.state.scope = SimpleNamespace(scope_id="tenant-a")
    service = _ThumbnailService()
    seen_requests: list[object] = []

    result = asyncio.run(
        reconcile_thumbnails(
            request,
            page=2,
            page_size=7,
            limit=3,
            token="thumbnail-admin-token",
            thumbnail_service=lambda received: (seen_requests.append(received) or service),
            authorize=_authorize,
            error=_error,
        )
    )

    assert result == {"scanned": 4, "submitted": 2, "available": 1, "failed": 1}
    assert seen_requests == [request]
    assert service.calls == [{"page": 2, "page_size": 7, "limit": 3}]


def test_reconcile_rejects_bad_credentials_before_service_or_query_work() -> None:
    """错误凭据必须在服务解析和 query 白名单检查前终止请求。"""
    service = _ThumbnailService()
    request = _request({"unexpected": "secret"})
    with pytest.raises(HTTPException) as caught:
        _call(request, service, token="wrong-token")
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "settings_forbidden"
    assert service.calls == []


def test_reconcile_rejects_unknown_query_parameters() -> None:
    """路径外的 scope、user 和其它参数不能进入回填服务。"""
    service = _ThumbnailService()
    with pytest.raises(HTTPException) as caught:
        _call(_request({"page": "2", "scope_id": "other"}), service)
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_request"
    assert service.calls == []


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (ThumbnailError("thumbnail_backpressure"), 429, "thumbnail_backpressure"),
        (ThumbnailError("thumbnail_task_unavailable"), 503, "thumbnail_reconcile_unavailable"),
        (DatabaseError("database_unavailable"), 503, "thumbnail_reconcile_unavailable"),
    ],
)
def test_reconcile_projects_backpressure_and_unavailable_errors(raised: Exception, status: int, code: str) -> None:
    """背压和基础服务不可用分别映射为可重试的稳定 HTTP 错误。"""
    class FailingService:
        """抛出指定 reconciliation 错误的服务替身。"""

        def reconcile(self, **_kwargs: object) -> None:
            """抛出测试指定的领域或数据库异常。"""
            raise raised

    with pytest.raises(HTTPException) as caught:
        _call(_request({"page": "1"}), FailingService())
    assert caught.value.status_code == status
    assert caught.value.detail["error"] == code


@pytest.mark.parametrize(
    "result",
    [
        {"scanned": True, "submitted": 0, "available": 0, "failed": 0},
        {"scanned": -1, "submitted": 0, "available": 0, "failed": 0},
        {"scanned": "1", "submitted": 0, "available": 0, "failed": 0},
        {"scanned": 1, "submitted": 0, "available": 0},
    ],
)
def test_reconcile_fails_closed_on_invalid_service_result(result: dict[str, object]) -> None:
    """服务返回缺字段或非非负整数计数时不能伪造成功响应。"""
    with pytest.raises(HTTPException) as caught:
        _call(_request(), _ThumbnailService(result))
    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "thumbnail_reconcile_unavailable"


@pytest.mark.parametrize(
    ("primary", "legacy", "authorization"),
    [
        ("thumbnail-admin-token", None, None),
        (None, "thumbnail-admin-token", None),
        (None, None, "Bearer thumbnail-admin-token"),
        (None, None, "bEaReR thumbnail-admin-token"),
    ],
)
def test_reconcile_route_forwards_compatible_token_headers(
    monkeypatch: pytest.MonkeyPatch,
    primary: str | None,
    legacy: str | None,
    authorization: str | None,
) -> None:
    """canonical Header、旧 Header 和大小写不敏感 Bearer 均传递管理 token。"""
    captured: dict[str, object] = {}

    async def fake_handler(request: object, **kwargs: object) -> dict[str, int]:
        """记录 route 解出的 token，避免该单测启动数据库生命周期。"""
        captured["request"] = request
        captured.update(kwargs)
        return {"scanned": 0, "submitted": 0, "available": 0, "failed": 0}

    monkeypatch.setattr(api, "_reconcile_thumbnails_http", fake_handler)
    request = _request()
    result = asyncio.run(
        api.reconcile_thumbnails(
            request,
            page=1,
            page_size=None,
            limit=None,
            x_settings_admin_token=primary,
            x_mememeow_settings_token=legacy,
            authorization=authorization,
        )
    )
    assert result == {"scanned": 0, "submitted": 0, "available": 0, "failed": 0}
    assert captured["token"] == "thumbnail-admin-token"


def test_settings_token_precedence_does_not_fall_back_from_wrong_primary_header() -> None:
    """已提供的错误主 Header 不得借助旧 Header 或 Bearer 绕过授权。"""
    assert _settings_token("wrong", "thumbnail-admin-token", "Bearer thumbnail-admin-token") == "wrong"
