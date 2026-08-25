"""公共核心合集 ZIP 导入 HTTP 边界。

本模块负责 `/collections/import` 的 multipart 预算、合集包预检、scope-bound 图片导入、
operation policy 收束和异步处理投递。路由注册、当前请求的服务装配、错误构造与检索失效
均由入口通过 callback 注入；本模块不反向依赖 ``api.py`` 或 Server 入口。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from backend.collection_packages import (
    CollectionPackageError,
    DEFAULT_MAX_FILE_SIZE,
    MAX_ARCHIVE_COMPRESSED_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    preflight_archive,
    resolve_import_filename,
)
from backend.database import DatabaseError
from backend.image_processing import ImageProcessingError
from backend.image_upload_http import UPLOAD_RESERVATION_RELEASE_ERRORS, _parse_upload_form, _read_upload_content
from backend.metadata import MetadataError
from backend.operation_policy import OperationPolicyError, Operations


EnvironmentProvider = Callable[[Request], Any]
MetadataServiceProvider = Callable[[Request], Any]
SettingsProvider = Callable[[Request], Any]
ProcessingWorkerProvider = Callable[[Request], Any]
ProcessingConfigProvider = Callable[[Request], dict[str, object]]
VisualTaskSubmitter = Callable[..., Any]
ContextEnqueueError = Callable[[Exception], str]
OperationAcquire = Callable[..., Any]
OperationCommit = Callable[[Request, Any], None]
OperationRelease = Callable[[Request, Any], None]
SearchInvalidator = Callable[[Request], None]
ErrorFactory = Callable[[int, str, str], HTTPException]
DatabaseErrorProjector = Callable[[DatabaseError], HTTPException]
MultipartFormParser = Callable[..., Any]
UploadContentReader = Callable[..., Any]
ArchivePreflight = Callable[..., Any]
FilenameResolver = Callable[..., Any]
PackageErrorProjector = Callable[[CollectionPackageError], HTTPException]


def collection_package_error(exc: CollectionPackageError, *, error: ErrorFactory) -> HTTPException:
    """把合集包错误投影为稳定 HTTP 响应。

    输入是 ZIP/图片预检或成员读取产生的业务错误及入口错误工厂；输出保留既有状态码、
    error code 和中文 message。调用场景是合集导入及公共合集导出兼容 helper，原始路径
    和异常诊断不会进入响应。
    """
    status = 413 if exc.code in {
        "archive_too_large",
        "file_too_large",
        "package_too_large",
        "member_count_exceeded",
        "manifest_too_large",
        "compression_ratio_exceeded",
        "image_frame_count_exceeded",
        "image_frame_pixels_exceeded",
        "image_total_pixels_exceeded",
    } else 409 if exc.code in {"collection_exists", "member_unreadable", "member_changed"} else 400
    messages = {
        "archive_too_large": "合集 ZIP 压缩包超过 64 MiB 限制",
        "invalid_zip": "合集 ZIP 无法读取",
        "manifest_missing": "合集 ZIP 缺少 manifest.json",
        "manifest_invalid": "合集 manifest 无效",
        "unsupported_package_version": "不支持的合集包格式版本",
        "manifest_entries_mismatch": "合集 manifest 与 ZIP 文件不一致",
        "sha256_mismatch": "图片 SHA-256 校验失败",
        "size_mismatch": "图片大小校验失败",
        "invalid_zip_path": "ZIP 路径非法",
        "unsafe_zip_entry": "ZIP 包含不安全条目",
        "zip64_not_supported": "不支持 ZIP64 合集包",
        "compression_ratio_exceeded": "ZIP 压缩放大比超过限制",
        "nested_archive": "ZIP 不允许嵌套归档",
        "duplicate_zip_entry": "ZIP 包含重复条目",
        "invalid_image": "包内图片无法解码",
        "unsupported_format": "包内图片格式不受支持",
        "image_frame_count_exceeded": "包内图片动画帧数超过限制",
        "image_frame_pixels_exceeded": "包内图片单帧像素超过限制",
        "image_total_pixels_exceeded": "包内图片累计帧像素超过限制",
        "image_preflight_timeout": "包内图片预检超时",
        "image_preflight_failed": "包内图片预检失败",
        "member_unreadable": "合集成员图片无法读取",
        "member_changed": "合集成员图片在导出期间发生变化",
        "package_too_large": "合集包解压后超过大小限制",
        "member_count_exceeded": "合集图片数量超过限制",
        "collection_exists": "合集名称已存在",
        "invalid_filename": "包内文件名非法",
        "filename_conflict": "包内文件名无法安全解决冲突",
    }
    return error(status, exc.code, messages.get(exc.code, "合集 ZIP 请求无效"))


async def import_collection(
    request: Request,
    *,
    environment: EnvironmentProvider,
    metadata_service: MetadataServiceProvider,
    settings: SettingsProvider,
    processing_worker: ProcessingWorkerProvider,
    processing_config: ProcessingConfigProvider,
    submit_visual_task: VisualTaskSubmitter,
    context_enqueue_error: ContextEnqueueError,
    acquire_operation: OperationAcquire,
    commit_operation: OperationCommit,
    release_operation: OperationRelease,
    invalidate_search: SearchInvalidator,
    error: ErrorFactory,
    database_error: DatabaseErrorProjector,
    parse_upload_form: MultipartFormParser | None = None,
    read_upload_content: UploadContentReader | None = None,
    preflight: ArchivePreflight | None = None,
    resolve_filename: FilenameResolver | None = None,
    package_error: PackageErrorProjector | None = None,
    release_errors: Collection[str] | None = None,
) -> dict[str, object]:
    """预检合集 ZIP 后在当前 scope 内逐图片导入并投递处理任务。

    输入是 FastAPI 请求及入口显式注入的 scope、存储、计量、处理和错误 callback；输出是
    合集 ID、名称、整体状态、逐成员结果及来源到目标 Meme 的映射。调用场景是唯一的
    `/collections/import` route，预检失败不产生副作用，单成员失败与任务告警不回滚其它
    已完成成员。
    """
    parse_form = parse_upload_form or _parse_upload_form
    read_content = read_upload_content or _read_upload_content
    preflight_package = preflight or preflight_archive
    resolve_target = resolve_filename or resolve_import_filename
    project_package_error = package_error or (lambda exc: collection_package_error(exc, error=error))
    allowed_release_errors = release_errors if release_errors is not None else UPLOAD_RESERVATION_RELEASE_ERRORS
    try:
        form = await parse_form(request, max_files=2, max_request_bytes=MAX_ARCHIVE_COMPRESSED_BYTES)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "request_too_large":
            raise project_package_error(CollectionPackageError("archive_too_large")) from exc
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "too_many_files":
            raise error(400, "file_required", "必须上传一个合集 ZIP 文件") from exc
        raise
    if set(form.keys()) - {"file"}:
        raise error(400, "invalid_request", "合集导入只接受一个 file ZIP 字段")
    values = form.getlist("file")
    uploads = [item for item in values if hasattr(item, "filename") and hasattr(item, "read")]
    if len(values) != 1 or len(uploads) != 1:
        raise error(400, "file_required", "必须上传一个合集 ZIP 文件")
    upload = uploads[0]
    if not str(upload.filename or "").lower().endswith(".zip"):
        raise error(400, "unsupported_package", "合集导入只接受 ZIP 文件")
    try:
        content, too_large = await read_content(upload, max_upload_size=MAX_ARCHIVE_COMPRESSED_BYTES)
        if too_large:
            raise CollectionPackageError("archive_too_large")
        configured_settings = settings(request)
        package = preflight_package(
            content,
            max_file_size=min(int(configured_settings.max_upload_size), DEFAULT_MAX_FILE_SIZE),
            max_total_size=MAX_TOTAL_UNCOMPRESSED_BYTES,
        )
    except CollectionPackageError as exc:
        raise project_package_error(exc) from exc
    finally:
        await upload.close()
    collection_name = package.manifest.collection.name
    try:
        with environment(request) as database_environment:
            if database_environment.collections.by_name(collection_name) is not None:
                raise CollectionPackageError("collection_exists")
    except DatabaseError as exc:
        raise database_error(exc) from exc
    except CollectionPackageError as exc:
        raise project_package_error(exc) from exc
    try:
        with environment(request) as database_environment:
            collection = database_environment.collections.create(collection_name)
            collection_id = collection.id
            existing_by_name: dict[str, object] = {}
            scoped_metadata = metadata_service(request)
            for record in database_environment.memes.list_all():
                valid = scoped_metadata.blob_store.exists_with_identity(
                    record.storage_key,
                    sha256=record.sha256,
                    size_bytes=record.size_bytes,
                )
                existing_by_name[record.storage_key] = {
                    "meme": record,
                    "sha256": record.sha256 if valid else "__changed__",
                }
    except DatabaseError as exc:
        raise database_error(exc) from exc

    results: list[dict[str, object]] = []
    meme_id_map: dict[str, str] = {}
    created_count = 0
    for package_member in package.members:
        member = package_member.manifest
        result: dict[str, object] = {
            "source_meme_id": member.source_meme_id,
            "filename": member.filename_at_export,
            "ok": False,
        }
        try:
            target = resolve_target(member.filename_at_export, member.sha256, existing_by_name)
            if target.existing_meme is not None:
                target_id = str(getattr(target.existing_meme, "id", target.existing_meme))
                with environment(request) as database_environment:
                    database_environment.collections.add_members(collection_id, [target_id])
                result.update({"ok": True, "status": "reused", "target_meme_id": target_id, "saved_filename": target.filename})
            else:
                try:
                    import_grant = acquire_operation(
                        request,
                        Operations.IMAGE_UPLOAD,
                        f"upload:{member.sha256}:{target.filename}",
                        resource_id=target.filename,
                        source="collection_import",
                        input_digest=member.sha256,
                    )
                except OperationPolicyError as exc:
                    result.update(exc.payload())
                    results.append(result)
                    meme_id_map[member.source_meme_id] = ""
                    continue
                try:
                    target_id, target_path = metadata_service(request).upload_bytes(
                        package_member.content,
                        target_key=target.filename,
                    )
                except (MetadataError, OSError) as exc:
                    # 只有明确知道 durable 写入尚未开始时才能归还上传 reservation。
                    if isinstance(exc, MetadataError) and exc.code in allowed_release_errors:
                        try:
                            release_operation(request, import_grant)
                        except OperationPolicyError:
                            pass
                    raise
                try:
                    commit_operation(request, import_grant)
                except OperationPolicyError:
                    # 文件和 Meme 已经提交，不能把未知计量状态误报为导入失败。
                    pass
                target_id = str(target_id)
                created_count += 1
                with environment(request) as database_environment:
                    database_environment.collections.add_members(collection_id, [target_id])
                existing_by_name[target.filename] = {"meme": target_id, "sha256": member.sha256}
                result.update({"ok": True, "status": "imported", "target_meme_id": target_id, "saved_filename": target_path.name})
                processing_worker_instance = None
                try:
                    processing_worker_instance = processing_worker(request)
                    if processing_worker_instance is None:
                        task = submit_visual_task(request, target_path, expected_sha256=member.sha256, schedule=True)
                        result.update(
                            {
                                "visual_task_id": task.task_id,
                                "visual_job_id": task.task_id,
                                "metadata_job_id": task.task_id,
                                "metadata_job_status": task.status,
                                "visual_task_status": task.status,
                            }
                        )
                except (OSError, RuntimeError, MetadataError) as exc:
                    # Meme、文件和合集关系已有效提交，任务失败只能作为可重试的逐项告警。
                    enqueue_error = context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
                    result["visual_task_error"] = enqueue_error
                    result["metadata_job_error"] = enqueue_error
                try:
                    worker = processing_worker_instance
                    if worker is not None:
                        processing = worker.submit(
                            target_id,
                            member.sha256,
                            config=processing_config(request),
                            reverse_image_policy="forbid",
                            schedule=False,
                        )
                        worker.schedule(processing.job_id)
                        # 旧合集客户端把视觉任务和语境任务都作为一个可轮询标识读取；统一
                        # 控制面现在返回父 Job，因此这些字段兼容地指向同一个 Job。
                        result.update(
                            {
                                "processing_job_id": processing.job_id,
                                "submission_mode": "pipeline",
                                "processing_status": processing.status,
                                "visual_task_id": processing.job_id,
                                "visual_job_id": processing.job_id,
                                "metadata_job_id": processing.job_id,
                                "metadata_job_status": processing.status,
                                "visual_task_status": processing.status,
                            }
                        )
                except (ImageProcessingError, DatabaseError, MetadataError, RuntimeError) as exc:
                    result["processing_job_error"] = getattr(exc, "code", "image_processing_unavailable")
            meme_id_map[member.source_meme_id] = str(result.get("target_meme_id") or "")
        except CollectionPackageError as exc:
            result["error"] = exc.code
        except (DatabaseError, MetadataError, OSError, RuntimeError) as exc:
            result["error"] = getattr(exc, "code", "import_failed")
        results.append(result)
    if created_count:
        invalidate_search(request)
    failed_count = sum(1 for item in results if not item.get("ok"))
    warning_count = sum(1 for item in results if item.get("metadata_job_error"))
    status = "succeeded" if not failed_count and not warning_count else "partial"
    return {
        "collection_id": str(collection_id),
        "name": collection_name,
        "status": status,
        "partial": status == "partial",
        "results": results,
        "meme_id_map": meme_id_map,
    }


__all__ = ["collection_package_error", "import_collection"]
