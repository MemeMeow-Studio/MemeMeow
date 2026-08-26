"""公共图片上传 HTTP 边界。

本模块负责 multipart 请求预算、单文件读取、图片预检、幂等上传和处理任务投递；当前
scope 的 service、文件校验、operation policy、错误投影和检索失效均由入口显式注入。
它位于公共核心 HTTP 层，不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from backend.collection_packages import sha256_bytes
from backend.database import DatabaseError
from backend.image_processing import ImageProcessingError
from backend.image_safety import ImagePreflightError, validate_image_content
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError, Operations
from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key


MAX_UPLOAD_FILES_PER_REQUEST = 20
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
# 只有能够确认 durable 写入尚未发生的 metadata 错误才允许返还 reservation；宿主可以
# 注入更保守的集合，以覆盖暂存写入可能已经产生未知副作用的适配层。
UPLOAD_RESERVATION_RELEASE_ERRORS = frozenset(
    {"target_exists", "invalid_filename", "invalid_image", "staging_conflict", "staging_write_failed"}
)


MetadataServiceProvider = Callable[[Request], Any]
TaskServiceProvider = Callable[[Request], Any]
SettingsProvider = Callable[[Request], Any]
ProcessingWorkerProvider = Callable[[Request], Any]
ErrorFactory = Callable[[int, str, str], HTTPException]
ProcessingOptionsNormalizer = Callable[..., Any]
MultipartBoolParser = Callable[..., bool]
FilenameSanitizer = Callable[[str], str]
StorageKeyValidator = Callable[[str], str]
ImageValidator = Callable[[bytes, str], None]
Sha256Calculator = Callable[[bytes], str]
OperationAcquire = Callable[..., Any]
OperationCommit = Callable[[Request, Any], None]
OperationRelease = Callable[[Request, Any], None]
ProcessingSubmitter = Callable[..., Any]
VisualSubmitter = Callable[..., Any]
ProcessingConfigProvider = Callable[[Request], dict[str, object]]
ThumbnailEnqueue = Callable[[Request, str], Any]
EnqueueErrorProjector = Callable[[Exception], str]
SearchInvalidator = Callable[[Request], None]
OperationErrorProjector = Callable[[OperationPolicyError], HTTPException]


class _BoundedUploadMultipartParser(MultiPartParser):
    """在 Starlette 写入临时文件前统计 multipart 文件字节。"""

    def __init__(self, *args: Any, max_request_bytes: int | None = None, **kwargs: Any) -> None:
        """保存请求级文件字节预算，供 ``on_part_data`` 在落盘前执行。"""
        super().__init__(*args, **kwargs)
        self._max_request_bytes = max_request_bytes
        self._request_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """只累计文件 part 实际字节，拒绝超过预算的数据。"""
        if self._current_part.file is not None:
            self._request_file_bytes += end - start
            if self._max_request_bytes is not None and self._request_file_bytes > self._max_request_bytes:
                raise MultiPartException("Upload request exceeded configured byte budget.")
        super().on_part_data(data, start, end)


def _default_error(status: int, code: str, message: str) -> HTTPException:
    """构造 parser/helper 默认使用的稳定错误。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


async def _parse_upload_form(
    request: Request,
    *,
    max_files: int,
    max_request_bytes: int | None,
    error: ErrorFactory | None = None,
):
    """解析上传 multipart，并在文件写入 spool 前执行总预算。"""
    error_factory = error or _default_error
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        return await request.form(max_files=max_files, max_fields=8)
    parser = _BoundedUploadMultipartParser(
        request.headers,
        request.stream(),
        max_files=max_files,
        max_fields=8,
        max_part_size=1024 * 1024,
        max_request_bytes=max_request_bytes,
    )
    try:
        return await parser.parse()
    except MultiPartException as exc:
        message = str(exc)
        if "Too many files" in message:
            raise error_factory(413, "too_many_files", "单个上传请求最多包含 20 个文件") from exc
        if "byte budget" in message:
            raise error_factory(413, "request_too_large", "上传请求超过服务端总字节预算") from exc
        raise error_factory(400, "invalid_request", "上传 multipart 请求无效") from exc


