"""公共核心合集 CRUD 与成员维护 HTTP 边界。

本模块负责合集列表、创建、详情、重命名、删除和成员增删的请求校验、状态投影与错误
映射；scope-bound environment、metadata service、错误构造和路由注册由入口通过 callback
注入。本模块不处理合集 ZIP 导入/导出，也不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request

from backend.database import DatabaseError


EnvironmentProvider = Callable[[Request], Any]
MetadataServiceProvider = Callable[[Request], Any]
ThumbnailServiceProvider = Callable[[Request], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]


def collection_payload(request: Request, environment: Any, row: Any, thumbnail_service: ThumbnailServiceProvider | None = None) -> dict[str, object]:
    """构造不暴露 scope 的合集摘要。

    输入是当前请求、scope-bound 数据环境和合集 ORM 行；输出包含稳定合集 ID、成员数、
    受控封面媒体地址及时间字段。调用场景是列表、创建和重命名成功响应。
    """
    cover = environment.collections.cover(row.id)
    payload: dict[str, object] = {
        "collection_id": str(row.id),
        "name": row.name,
        "member_count": environment.collections.member_count(row.id),
        "cover_media_url": f"/media/{cover.id}" if cover else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    if cover is not None and thumbnail_service is not None:
        thumbnails = thumbnail_service(request)
        project = getattr(thumbnails, "projection", None)
        payload["cover_meme_id"] = str(cover.id)
        payload["cover_thumbnail"] = project(cover) if callable(project) else {"status": "pending", "media_url": None}
    return payload


def collection_error(exc: DatabaseError, *, error: ErrorFactory) -> HTTPException:
    """把合集 repository 错误映射为稳定 HTTP 契约。

    输入是 repository 抛出的业务错误和宿主错误工厂；输出保留既有状态码、error code
    与中文 message。调用场景是合集 CRUD、详情和成员 handler，也供入口旧 helper 兼容调用。
    """
    mapping = {
        "collection_not_found": (404, "collection_not_found", "合集不存在"),
        "collection_exists": (409, "collection_exists", "合集名称已存在"),
        "invalid_collection_name": (422, "invalid_collection_name", "合集名称必须为 1 至 100 个字符"),
        "meme_not_found": (404, "meme_not_found", "图片不存在"),
        "empty_members": (422, "empty_members", "至少选择一张图片"),
    }
    status, code, message = mapping.get(exc.code, (400, exc.code, "合集请求无效"))
    return error(status, code, message)


async def list_collections(
    request: Request,
    *,
    page: int,
    page_size: int,
    environment: EnvironmentProvider,
    error: ErrorFactory,
    thumbnail_service: ThumbnailServiceProvider | None = None,
) -> dict[str, object]:
    """分页列出当前 scope 的合集。

    输入是 FastAPI 已校验的分页参数；输出包含当前 scope 的合集摘要、总数和分页信息。
    调用场景是合集工作区列表请求，任何客户端 scope/user query 都在 repository 前拒绝。
    """
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise error(400, "invalid_request", "合集列表不接受 scope 或 user 参数")
    with environment(request) as database_environment:
        rows = database_environment.collections.list(page=page, page_size=page_size)
        return {
            "items": [collection_payload(request, database_environment, row, thumbnail_service) for row in rows],
            "total": database_environment.collections.count(),
            "page": page,
            "page_size": page_size,
        }


async def create_collection(
    request: Request,
    payload: Any,
    *,
    environment: EnvironmentProvider,
    error: ErrorFactory,
    thumbnail_service: ThumbnailServiceProvider | None = None,
) -> dict[str, object]:
    """创建当前 scope 的空合集。

    输入是已由入口严格校验的合集名称；输出是新合集摘要。调用场景是合集创建请求，
    名称规范化和唯一性由 scope-bound CollectionRepository 负责。
    """
    try:
        with environment(request) as database_environment:
            row = database_environment.collections.create(payload.name)
            return collection_payload(request, database_environment, row, thumbnail_service)
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


async def get_collection(
    request: Request,
    collection_id: str,
    *,
    page: int,
    page_size: int,
    environment: EnvironmentProvider,
    metadata_service: MetadataServiceProvider,
    error: ErrorFactory,
    thumbnail_service: ThumbnailServiceProvider | None = None,
) -> dict[str, object]:
    """返回合集元数据和当前文件信息的分页成员。

    输入是当前 scope 的合集 ID 与已校验分页参数；输出包含成员当前文件名、稳定 Meme ID、
    受控媒体地址和 metadata 状态。调用场景是合集详情请求，文件状态由注入的 metadata
    service 根据 scope-bound storage 解析。
    """
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise error(400, "invalid_request", "合集详情不接受 scope 或 user 参数")
    try:
        with environment(request) as database_environment:
            row = database_environment.collections.get(collection_id)
            if row is None:
                raise DatabaseError("collection_not_found")
            members: list[dict[str, object]] = []
            for _item, meme in database_environment.collections.members(row.id, page=page, page_size=page_size):
                scoped_metadata = metadata_service(request)
                metadata_status = scoped_metadata.status(scoped_metadata.blob_store.resolve(meme.storage_key))
                members.append(
                    {
                        "meme_id": str(meme.id),
                        "filename": meme.storage_key,
                        "extension": meme.extension,
                        "size": meme.size_bytes,
                        "media_url": f"/media/{meme.id}",
                        "metadata": metadata_status,
                    }
                )
                if thumbnail_service is not None:
                    thumbnails = thumbnail_service(request)
                    project = getattr(thumbnails, "projection", None)
                    members[-1]["thumbnail"] = project(meme) if callable(project) else {"status": "pending", "media_url": None}
            payload = collection_payload(request, database_environment, row, thumbnail_service)
            payload["members"] = members
            payload["total"] = database_environment.collections.member_count(row.id)
            payload["page"] = page
            payload["page_size"] = page_size
            return payload
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


async def rename_collection(
    request: Request,
    collection_id: str,
    payload: Any,
    *,
    environment: EnvironmentProvider,
    error: ErrorFactory,
    thumbnail_service: ThumbnailServiceProvider | None = None,
) -> dict[str, object]:
    """重命名合集并保留成员关系。

    输入是当前 scope 的合集 ID 和已校验名称；输出是更新后的合集摘要。调用场景是合集
    重命名请求，冲突和资源不存在错误由统一合集错误映射投影。
    """
    try:
        with environment(request) as database_environment:
            row = database_environment.collections.rename(collection_id, payload.name)
            return collection_payload(request, database_environment, row, thumbnail_service)
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


async def delete_collection(
    request: Request,
    collection_id: str,
    *,
    environment: EnvironmentProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """删除合集及成员关系，不删除 Meme 或图片文件。

    输入是当前 scope 的合集 ID；输出确认删除的合集 ID。调用场景是合集删除请求，删除
    语义完全交给 scope-bound repository 和其事务边界。
    """
    try:
        with environment(request) as database_environment:
            database_environment.collections.delete(collection_id)
            return {"collection_id": collection_id, "deleted": True}
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


async def add_collection_items(
    request: Request,
    collection_id: str,
    payload: Any,
    *,
    environment: EnvironmentProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """原子批量加入图片并返回幂等计数。

    输入是当前 scope 的合集 ID 和 Meme ID 列表；输出包含新增、已存在和最终成员数。
    调用场景是合集成员批量维护，任一无效 Meme 的原子拒绝由 repository 保证。
    """
    try:
        with environment(request) as database_environment:
            added, existing, total = database_environment.collections.add_members(collection_id, payload.meme_ids)
            return {
                "collection_id": collection_id,
                "added_count": added,
                "existing_count": existing,
                "member_count": total,
            }
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


async def remove_collection_item(
    request: Request,
    collection_id: str,
    meme_id: str,
    *,
    environment: EnvironmentProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """幂等移除合集成员。

    输入是当前 scope 的合集 ID 和稳定 Meme ID；输出确认移除及最终成员数。调用场景是
    合集详情中的单成员删除，repository 不会触碰 Meme 或图片文件。
    """
    try:
        with environment(request) as database_environment:
            total = database_environment.collections.remove_member(collection_id, meme_id)
            return {"collection_id": collection_id, "meme_id": meme_id, "removed": True, "member_count": total}
    except DatabaseError as exc:
        raise collection_error(exc, error=error) from exc


__all__ = [
    "add_collection_items",
    "collection_error",
    "collection_payload",
    "create_collection",
    "delete_collection",
    "get_collection",
    "list_collections",
    "remove_collection_item",
    "rename_collection",
]
