"""公共合集 CRUD 与成员 HTTP 边界测试。"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import api
import backend.collection_http as collection_http
from backend.database import DatabaseError


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造与公共入口相同 detail 形状的测试异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _request(query_params: dict[str, str] | None = None) -> SimpleNamespace:
    """构造只包含合集 handler 所需 query 参数的最小请求。"""
    return SimpleNamespace(query_params=query_params or {})


def _row(name: str = "工作") -> SimpleNamespace:
    """构造具有稳定时间字段的合集 ORM 行替身。"""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(id=uuid4(), name=name, created_at=now, updated_at=now)


def _meme(storage_key: str = "cat.png") -> SimpleNamespace:
    """构造合集详情所需的当前 scope Meme 行替身。"""
    return SimpleNamespace(id=uuid4(), storage_key=storage_key, extension=".png", size_bytes=12)


class _Collections:
    """记录合集 repository 调用并提供可控响应的 scope repository 替身。"""

    def __init__(self, row: SimpleNamespace, member: SimpleNamespace | None = None) -> None:
        self.row = row
        self.member = member
        self.calls: list[tuple[str, object]] = []

    def list(self, **kwargs: object) -> list[SimpleNamespace]:
        """返回当前 scope 的一页合集。"""
        self.calls.append(("list", kwargs))
        return [self.row]

    def count(self) -> int:
        """返回当前 scope 合集总数。"""
        self.calls.append(("count", None))
        return 1

    def cover(self, collection_id: object) -> SimpleNamespace | None:
        """返回最早成员作为封面。"""
        self.calls.append(("cover", collection_id))
        return self.member

    def member_count(self, collection_id: object) -> int:
        """返回当前合集成员数。"""
        self.calls.append(("member_count", collection_id))
        return 1 if self.member is not None else 0

    def get(self, collection_id: str) -> SimpleNamespace | None:
        """返回当前 scope 合集或模拟资源不存在。"""
        self.calls.append(("get", collection_id))
        return self.row if collection_id == str(self.row.id) else None

    def members(self, collection_id: object, **kwargs: object) -> list[tuple[object, SimpleNamespace]]:
        """返回按加入顺序排列的成员。"""
        self.calls.append(("members", (collection_id, kwargs)))
        return [(SimpleNamespace(), self.member)] if self.member is not None else []

    def create(self, name: str) -> SimpleNamespace:
        """返回新建合集或抛出受控 repository 错误。"""
        self.calls.append(("create", name))
        return self.row

    def rename(self, collection_id: str, name: str) -> SimpleNamespace:
        """返回重命名后的合集。"""
        self.calls.append(("rename", (collection_id, name)))
        self.row.name = name
        return self.row

    def delete(self, collection_id: str) -> None:
        """记录删除合集。"""
        self.calls.append(("delete", collection_id))

    def add_members(self, collection_id: str, meme_ids: list[str]) -> tuple[int, int, int]:
        """返回批量加入的幂等计数。"""
        self.calls.append(("add_members", (collection_id, meme_ids)))
        return 1, 1, 2

    def remove_member(self, collection_id: str, meme_id: str) -> int:
        """返回移除后的最终成员数。"""
        self.calls.append(("remove_member", (collection_id, meme_id)))
        return 0


class _Environment:
    """提供当前 scope 合集 repository 的 context manager 替身。"""

    def __init__(self, collections: _Collections) -> None:
        self.collections = collections

    def __enter__(self) -> "_Environment":
        """返回 scope-bound environment。"""
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """结束环境且不吞掉异常。"""
        del exc_type, exc, traceback


def test_collection_routes_keep_single_canonical_crud_boundary() -> None:
    """七个 CRUD/成员 route 只注册一次且不改变公开 metadata。"""
    expected = {
        ("/collections", "GET", None),
        ("/collections", "POST", 201),
        ("/collections/{collection_id}", "GET", None),
        ("/collections/{collection_id}", "PATCH", None),
        ("/collections/{collection_id}", "DELETE", None),
        ("/collections/{collection_id}/items", "POST", None),
        ("/collections/{collection_id}/items/{meme_id}", "DELETE", None),
    }
    actual = {
        (route.path, method, route.status_code)
        for route in api.app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/collections") and route.path not in {"/collections/import", "/collections/{collection_id}/export"}
    }
    assert actual == expected
    assert all(route.tags == ["collections"] for route in api.app.routes if route.path.startswith("/collections") and route.path not in {"/collections/import", "/collections/{collection_id}/export"})
    assert sum(1 for route in api.app.routes if route.path == "/collections/import") == 1
    assert sum(1 for route in api.app.routes if route.path == "/collections/{collection_id}/export") == 1


def test_collection_module_keeps_one_way_dependency_and_legacy_imports() -> None:
    """新模块不反向导入入口，旧 helper 和 handler 名称仍可用。"""
    source = Path(collection_http.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported.update(alias.name.split(".", 1)[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert "api" not in imported
    assert "server_api" not in imported
    assert api.list_collections.__name__ == "list_collections"
    assert api.create_collection.__name__ == "create_collection"
    assert api.get_collection.__name__ == "get_collection"
    assert api.rename_collection.__name__ == "rename_collection"
    assert api.delete_collection.__name__ == "delete_collection"
    assert api.add_collection_items.__name__ == "add_collection_items"
    assert api.remove_collection_item.__name__ == "remove_collection_item"


def test_collection_list_projects_cover_and_rejects_query_selector() -> None:
    """合集列表保留摘要字段，并在调用 scope repository 前拒绝未知 query。"""
    row = _row()
    member = _meme()
    repository = _Collections(row, member)
    environment = _Environment(repository)
    result = asyncio.run(collection_http.list_collections(_request(), page=2, page_size=7, environment=lambda _request: environment, error=_error))
    assert result["page"] == 2
    assert result["page_size"] == 7
    assert result["total"] == 1
    assert result["items"][0]["collection_id"] == str(row.id)
    assert result["items"][0]["cover_media_url"] == f"/media/{member.id}"
    assert repository.calls[:2] == [("list", {"page": 2, "page_size": 7}), ("cover", row.id)]

    with pytest.raises(HTTPException) as caught:
        asyncio.run(collection_http.list_collections(_request({"scope_id": "other"}), page=1, page_size=50, environment=lambda _request: (_ for _ in ()).throw(AssertionError("scope repository must not be used")), error=_error))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == "invalid_request"


def test_collection_detail_projects_current_member_file_and_metadata() -> None:
    """合集详情使用稳定 Meme ID、当前文件名、媒体地址和 scope metadata 状态。"""
    row = _row()
    member = _meme("renamed.png")
    repository = _Collections(row, member)
    environment = _Environment(repository)
    image = Path("/scope/renamed.png")
    metadata = SimpleNamespace(blob_store=SimpleNamespace(resolve=lambda key: image), status=lambda path: {"status": "ready", "path": str(path)})
    result = asyncio.run(collection_http.get_collection(_request(), str(row.id), page=1, page_size=50, environment=lambda _request: environment, metadata_service=lambda _request: metadata, error=_error))
    assert result["total"] == 1
    assert result["members"] == [{"meme_id": str(member.id), "filename": "renamed.png", "extension": ".png", "size": 12, "media_url": f"/media/{member.id}", "metadata": {"status": "ready", "path": str(image)}}]


@pytest.mark.parametrize("operation", ["create", "rename", "delete", "add", "remove"])
def test_collection_database_errors_use_stable_mapping(operation: str) -> None:
    """CRUD 和成员操作把 repository 业务错误交给统一 HTTP 映射。"""
    row = _row()
    repository = _Collections(row)
    environment = _Environment(repository)
    method = {
        "create": lambda: collection_http.create_collection(_request(), SimpleNamespace(name="合集"), environment=lambda _request: (_ for _ in ()).throw(DatabaseError("collection_exists")), error=_error),
        "rename": lambda: collection_http.rename_collection(_request(), str(row.id), SimpleNamespace(name="合集"), environment=lambda _request: (_ for _ in ()).throw(DatabaseError("collection_exists")), error=_error),
        "delete": lambda: collection_http.delete_collection(_request(), str(row.id), environment=lambda _request: (_ for _ in ()).throw(DatabaseError("collection_not_found")), error=_error),
        "add": lambda: collection_http.add_collection_items(_request(), str(row.id), SimpleNamespace(meme_ids=["meme"]), environment=lambda _request: (_ for _ in ()).throw(DatabaseError("meme_not_found")), error=_error),
        "remove": lambda: collection_http.remove_collection_item(_request(), str(row.id), "meme", environment=lambda _request: (_ for _ in ()).throw(DatabaseError("collection_not_found")), error=_error),
    }[operation]
    with pytest.raises(HTTPException) as caught:
        asyncio.run(method())
    expected = {"create": (409, "collection_exists"), "rename": (409, "collection_exists"), "delete": (404, "collection_not_found"), "add": (404, "meme_not_found"), "remove": (404, "collection_not_found")}[operation]
    assert (caught.value.status_code, caught.value.detail["error"]) == expected


def test_collection_membership_response_uses_injected_scope_repository() -> None:
    """成员增删只使用入口传入的 scope repository，并保留计数响应。"""
    row = _row()
    repository = _Collections(row)
    environment = _Environment(repository)
    payload = SimpleNamespace(meme_ids=["meme-1", "meme-1"])
    added = asyncio.run(collection_http.add_collection_items(_request(), str(row.id), payload, environment=lambda _request: environment, error=_error))
    removed = asyncio.run(collection_http.remove_collection_item(_request(), str(row.id), "meme-1", environment=lambda _request: environment, error=_error))
    assert added == {"collection_id": str(row.id), "added_count": 1, "existing_count": 1, "member_count": 2}
    assert removed == {"collection_id": str(row.id), "meme_id": "meme-1", "removed": True, "member_count": 0}
    assert [name for name, _value in repository.calls] == ["add_members", "remove_member"]