async def _read_upload_content(upload: UploadFile, *, max_upload_size: int) -> tuple[bytes, bool]:
    """顺序读取单个 multipart spool，最多保留单文件上限加一个字节。"""
    content = bytearray()
    while len(content) <= max_upload_size:
        chunk = await upload.read(min(UPLOAD_READ_CHUNK_BYTES, max_upload_size + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content), len(content) > max_upload_size


def idempotent_upload_result(
    request: Request,
    metadata_service: Any,
    record: Any,
    image: Path,
    *,
    original: str,
    reverse_image_policy: str,
    auto_name: bool,
    processing_worker: ProcessingWorkerProvider,
    submit_processing_job: ProcessingSubmitter,
    thumbnail_enqueue: ThumbnailEnqueue | None = None,
) -> dict[str, object]:
    """构造已存在 durable 图片的幂等成功结果并复用当前处理状态。

    输入是当前 scope 已验证的 Meme、图片路径和处理选项，输出沿用上传接口的兼容字段。
    调用场景是同名、大小和 SHA-256 三方事实一致的重试上传；不会重复 acquire upload
    operation 或创建第二条 Meme。
    """
    result: dict[str, object] = {
        "meme_id": str(record.id),
        "filename": original,
        "ok": True,
        "saved_filename": Path(record.storage_key).name,
        "media_url": f"/media/{record.id}",
        "metadata_status": metadata_service.status(image)["status"],
        "idempotent": True,
        "auto_name": auto_name,
        "reverse_image_policy": reverse_image_policy,
    }
    if thumbnail_enqueue is not None:
        try:
            thumbnail_enqueue(request, str(record.id))
        except Exception as exc:  # noqa: BLE001 - 缩略图失败不得影响原图幂等成功
            result["thumbnail_enqueue_error"] = getattr(exc, "code", "thumbnail_enqueue_failed")
    worker = processing_worker(request)
    snapshot = worker.jobs.latest_for_target(record.id, record.sha256) if worker is not None else None
    if snapshot is None:
        try:
            snapshot = submit_processing_job(
                request,
                record,
                image,
                reverse_image_policy=reverse_image_policy,
                auto_name=auto_name,
            )
        except (ImageProcessingError, MetadataError, RuntimeError, DatabaseError) as exc:
            result["processing_job_error"] = getattr(exc, "code", "image_processing_unavailable")
    if snapshot is not None:
        result.update(
            {
                "processing_job_id": snapshot.job_id,
                "processing_status": snapshot.status,
                "processing_progress": snapshot.progress,
                "processing_message": snapshot.message,
                "metadata_job_id": snapshot.job_id,
            }
        )
    return result


async def upload_images(
    request: Request,
    *,
    settings: SettingsProvider,
    metadata_service: MetadataServiceProvider,
    task_service: TaskServiceProvider,
    normalize_processing_options: ProcessingOptionsNormalizer,
    parse_multipart_bool: MultipartBoolParser,
    sanitize_filename: FilenameSanitizer,
    validate_storage_key: StorageKeyValidator = validate_business_storage_key,
    validate_image: ImageValidator = validate_image_content,
    calculate_sha256: Sha256Calculator = sha256_bytes,
    processing_worker: ProcessingWorkerProvider,
    submit_processing_job: ProcessingSubmitter,
    processing_config: ProcessingConfigProvider,
    submit_visual_task: VisualSubmitter,
    context_enqueue_error: EnqueueErrorProjector,
    acquire_operation: OperationAcquire,
    commit_operation: OperationCommit,
    release_operation: OperationRelease,
    invalidate_search: SearchInvalidator,
    error: ErrorFactory,
    thumbnail_enqueue: ThumbnailEnqueue | None = None,
    operation_error: OperationErrorProjector | None = None,
    release_errors: Collection[str] = UPLOAD_RESERVATION_RELEASE_ERRORS,
) -> dict[str, object]:
    """解析并逐文件处理图片上传，保留旧接口的 scope、配额和任务语义。

    输入是 FastAPI ``Request`` 及其 multipart body；输出是原 ``batch_id`` 与逐文件
    ``results``。调用场景是公共 `/images/upload` route，所有 durable 操作都通过当前
    scope service 和显式 policy callback 执行，批量中的单项失败不会回滚其它成功项。
    """
    configured_settings = settings(request)
    configured_file_limit = int(getattr(configured_settings, "max_files_per_request", MAX_UPLOAD_FILES_PER_REQUEST))
    file_limit = max(1, min(configured_file_limit, MAX_UPLOAD_FILES_PER_REQUEST))
    max_request_bytes = getattr(configured_settings, "max_request_bytes", None)
    if max_request_bytes is not None:
        max_request_bytes = int(max_request_bytes)
    try:
        # 多解析一个文件是为了把越过业务上限的请求映射为稳定错误；更大的请求仍由
        # parser 在读取过量文件时拒绝，不进入任何 durable 写入路径。
        form = await _parse_upload_form(request, max_files=file_limit + 1, max_request_bytes=max_request_bytes, error=error)
    except HTTPException as exc:
        if exc.status_code == 400 and "Too many files" in str(exc.detail):
            raise error(413, "too_many_files", "单个上传请求最多包含 20 个文件") from exc
        raise
    unknown = set(form.keys()) - {"auto_name", "files", "reverse_image_policy"}
    if unknown:
        raise error(400, "invalid_request", "上传不接受已废弃的目标目录字段")
    try:
        auto_name = parse_multipart_bool(form.get("auto_name"), default=False)
        options = normalize_processing_options(
            request,
            reverse_image_policy=form.get("reverse_image_policy"),
            auto_name=auto_name,
        )
    except ImageProcessingError as exc:
        status = 503 if exc.code == "reverse_image_unavailable" else 400
        raise error(status, exc.code, "图片处理选项无效或服务不可用") from exc
    reverse_image_policy = options.reverse_image_policy
    auto_name = options.auto_name
    files = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "read")]
    if not files:
        raise error(400, "files_required", "必须上传图片文件")
    if len(files) > file_limit:
        raise error(413, "too_many_files", "单个上传请求最多包含 20 个文件")
    scoped_metadata = metadata_service(request)
    # parser 已在返回 form 前按所有文件 part 校验总预算；业务层只保留当前文件的受限内容。
    results: list[dict[str, object]] = []
    upload_batch_id = uuid4().hex if len(files) > 1 else None
    upload_task_ids: list[str] = []
    unified_processing_worker_used = False
    for upload in files:
        # 释放上一项的内容缓冲后再触碰下一个 spool，避免请求内文件字节累积。
        content = b""
        content, too_large = await _read_upload_content(upload, max_upload_size=configured_settings.max_upload_size)
        original = upload.filename or "image"
        clean = sanitize_filename(original)
        if Path(clean).suffix.lower() not in SUPPORTED_EXTENSIONS:
            results.append({"filename": original, "ok": False, "error": "unsupported_format"})
            continue
        if too_large:
            results.append({"filename": original, "ok": False, "error": "file_too_large"})
            continue
        try:
            validate_storage_key(clean)
        except ValueError:
            results.append({"filename": original, "ok": False, "error": "invalid_filename"})
            continue
        try:
            validate_image(content, Path(clean).suffix.lower())
        except ImagePreflightError as exc:
            results.append({"filename": original, "ok": False, "error": exc.code})
            continue
        content_digest = calculate_sha256(content)
        try:
            existing = scoped_metadata.find_existing_upload(clean, sha256=content_digest, size_bytes=len(content))
        except MetadataError as exc:
            if exc.code == "upload_reconciliation_required":
                results.append({"filename": original, "ok": False, "error": exc.code})
                continue
            results.append({"filename": original, "ok": False, "error": "file_exists"})
            continue
        if existing is not None:
            existing_record, existing_path = existing
            results.append(
                idempotent_upload_result(
                    request,
                    scoped_metadata,
                    existing_record,
                    existing_path,
                    original=original,
                    reverse_image_policy=reverse_image_policy,
                    auto_name=auto_name,
                    processing_worker=processing_worker,
                    submit_processing_job=submit_processing_job,
                    thumbnail_enqueue=thumbnail_enqueue,
                )
            )
            continue
        target = scoped_metadata.blob_store.resolve(clean, must_exist=False)
        grant = None
        try:
            upload_key = f"upload:{content_digest}:{clean}"
            grant = acquire_operation(
                request,
                Operations.IMAGE_UPLOAD,
                upload_key,
                resource_id=clean,
                source="upload",
                input_digest=content_digest,
            )
        except OperationPolicyError as exc:
            if operation_error is not None and len(files) == 1:
                raise operation_error(exc) from exc
            results.append({"filename": original, **exc.payload()})
            continue
        try:
            meme_id, saved_path = scoped_metadata.upload_bytes(content, target_key=clean)
        except (OSError, MetadataError) as exc:
            if isinstance(exc, MetadataError) and exc.code == "target_exists":
                # 并发首个提交可能已 durable 落位；重新验证事实后收束为幂等成功。
                try:
                    existing = scoped_metadata.find_existing_upload(clean, sha256=content_digest, size_bytes=len(content))
                except MetadataError:
                    existing = None
                if existing is not None:
                    existing_record, existing_path = existing
                    try:
                        release_operation(request, grant)
                    except OperationPolicyError:
                        # 竞争请求可能已由另一执行者提交同一 grant，保留未知状态待恢复。
                        pass
                    results.append(
                        idempotent_upload_result(
                            request,
                            scoped_metadata,
                            existing_record,
                            existing_path,
                            original=original,
                            reverse_image_policy=reverse_image_policy,
                            auto_name=auto_name,
                            processing_worker=processing_worker,
                            submit_processing_job=submit_processing_job,
                            thumbnail_enqueue=thumbnail_enqueue,
                        )
                    )
                    continue
            # 普通 I/O 异常可能已经产生未知暂存副作用，只有白名单错误可以 release。
            if isinstance(exc, MetadataError) and exc.code in release_errors and grant is not None:
                try:
                    release_operation(request, grant)
                except OperationPolicyError:
                    pass
            results.append({"filename": original, "ok": False, "error": "metadata_write_failed"})
            continue
        if grant is not None:
            try:
                commit_operation(request, grant)
            except OperationPolicyError:
                # 文件和 Meme 已 durable，不能回滚或 release；保留成功事实。
                pass
        meme_id = str(meme_id)
        target = saved_path
        result: dict[str, object] = {
            "meme_id": meme_id,
            "filename": original,
            "ok": True,
            "saved_filename": target.name,
            "media_url": f"/media/{meme_id}",
            "auto_named": False,
        }
        if thumbnail_enqueue is not None:
            try:
                thumbnail_enqueue(request, meme_id)
            except Exception as exc:  # noqa: BLE001 - 派生失败保留原图成功事实
                result["thumbnail_enqueue_error"] = getattr(exc, "code", "thumbnail_enqueue_failed")
        worker = None
        try:
            worker = processing_worker(request)
            if worker is not None:
                unified_processing_worker_used = True
            if worker is None:
                # 没有统一图片控制面时保留旧视觉入口；统一路径由下方 pipeline 提交。
                task = submit_visual_task(
                    request,
                    target,
                    batch_id=upload_batch_id,
                    reverse_image_policy=reverse_image_policy,
                    schedule=upload_batch_id is None,
                )
                result["visual_task_id"] = task.task_id
                result["visual_job_id"] = task.task_id
                result["metadata_job_id"] = task.task_id
                result["visual_task_status"] = task.status
                if upload_batch_id:
                    upload_task_ids.append(task.task_id)
        except (OSError, RuntimeError, MetadataError) as exc:
            # 图片和 pending Meme 已提交，任务失败只能作为可重试告警。
            result["visual_task_error"] = context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
            result["metadata_job_error"] = result["visual_task_error"]
        result["metadata_status"] = scoped_metadata.status(target)["status"]
        result["auto_name"] = auto_name
        result["reverse_image_policy"] = reverse_image_policy
        try:
            if worker is not None:
                embedding_record = scoped_metadata.embedding_record(target)
                processing = worker.submit(
                    meme_id,
                    scoped_metadata.image_sha256(target),
                    metadata_hash=embedding_record.get("metadata_hash") if isinstance(embedding_record, Mapping) else None,
                    config=processing_config(request),
                    reverse_image_policy=reverse_image_policy,
                    auto_name=auto_name,
                    schedule=False,
                )
                worker.schedule(processing.job_id)
                # 旧字段继续返回完整 Job 标识；阶段叶子从图片处理状态接口读取。
                result.update(
                    {
                        "processing_job_id": processing.job_id,
                        "submission_mode": "pipeline",
                        "processing_status": processing.status,
                        "auto_name": processing.auto_name,
                        "reverse_image_policy": processing.reverse_image_policy,
                        "visual_task_id": processing.job_id,
                        "visual_job_id": processing.job_id,
                        "metadata_job_id": processing.job_id,
                        "metadata_job_status": processing.status,
                        "visual_task_status": processing.status,
                    }
                )
        except (ImageProcessingError, MetadataError, RuntimeError, DatabaseError) as exc:
            # 图片 durable 事实已经完成；控制面异常只作为可重试诊断返回。
            result["processing_job_error"] = getattr(exc, "code", "image_processing_unavailable")
        invalidate_search(request)
        results.append(result)
    # 统一图片 Worker 使用逐图增量向量，不为上传批次创建旧 cache generation。
    if upload_batch_id and upload_task_ids and not unified_processing_worker_used:
        scoped_tasks = task_service(request)
        scoped_tasks.seal_batch(upload_batch_id)
        for task_id in upload_task_ids:
            scoped_tasks.schedule(task_id)
    return {"batch_id": upload_batch_id, "results": results}


__all__ = [
    "MAX_UPLOAD_FILES_PER_REQUEST",
    "UPLOAD_READ_CHUNK_BYTES",
    "UPLOAD_RESERVATION_RELEASE_ERRORS",
    "_BoundedUploadMultipartParser",
    "_parse_upload_form",
    "_read_upload_content",
    "idempotent_upload_result",
    "upload_images",
]
