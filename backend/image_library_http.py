"""公共核心图片库只读 HTTP 边界。

本模块负责图片列表、metadata 详情和媒体读取的请求校验、状态投影与错误映射；scope
services、数据库环境、处理 repository、视觉 identity 和路由注册由入口通过 callback 注入，
不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from anyio import to_thread
from fastapi import HTTPException, Request
from fastapi.responses import Response
from loguru import logger

from backend.database import DatabaseError
from backend.embedding_input import build_text_input
from backend.http_file_cache import revalidated_file_response
from backend.image_export import DerivedImageExportService, IMAGE_EXPORT_EDGES
from backend.image_naming import public_filename_fields, saved_filename
from backend.image_library_status import ImageStatus, image_status_condition
from backend.metadata import MetadataError
from backend.persistence.repositories.memes import ImageSort
from backend.public_dto import sanitize_public_timestamp
from backend.services.thumbnails import ThumbnailError


ServicesProvider = Callable[[Request], Any]
EnvironmentProvider = Callable[[Request], Any]
ProcessingRepositoryProvider = Callable[[Request], Any]
VisualIdentityProvider = Callable[[Request], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]
MISSING_MEDIA_ERRORS = frozenset({"metadata_missing", "file_not_found"})
MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


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
    sort: ImageSort = "name_asc",
    status: ImageStatus = "all",
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
    unknown = set(request.query_params) - {"search", "page", "page_size", "sort", "status"}
    if unknown:
        raise error(400, "invalid_request", "图片列表不接受已废弃的目录参数")
    scoped_services = services(request)
    identity = visual_identity(request)
    with environment(request) as database_environment:
        condition = image_status_condition(database_environment, status, embedding_model=scoped_services.search.model, visual_identity=identity) if status != "all" else None
        filters = {"condition": condition} if condition is not None else {}
        records = database_environment.memes.list(search=search, page=page, page_size=page_size, sort=sort, **filters)
        total = database_environment.memes.count(search=search, **filters)
    valid_records = records
    # 图片列表复用数据库保存的 SHA 和大小，缩略图状态查询使用同一份记录。
    source_identities = {record.id: (record.size_bytes, str(record.sha256)) for record in records}
    items: list[dict[str, object]] = []
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
        text_input = build_text_input(record.display_name, record.meme_context, record.context_status)
        visual_ready = record.id in ready_visual_embedding_ids
        item: dict[str, object] = {
            "meme_id": str(record.id),
            "filename": saved_filename(record.display_name, record.extension),
            "name_source": record.name_source,
            "text_input_status": text_input.status,
            "extension": record.extension,
            "size": record.size_bytes,
            "media_url": f"/media/{record.id}",
            "metadata": metadata_status,
            "embedding_status": "blocked" if metadata_status.get("status") == "repair_required" else "waiting_for_text" if text_input.status == "waiting_for_text" else "ready" if record.id in ready_text_embedding_ids else "pending",
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
    max_edge: int | None = None,
) -> Response:
    """按当前 scope 的稳定 meme_id 返回原图或指定最长边的图片。

    输入是路径中的稳定 Meme 标识；输出是文件或无内容的 304 响应。
    调用场景是预览、复制和相册保存；尺寸生成在数据库事务之外执行。
    """
    if max_edge is not None and (type(max_edge) is not int or max_edge not in IMAGE_EXPORT_EDGES):
        raise error(422, "image_export_size_invalid", "图片最长边只允许 60、120、240、360、480 或 640 像素")
    scope_services = services(request)
    try:
        record, path = scope_services.metadata.image_for_meme(meme_id, verify_content=False)
    except MetadataError as exc:
        if exc.code in MISSING_MEDIA_ERRORS:
            raise error(404, "meme_not_found", "图片不存在") from exc
        raise error(409, exc.code, "图片身份校验失败") from exc
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise error(409, "invalid_storage_key", "图片存储格式无效")
    digest = record.sha256
    if max_edge is not None:
        thumbnails = scope_services.thumbnails
        if thumbnails is None:
            raise error(503, "image_export_service_unavailable", "图片尺寸服务尚未初始化")
        try:
            exports = DerivedImageExportService(thumbnails, max_edge)
            path, media_type, digest = await to_thread.run_sync(partial(exports.prepare_media, meme_id))
        except (ThumbnailError, DatabaseError) as exc:
            logger.warning("image_export stage=prepare error_code={}", exc.code)
            if exc.code in {"meme_not_found", "thumbnail_not_found", "file_not_found"}:
                raise error(404, "meme_not_found", "图片不存在") from exc
            status = 429 if exc.code in {"thumbnail_backpressure", "gif_processing_busy"} else 504 if exc.code == "thumbnail_generation_timeout" else 409
            messages = {
                "thumbnail_backpressure": "图片处理并发数量已达到上限，请稍后重试",
                "gif_processing_busy": "图片处理并发数量已达到上限，请稍后重试",
                "thumbnail_generation_timeout": "图片尺寸处理超时",
                "thumbnail_source_unavailable": "原图文件无法读取，请检查存储读取权限及文件状态",
                "thumbnail_source_changed": "原图版本发生变化，请重新检索",
                "image_export_temp_limit_exceeded": "图片解码所需内存超过处理上限",
                "image_export_memory_exceeded": "图片处理所需内存超过处理上限",
                "image_export_output_limit_exceeded": "处理后的图片文件超过大小上限",
                "image_export_tool_missing": "服务器缺少 GIF 尺寸处理程序 gifsicle",
                "image_export_animation_changed": "GIF 处理结果的动画时间信息发生变化",
                "image_export_transparency_changed": "GIF 处理结果的透明背景发生变化",
            }
            raise error(status, exc.code, messages.get(exc.code, f"图片尺寸处理异常：{exc.code}")) from exc
    try:
        return await revalidated_file_response(request, path, media_type=media_type, content_digest=digest)
    except FileNotFoundError as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc


async def thumbnail_media(
    request: Request,
    *,
    meme_id: str,
    services: ServicesProvider,
    error: ErrorFactory,
) -> Response:
    """按当前 scope 的稳定 Meme ID读取可用缩略图，隐藏内部输出 key。"""
    thumbnails = getattr(services(request), "thumbnails", None)
    if thumbnails is None:
        raise error(404, "meme_not_found", "图片不存在")
    try:
        path, media_type, digest = thumbnails.media_path(meme_id)
    except (ThumbnailError, DatabaseError, MetadataError) as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc
    try:
        return await revalidated_file_response(request, path, media_type=media_type, content_digest=digest)
    except FileNotFoundError as exc:
        raise error(404, "meme_not_found", "图片不存在") from exc


__all__ = ["image_metadata", "list_images", "media", "thumbnail_media"]
