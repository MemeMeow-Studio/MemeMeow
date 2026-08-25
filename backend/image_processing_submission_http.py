"""公共核心图片处理批量提交 HTTP 边界。

本模块负责分页枚举当前 scope 图片、读取 metadata 输入、提交或复用处理 Job 以及逐项
错误投影；Worker、Repository、scope environment、配置规范化和路由注册由入口通过 callback
注入，不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException, Request

from backend.database import DatabaseError
from backend.image_processing import ImageProcessingError
from backend.metadata import MetadataError


ProcessingWorkerProvider = Callable[[Request], Any]
NormalizeOptions = Callable[..., Any]
ProcessingRepositoryProvider = Callable[[Request], Any]
MetadataServiceProvider = Callable[[Request], Any]
EnvironmentProvider = Callable[[Request], Any]
ProcessingConfigProvider = Callable[[Request], Mapping[str, object]]
ErrorFactory = Callable[[int, str, str], HTTPException]


async def process_image_library(
    request: Request,
    payload: Any,
    *,
    page: int,
    page_size: int,
    processing_worker: ProcessingWorkerProvider,
    normalize_processing_options: NormalizeOptions,
    processing_repository: ProcessingRepositoryProvider,
    metadata_service: MetadataServiceProvider,
    environment: EnvironmentProvider,
    processing_config: ProcessingConfigProvider,
    error: ErrorFactory,
) -> dict[str, object]:
    """分页枚举当前 scope 图片并逐图提交或复用处理 Job。

    输入是已由入口校验的联网策略、auto-name 和分页参数；输出保留旧的逐图 Job 摘要、
    复用标志、总数和分页字段。调用场景是图片工作区批量处理请求，任何单图异常只影响
    当前结果，不阻止同页其它 Meme。
    """
    worker = processing_worker(request)
    if worker is None:
        raise error(503, "image_processing_unavailable", "图片处理服务当前不可用")
    try:
        options = normalize_processing_options(
            request,
            reverse_image_policy=payload.reverse_image_policy,
            auto_name=payload.auto_name,
        )
    except ImageProcessingError as exc:
        raise error(503 if exc.code == "reverse_image_unavailable" else 400, exc.code, "图片处理选项无效或服务不可用") from exc

    repository = processing_repository(request)
    with environment(request) as database_environment:
        memes = database_environment.memes.list(page=page, page_size=page_size)
        total = database_environment.memes.count()
    results: list[dict[str, object]] = []
    for meme in memes:
        try:
            latest = repository.latest_for_target(meme.id, meme.sha256)
            image_service = metadata_service(request)
            image = image_service.blob_store.resolve(meme.storage_key)
            embedding_record = image_service.embedding_record(image)
            snapshot = worker.submit(
                meme.id,
                meme.sha256,
                metadata_hash=embedding_record.get("metadata_hash") if isinstance(embedding_record, Mapping) else None,
                config=processing_config(request),
                reverse_image_policy=options.reverse_image_policy,
                auto_name=options.auto_name,
                explicit_retry=latest is not None and latest.status in {"failed", "blocked", "unknown_execution"},
            )
            results.append(
                {
                    "meme_id": str(meme.id),
                    "job_id": snapshot.job_id,
                    "processing_job_id": snapshot.job_id,
                    "submission_mode": "pipeline",
                    "status": snapshot.status,
                    "reused": latest is not None and snapshot.job_id == latest.job_id,
                }
            )
        except ImageProcessingError as exc:
            results.append({"meme_id": str(meme.id), "error": exc.code})
        except (DatabaseError, MetadataError, RuntimeError):
            results.append({"meme_id": str(meme.id), "error": "image_processing_failed"})
    return {"results": results, "count": len(results), "total": total, "page": page, "page_size": page_size}


__all__ = ["process_image_library"]
