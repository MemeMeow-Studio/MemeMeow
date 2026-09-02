"""公共核心图片库只读 HTTP 边界。

本模块负责图片列表、metadata 详情和媒体读取的请求校验、状态投影与错误映射；scope
services、数据库环境、处理 repository、视觉 identity 和路由注册由入口通过 callback 注入，
不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

import inspect
import mimetypes
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.database import DatabaseError
from backend.metadata import MetadataError
from backend.services.thumbnails import ThumbnailError


ServicesProvider = Callable[[Request], Any]
EnvironmentProvider = Callable[[Request], Any]
ProcessingRepositoryProvider = Callable[[Request], Any]
VisualIdentityProvider = Callable[[Request], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]


async def list_images(
    request: Request,
    *,
    search: str,
    page: int,
    page_size: int,
    services: ServicesProvider,
    environment: EnvironmentProvider,
    processing_repository: ProcessingRepositoryProvider,
    visual_identity: VisualIdentityProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """按文件名筛选并分页列出当前 scope 的扁平图片。

    输入是已由 FastAPI 校验的搜索和分页参数；输出包含图片稳定 ID、脱敏状态和可选的
    最新处理摘要。调用场景是公共图片库只读请求，所有数据均从入口注入的当前 scope
    services/environment 派生。
    """
    unknown = set(request.query_params) - {"search", "page", "page_size"}
    if unknown:
        raise error(400, "invalid_request", "图片列表不接受已废弃的目录参数")
    scoped_services = services(request)
    with environment(request) as database_environment:
        records = database_environment.memes.list(search=search, page=page, page_size=page_size)
        total = database_environment.memes.count(search=search)
    valid_records = records
    source_identities = {record.id: (int(getattr(record, "size_bytes", 0)), str(record.sha256)) for record in records if hasattr(record, "size_bytes")}
    compatibility_identities: dict[Any, dict[str, object]] = {}
    # 旧测试夹具或兼容 facade 没有物化大小时仍可读取一次身份；生产 ORM 路径不进入此分支。
    if len(source_identities) != len(records):
        for record in records:
            if record.id in source_identities:
                continue
            try:
                image = scoped_services.metadata.blob_store.resolve(record.storage_key)
                compatibility_identities[record.id] = scoped_services.metadata._identity(image)
                source_identities[record.id] = (int(compatibility_identities[record.id]["size_bytes"]), str(record.sha256))
            except (DatabaseError, MetadataError, KeyError, TypeError, ValueError):
                source_identities[record.id] = (0, str(record.sha256))
    items: list[dict[str, object]] = []
    identity = visual_identity(request)
    thumbnails = getattr(scoped_services, "thumbnails", None)
    projection_batch = getattr(thumbnails, "projections", None) if thumbnails is not None else None
    thumbnail_projections: dict[Any, dict[str, object]] = {}
    if callable(projection_batch) and valid_records:
        accepts_source_identities = False
        try:
            projection_parameters = inspect.signature(projection_batch).parameters
            accepts_source_identities = "source_identities" in projection_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in projection_parameters.values()
            )
        except (TypeError, ValueError):
            # 无法反射的旧 facade 使用最小 positional 形状，避免把兼容读取升级为 500。
            accepts_source_identities = False
        if accepts_source_identities:
            thumbnail_projections = projection_batch(valid_records, source_identities=source_identities)
        else:
            thumbnail_projections = projection_batch(valid_records)
    ready_text_embedding_ids: set[object] = set()
    ready_text_embedding_ids_fn = getattr(scoped_services.search, "valid_text_embedding_ids", None)
    if callable(ready_text_embedding_ids_fn) and valid_records:
        ready_text_embedding_ids = set(ready_text_embedding_ids_fn(valid_records))
    ready_visual_embedding_ids: set[object] = set()
    with environment(request) as database_environment:
        ready_ids_fn = getattr(database_environment.visual, "ready_ids", None)
        if callable(ready_ids_fn) and valid_records:
            ready_visual_embedding_ids = set(ready_ids_fn(valid_records, model=identity.model, preprocess_version=identity.preprocess_version, dimensions=identity.dimensions))
    processing = processing_repository(request)
    latest_processing_by_meme: dict[object, Any] = {}
    latest_for_targets = getattr(processing, "latest_for_targets", None)
    if callable(latest_for_targets) and valid_records:
        latest_processing_by_meme = latest_for_targets((record.id, record.sha256) for record in valid_records)
    for record in valid_records:
        if hasattr(record, "context_status"):
            metadata_status = {"status": record.context_status}
        else:
            image = scoped_services.metadata.blob_store.resolve(record.storage_key)
            metadata_status_fn = scoped_services.metadata.status
            try:
                if "identity" in inspect.signature(metadata_status_fn).parameters:
                    metadata_status = metadata_status_fn(image, identity=compatibility_identities.get(record.id))
                else:
                    metadata_status = metadata_status_fn(image)
            except (TypeError, ValueError):
                metadata_status = metadata_status_fn(image)
        visual_ready = record.id in ready_visual_embedding_ids
        if not callable(getattr(database_environment.visual, "ready_ids", None)):
            with environment(request) as fallback_environment:
                visual_ready = fallback_environment.visual.get(record.id, model=identity.model, preprocess_version=identity.preprocess_version, dimensions=identity.dimensions, image_sha256=record.sha256) is not None
        item: dict[str, object] = {
            "meme_id": str(record.id),
            "filename": record.storage_key,
            "extension": record.extension,
            "size": getattr(record, "size_bytes", 0),
            "media_url": f"/media/{record.id}",
            "metadata": metadata_status,
            "embedding_status": "blocked" if metadata_status.get("status") == "repair_required" else "ready" if record.id in ready_text_embedding_ids else "pending",
            "visual_embedding_status": "ready" if visual_ready else "pending",
        }
        if thumbnails is not None:
            item["thumbnail"] = thumbnail_projections.get(record.id, {"status": "pending", "media_url": None})
        latest_processing = latest_processing_by_meme.get(record.id) if latest_processing_by_meme else processing.latest_for_target(record.id, record.sha256)
        if latest_processing is not None:
            processing_public = latest_processing.as_dict()
            item.update(
                {
                    "processing_job_id": processing_public.get("job_id"),
                    "processing_status": processing_public.get("status"),
                    "processing_auto_name": processing_public.get("auto_name", False),
                    "processing_has_warnings": processing_public.get("has_warnings", False),
                    "processing_stages": processing_public.get("stages", []),
                }
            )
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


async def image_metadata(
    request: Request,
    *,
    meme_id: str | None,
    services: ServicesProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """按稳定 ``meme_id`` 返回当前 scope 的数据库语境记录。

    输入是客户端提供的稳定 Meme 标识；输出是经过 metadata service 指纹校验的 sidecar
    JSON。调用场景是图片详情请求，物理路径和 scope 不接受客户端覆盖。
    """
    if not meme_id:
        raise error(400, "meme_id_required", "必须提供 meme_id")
    metadata_service = services(request).metadata
    try:
        _record, image = metadata_service.image_for_meme(meme_id)
        metadata = metadata_service.load(image)
    except MetadataError as exc:
        status = 404 if exc.code == "metadata_missing" else 409
        code = "meme_not_found" if exc.code == "metadata_missing" else exc.code
        message = "图片不存在" if exc.code == "metadata_missing" else "图片元数据无法读取"
        raise error(status, code, message) from exc
    payload = metadata.model_dump(mode="json", exclude_none=False)
    payload["meme_id"] = meme_id
    return payload


async def media(
    request: Request,
    *,
    meme_id: str,
    services: ServicesProvider,
    error: ErrorFactory,
) -> FileResponse:
    """按当前 scope 的稳定 meme_id 读取经过指纹校验的图片。

    输入是路径中的稳定 Meme 标识；输出是受控文件的 `FileResponse`。调用场景是媒体读取，
    既有 metadata service 负责 BlobStore 路径和数据库 SHA/size 一致性校验。
    """
    try:
        _record, path = services(request).metadata.image_for_meme(meme_id)
    except MetadataError as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store", "Vary": "Cookie"})


async def thumbnail_media(
    request: Request,
    *,
    meme_id: str,
    services: ServicesProvider,
    error: ErrorFactory,
) -> FileResponse:
    """按当前 scope 的稳定 Meme ID读取可用缩略图，隐藏内部输出 key。"""
    thumbnails = getattr(services(request), "thumbnails", None)
    if thumbnails is None:
        raise error(404, "meme_not_found", "图片不存在")
    try:
        path, media_type = thumbnails.media_path(meme_id)
    except (ThumbnailError, DatabaseError, MetadataError) as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store", "Vary": "Cookie"})


__all__ = ["image_metadata", "list_images", "media", "thumbnail_media"]
