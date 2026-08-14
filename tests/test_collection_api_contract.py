"""合集 API 路由和请求模型的静态契约测试。"""

from __future__ import annotations

from api import CollectionItemsRequest, CollectionRequest, app


def test_collection_routes_are_scoped_resource_endpoints() -> None:
    """公共入口只暴露 CRUD 和成员维护，不接受资源包导入导出。"""
    routes = {(route.path, method) for route in app.routes for method in getattr(route, "methods", set())}
    expected = {
        ("/collections", "GET"),
        ("/collections", "POST"),
        ("/collections/{collection_id}", "GET"),
        ("/collections/{collection_id}", "PATCH"),
        ("/collections/{collection_id}", "DELETE"),
        ("/collections/{collection_id}/items", "POST"),
        ("/collections/{collection_id}/items/{meme_id}", "DELETE"),
    }
    assert expected <= routes


def test_collection_requests_reject_scope_and_empty_members() -> None:
    """客户端不能覆盖 scope，成员数组必须非空。"""
    assert CollectionRequest(name=" 工作 ").name == " 工作 "
    try:
        CollectionRequest(name="工作", scope_id="other")
    except Exception:
        pass
    else:
        raise AssertionError("scope_id must be rejected")
    try:
        CollectionItemsRequest(meme_ids=[])
    except Exception:
        pass
    else:
        raise AssertionError("empty meme_ids must be rejected")
