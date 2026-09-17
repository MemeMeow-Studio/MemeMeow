"""公共核心图片库只读 HTTP 边界。

本模块负责图片列表、metadata 详情和媒体读取的请求校验、状态投影与错误映射；scope
services、数据库环境、处理 repository、视觉 identity 和路由注册由入口通过 callback 注入，
不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.database import DatabaseError
from backend.image_naming import public_filename_fields, saved_filename
from backend.metadata import MetadataError
from backend.public_dto import sanitize_public_timestamp
from backend.services.thumbnails import ThumbnailError


ServicesProvider = Callable[[Request], Any]
EnvironmentProvider = Callable[[Request], Any]
ProcessingRepositoryProvider = Callable[[Request], Any]
VisualIdentityProvider = Callable[[Request], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]
MISSING_MEDIA_ERRORS = frozenset({"metadata_missing", "file_not_found"})


def agent_analysis_summary(provenance: object) -> dict[str, str] | None:
    """从已保存的 Agent 语境提取模型和完整完成时间，供图片详情只读展示。"""
    context = provenance.get("agent_context") if isinstance(provenance, Mapping) else None
    if not isinstance(context, Mapping):
        return None
    model = context.get("model")
    completed_at = sanitize_public_timestamp(context.get("completed_at"))
    if (
        not isinstance(model, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", model)
        or completed_at is None
    ):
        return None
    return {"model": model, "completed_at": completed_at}


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
    # 列表只使用数据库保存的源版本事实；原图身份校验属于媒体和处理等消费字节的路径。
    source_identities = {record.id: (record.size_bytes, str(record.sha256)) for record in records}
    items: list[dict[str, object]] = []
    identity = visual_identity(request)
    thumbnails = getattr(scoped_services, "thumbnails", None)
    projection_batch = getattr(thumbnails, "projections", None) if thumbnails is not None else None
    thumbnail_projections: dict[Any, dict[str, object]] = {}
    if callable(projection_batch) and valid_records:
        thumbnail_projections = projection_batch(valid_records, source_identities=source_identities)
    ready_text_embedding_ids: set[object] = set()
    ready_text_embedding_ids_fn = getattr(scoped_services.search, "valid_text_embedding_ids", None)
    if callable(ready_text_embedding_ids_fn) and valid_records:
        ready_text_embedding_ids = set(ready_text_embedding_ids_fn(valid_records))
    ready_visual_embedding_ids: set[object] = set()
    with environment(request) as database_environment:
        ready_ids_fn = getattr(database_environment.visual, "ready_ids", None)
        if callable(ready_ids_fn) and valid_records:
            ready_visual_embedding_ids = set(ready_ids_fn(valid_records, model=identity.model, preprocess_version=identity.preprocess_version, dimensions=identity.dimensions))
        visual_batch_available = callable(ready_ids_fn)
    processing = processing_repository(request)
    latest_processing_by_meme: dict[object, Any] = {}
    latest_for_targets = getattr(processing, "latest_for_targets", None)
    if callable(latest_for_targets) and valid_records:
        latest_processing_by_meme = latest_for_targets((record.id, record.sha256) for record in valid_records)
    for record in valid_records:
        metadata_status = {"status": record.context_status}
        visual_ready = record.id in ready_visual_embedding_ids
        item: dict[str, object] = {
            "meme_id": str(record.id),
            "filename": saved_filename(record.display_name, record.extension),
            "extension": record.extension,
            "size": record.size_bytes,
            "media_url": f"/media/{record.id}",
            "metadata": metadata_status,
            "embedding_status": "blocked" if metadata_status.get("status") == "repair_required" else "ready" if record.id in ready_text_embedding_ids else "pending",
            "visual_embedding_status": "ready" if visual_batch_available and visual_ready else "pending",
        }
        if thumbnails is not None:
            item["thumbnail"] = thumbnail_projections.get(record.id, {"status": "pending", "media_url": None})
        latest_processing = (
            latest_processing_by_meme.get(record.id)
        )
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
        record, image = metadata_service.image_for_meme(meme_id)
        metadata = metadata_service.load(image)
    except MetadataError as exc:
        status = 404 if exc.code in MISSING_MEDIA_ERRORS else 409
        code = "meme_not_found" if exc.code in MISSING_MEDIA_ERRORS else exc.code
        message = "图片不存在" if exc.code in MISSING_MEDIA_ERRORS else "图片元数据无法读取"
        raise error(status, code, message) from exc
    payload = metadata.model_dump(mode="json", exclude_none=False)
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        # 摘要只由服务端事实派生，不能沿用导入数据中同名的扩展字段。
        provenance.pop("agent_analysis", None)
        analysis = agent_analysis_summary(provenance)
        if analysis is not None:
            provenance["agent_analysis"] = analysis
    try:
        public_fields = public_filename_fields(record)
    except ValueError as exc:
        raise error(409, "metadata_invalid", "图片元数据无法读取") from exc
    image_payload = payload.get("image")
    if not isinstance(image_payload, dict):
        raise error(409, "metadata_invalid", "图片元数据无法读取")
    # sidecar 内部仍保留物理 relative_path 供后端校验；HTTP 响应只能返回展示文件名。
    image_payload.update(public_fields)
    image_payload["relative_path"] = public_fields["filename"]
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
        if exc.code in MISSING_MEDIA_ERRORS:
            raise error(404, "meme_not_found", "图片不存在") from exc
        raise error(409, exc.code, "图片身份校验失败") from exc
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
