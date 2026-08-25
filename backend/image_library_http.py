"""公共核心图片库只读 HTTP 边界。

本模块负责图片列表、metadata 详情和媒体读取的请求校验、状态投影与错误映射；scope
services、数据库环境、处理 repository、视觉 identity 和路由注册由入口通过 callback 注入，
不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse

from backend.database import DatabaseError
from backend.metadata import MetadataError


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
    items: list[dict[str, object]] = []
    identity = visual_identity(request)
    for record in records:
        try:
            image = scoped_services.metadata.blob_store.resolve(record.storage_key)
            image_identity = scoped_services.metadata._identity(image)
        except (DatabaseError, MetadataError):
            # 与旧入口一致：无法证明文件指纹的记录不进入公开列表。
            continue
        metadata_status = scoped_services.metadata.status(image)
        with environment(request) as database_environment:
            visual_row = database_environment.visual.get(
                record.id,
                model=identity.model,
                preprocess_version=identity.preprocess_version,
                dimensions=identity.dimensions,
                image_sha256=record.sha256,
            )
        item: dict[str, object] = {
            "meme_id": str(record.id),
            "filename": record.storage_key,
            "extension": record.extension,
            "size": image_identity["size_bytes"],
            "media_url": f"/media/{record.id}",
            "metadata": metadata_status,
            "embedding_status": "ready" if scoped_services.search.has_cache() and metadata_status.get("status") in {"partial", "ready"} else "blocked" if metadata_status.get("status") == "repair_required" else "pending",
            "visual_embedding_status": "ready" if visual_row is not None else "pending",
        }
        latest_processing = processing_repository(request).latest_for_target(record.id, record.sha256)
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
    return FileResponse(path, media_type=media_type)


__all__ = ["image_metadata", "list_images", "media"]
