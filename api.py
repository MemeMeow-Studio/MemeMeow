"""MemeMeow FastAPI 应用入口。

路由层只处理 HTTP 契约和安全边界，搜索、持久任务与 OpenCode 服务在 lifespan 中初始化。
"""

from __future__ import annotations

import mimetypes
import hashlib
import inspect
import json
import re
import tempfile
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import UUID, uuid4
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.formparsers import MultiPartException, MultiPartParser
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from backend.config import Settings, validate_agent_concurrency
from backend.collection_packages import (
    CollectionPackageError,
    DEFAULT_MAX_FILE_SIZE,
    MAX_ARCHIVE_COMPRESSED_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    build_export_archive,
    cleanup_archive,
    preflight_archive,
    resolve_import_filename,
    safe_download_filename,
    sha256_bytes,
)
from backend.image_safety import ImagePreflightError, validate_image_content
from backend.errors import ErrorBody
from backend.metadata import MetadataError
from backend.database import DatabaseError, DatabaseResources, Meme, MemeTextEmbedding, ScopeContext, check_database, create_engine_for_settings, utcnow
from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService, PostgresTaskWorkerManager
from backend.paths import PathResolver, SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.rate_limiter import RateLimiter
from backend.opencode import OpenCodeError, OpenCodeRunner
from backend.opencode_workspace import LocalWorkspaceProvider, MissingWorkspaceProvider, TrustedWorkspaceContext, WorkspaceResolutionError
from backend.opencode_activity import OpenCodeActivityReader
from backend.reverse_image import ReverseImageService
from backend.tasks import TaskRecord
from backend.visual import VisualEmbeddingError, VisualInferenceClient, VisualSearchService, identity_from_settings
from backend.scope import LocalScopeResolver, ScopeResolutionError, ScopeResolver, ScopeServiceFactory, ScopeServices, resolve_scope, resolve_scope_async, validate_scope_services
from backend.config_http import STORAGE_PREFLIGHT_BLOCKING_KEYS, _storage_preflight_summary, config_status as _config_status
from backend.search_http import SearchRequest, search_images as _search_images
from backend.cache_task_http import generate_cache as _generate_cache
from backend.task_http import (
    activity_payload as _task_activity_payload,
    cancel_task as _cancel_task_http,
    get_task as _get_task_http,
    list_tasks as _list_tasks_http,
    read_agent_activity as _read_agent_activity_http,
    retry_task as _retry_task_http,
    task_summary as _task_summary_http,
)
from backend.image_stage_http import (
    ImageStageBatchItem,
    ImageStageBatchRequest,
    ImageStageSubmissionRequest,
    ProcessingRetryRequest,
    get_image_processing_job as _get_image_processing_job_http,
    list_image_processing_jobs as _list_image_processing_jobs_http,
    retry_image_processing_job as _retry_image_processing_job_http,
    submit_image_stage as _submit_image_stage_http,
    submit_image_stage_batch as _submit_image_stage_batch_http,
)
from backend.image_context_http import (
    ContextBatchRequest,
    ContextRequest,
    generate_context as _generate_context_http,
    generate_context_batch as _generate_context_batch_http,
    generate_visual_embedding as _generate_visual_embedding_http,
    generate_visual_embedding_batch as _generate_visual_embedding_batch_http,
    repair_metadata as _repair_metadata_http,
)
from backend.visual_callback_http import (
    VisualMatchRequest,
    internal_visual_search_match as _internal_visual_search_match_http,
)
from backend.reverse_image_http import internal_reverse_image_search as _internal_reverse_image_search_http
from backend.settings_http import (
    ConcurrencyUpdateRequest,
    _authorize_settings,
    _backend_settings_status,
    _update_backend_settings,
    backend_settings,
    settings_router,
    update_backend_concurrency,
    update_backend_settings,
)
from backend.operation_policy import AllowAllOperationPolicy, GrantAssociation, GrantAssociationStore, OperationPolicyError, OperationPolicyGateway, Operations, PersistentGrantAssociationStore, UnavailableOperationPolicy, require_allowed
from backend.callbacks import (
    AGENT_CALLBACK_TOKEN_TTL_SECONDS,
    CallbackBinding,
    CallbackError,
    CallbackRegistry,
    HMACCallbackCredentials,
    DEFAULT_CALLBACK_REGISTRY,
    install_body_guard,
    binding_input_digest,
    canonical_callback_request_id,
    log_callback_rejection,
    validate_binding_task,
    validate_callback_headers,
    validate_input_digest,
    validate_request_binding,
    validate_request_id,
    verify_content_length,
)
from backend.image_processing import AUTO_RENAME_WARNING_ERRORS, ImageProcessingError, ImageProcessingOptions, ImageProcessingRepository, ImageProcessingSnapshot, ImageProcessingWorker, SingleImageEmbeddingService, image_file_matches, processing_config_hash, stable_input_digest
from backend.agent_resume import ResumeDecision, classify_resume_error, normalize_identifier, within_total_timeout
from backend.app_extensions import ApplicationExtension, extension_paths, path_is_exempt


INTERNAL_SCOPE_CALLBACK_PATHS = frozenset({"/internal/reverse-image/search", "/internal/visual-search/match"})
OPERATION_POLICY_PATH = "/operations/availability"
SCOPE_SELECTOR_FIELDS = frozenset({"scope_id", "scope-id", "user_id", "user-id"})
MAX_UPLOAD_FILES_PER_REQUEST = 20
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


class _BoundedUploadMultipartParser(MultiPartParser):
    """在 Starlette multipart parser 写入临时文件前统计上传文件字节。"""

    def __init__(self, *args: Any, max_request_bytes: int | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_request_bytes = max_request_bytes
        self._request_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        """只累计文件 part 的实际字节，拒绝超预算数据而不信任请求头。"""
        if self._current_part.file is not None:
            self._request_file_bytes += end - start
            if self._max_request_bytes is not None and self._request_file_bytes > self._max_request_bytes:
                raise MultiPartException("Upload request exceeded configured byte budget.")
        super().on_part_data(data, start, end)


async def _parse_upload_form(request: Request, *, max_files: int, max_request_bytes: int | None):
    """解析上传 multipart，并在文件写入 SpooledTemporaryFile 前执行总预算。"""
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
            raise _error(413, "too_many_files", "单个上传请求最多包含 20 个文件") from exc
        if "byte budget" in message:
            raise _error(413, "request_too_large", "上传请求超过服务端总字节预算") from exc
        raise _error(400, "invalid_request", "上传 multipart 请求无效") from exc


async def _read_upload_content(upload: UploadFile, *, max_upload_size: int) -> tuple[bytes, bool]:
    """从 multipart spool 顺序读取单个文件，返回受单文件上限截断的字节和超限标记。"""
    content = bytearray()
    while len(content) <= max_upload_size:
        chunk = await upload.read(min(UPLOAD_READ_CHUNK_BYTES, max_upload_size + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content), len(content) > max_upload_size


class StrictRequestModel(BaseModel):
    """公共业务 JSON 请求基类，拒绝客户端提交范围选择字段。"""

    model_config = ConfigDict(extra="forbid")


def _extension_list(app: FastAPI) -> tuple[ApplicationExtension, ...]:
    """读取应用创建时冻结的扩展列表，缺失时返回空元组。"""
    value = getattr(app.state, "extensions", ())
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def _extension_scope_exempt(app: FastAPI, path: str) -> bool:
    """判断路径是否由任一宿主扩展声明为不需要业务 scope。"""
    paths: list[str] = []
    for extension in _extension_list(app):
        paths.extend(extension_paths(extension))
    return path_is_exempt(path, paths)


def _scope_field_name(value: str) -> bool:
    """判断请求字段名是否为客户端提交的范围选择器。"""
    lowered = value.strip().lower()
    if lowered in SCOPE_SELECTOR_FIELDS:
        return True
    return lowered.startswith("x-") and lowered[2:] in SCOPE_SELECTOR_FIELDS


def _request_declares_scope(request: Request) -> bool:
    """只检查 query/header 的范围字段，不读取可能包含文件的请求体。"""
    query_params = getattr(request, "query_params", {})
    headers = getattr(request, "headers", {})
    return any(_scope_field_name(str(key)) for key in query_params.keys()) or any(_scope_field_name(str(key)) for key in headers.keys())


async def _invoke_extension_hook(extension: ApplicationExtension, name: str, *args: Any) -> Any:
    """调用可选扩展钩子并兼容同步和异步实现。"""
    hook = getattr(extension, name, None)
    if not callable(hook):
        return None
    result = hook(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _callback_verification_keys(settings: Settings) -> dict[str, str] | None:
    """解析可选的 ``kid=secret`` 轮换验证窗口，不接受模糊配置。"""
    raw = getattr(settings, "agent_callback_verification_keys", None)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise CallbackError("agent_callback_unavailable")
    values: dict[str, str] = {}
    for item in raw.split(","):
        key_id, separator, secret = item.partition("=")
        if not separator or not key_id.strip() or not secret.strip() or key_id.strip() in values:
            raise CallbackError("agent_callback_unavailable")
        values[key_id.strip()] = secret.strip()
    return values


class RenameRequest(StrictRequestModel):
    """图片重命名请求。"""

    meme_id: str | None = None
    new_name: str = Field(min_length=1, max_length=255)


class DeleteRequest(StrictRequestModel):
    """按稳定 meme_id 删除图片的请求。"""

    meme_id: str | None = None


class ProcessingBatchRequest(StrictRequestModel):
    """图片库逐图显式处理请求。"""

    model_config = ConfigDict(extra="forbid")
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")
    auto_name: StrictBool = False


class CollectionRequest(StrictRequestModel):
    """合集创建和重命名请求。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class CollectionItemsRequest(StrictRequestModel):
    """合集批量成员请求；空数组在 API 边界拒绝。"""

    model_config = ConfigDict(extra="forbid")
    meme_ids: list[str] = Field(min_length=1, max_length=500)


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造统一错误异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _operation_gateway(request: Request) -> OperationPolicyGateway:
    """读取应用级 policy 门面；未装配时保持 fail-closed。"""
    gateway = getattr(request.app.state, "operation_policy_gateway", None)
    if isinstance(gateway, OperationPolicyGateway):
        return gateway
    policy = getattr(request.app.state, "operation_policy", None)
    gateway = OperationPolicyGateway(policy)
    request.app.state.operation_policy_gateway = gateway
    if not hasattr(request.app.state, "operation_grants"):
        request.app.state.operation_grants = GrantAssociationStore()
    return gateway


def _acquire_operation(request: Request, operation: str, idempotency_key: str, *, resource_id: str | None = None, task_id: str | None = None, source: str = "core", input_digest: str | None = None):
    """在真实副作用前 acquire，并把 grant 关联保存在服务端内存/宿主替换边界。"""
    scope = _request_scope(request)
    gateway = _operation_gateway(request)
    operation_request = gateway.request(scope, operation, idempotency_key, resource_id=resource_id, task_id=task_id, source=source, input_digest=input_digest)
    # store.acquire 同时校验请求指纹和 association 状态；不能先 get 再把 terminal grant
    # 当作新的执行权返回给上传、删除或其它真实副作用路径。
    association = request.app.state.operation_grants.acquire(operation_request, gateway)
    return association.grant


def _commit_operation(request: Request, grant) -> None:
    """在持久副作用确认后幂等 commit；失败不回滚已完成副作用。"""
    gateway = _operation_gateway(request)
    store = getattr(request.app.state, "operation_grants", None)
    try:
        result = gateway.commit(grant)
        if not result.ok or result.state not in {"committed", "already_committed"}:
            raise OperationPolicyError(result.reason or "operation_policy_unavailable", retry_at=result.retry_at)
        if callable(getattr(store, "transition", None)) and not store.transition(grant, "committed"):
            raise OperationPolicyError("operation_grant_invalid")
    except OperationPolicyError:
        if callable(getattr(store, "transition", None)):
            try:
                store.transition(grant, "unknown")
            except OperationPolicyError:
                pass
        raise


def _release_operation(request: Request, grant) -> None:
    """确认本请求未发生 durable 副作用后释放 reservation，并保留并发 committed 事实。"""
    gateway = _operation_gateway(request)
    store = getattr(request.app.state, "operation_grants", None)
    try:
        result = gateway.release(grant)
        if not result.ok or result.state not in {"released", "already_released", "committed", "already_committed"}:
            raise OperationPolicyError(result.reason or "operation_policy_unavailable", retry_at=result.retry_at)
        if callable(getattr(store, "transition", None)):
            # 并发幂等请求可能已经由另一执行者提交同一 grant；此时 release 是
            # no-op，必须保留 committed 事实，不能把已计量操作改写为 released。
            state = "committed" if result.state in {"committed", "already_committed"} else "released"
            if not store.transition(grant, state):
                raise OperationPolicyError("operation_grant_invalid")
    except OperationPolicyError:
        if callable(getattr(store, "transition", None)):
            try:
                store.transition(grant, "unknown")
            except OperationPolicyError:
                pass
        raise


def _operation_http_error(exc: OperationPolicyError) -> HTTPException:
    """映射稳定 policy 错误，不泄露 policy 原始诊断。"""
    status = 403 if exc.code == "operation_forbidden" else 429 if exc.code == "operation_limit_exceeded" else 503
    return _error(status, exc.code, str(exc))


def _safe_filename(name: str) -> str:
    """清理上传或人工输入的文件名，不改变扩展名语义。"""
    name = Path(name or "").name
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", stem).strip(" .")
    return f"{stem or 'image'}{suffix}"


def _filename_from_title(title: str, suffix: str) -> str:
    """从自然语言标题派生安全文件名并限制单个文件名长度。"""
    stem = unicodedata.normalize("NFKC", title).strip()
    stem = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", stem).strip(" .")
    if not stem:
        raise ValueError("empty_title")
    suffix = suffix.lower()
    max_stem_bytes = 255 - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:max_stem_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    if not stem:
        raise ValueError("empty_title")
    return f"{stem}{suffix}"


def _parse_multipart_bool(value: object, *, default: bool = False) -> bool:
    """严格解析 multipart 布尔字段，未知文本不再静默变成 False。"""
    if value is None:
        return default
    if type(value) is bool:
        return value
    if not isinstance(value, str):
        raise ImageProcessingError("invalid_auto_name")
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ImageProcessingError("invalid_auto_name")


def _normalize_processing_options(request: Request, *, reverse_image_policy: object = None, auto_name: object = None, check_availability: bool = True) -> ImageProcessingOptions:
    """统一规范化图片选项，并在产生 Job 前校验联网能力。"""
    try:
        options = ImageProcessingOptions.normalize(reverse_image_policy=reverse_image_policy, auto_name=auto_name)
    except ImageProcessingError:
        raise
    if check_availability and options.reverse_image_policy == "auto":
        try:
            available = bool(_service(request, "reverse_image").available)
        except Exception:  # noqa: BLE001 - 能力探测异常必须按不可用处理
            available = False
        if not available:
            raise ImageProcessingError("reverse_image_unavailable")
    return options


def _media_for_meme(request: Request, meme_id: str) -> str | None:
    """将当前 scope 的稳定 meme_id 映射为受控媒体 URL。"""
    try:
        _record, image = _service(request, "metadata").image_for_meme(meme_id)
        return f"/media/{meme_id}"
    except (MetadataError, ValueError):
        return None


def _invalidate_search(request: Request) -> None:
    """通知检索服务；PostgreSQL generation 不在进程内缓存。"""
    invalidate = getattr(_service(request, "search"), "invalidate_cache", None)
    if invalidate:
        invalidate()


def _invalidate_stale_text_embeddings(
    database: DatabaseResources,
    *,
    scope_id: str,
    meme_id: str | UUID,
    image_sha256: str,
    metadata_hash: str,
) -> None:
    """把指定图片旧 metadata hash 的 ready 文本向量标记为失效。

    standalone 图片阶段只负责当前叶子任务的事实收束，不能创建新的文本任务；
    其成功改变图片语境或路径后，由该辅助函数让旧向量退出检索候选。
    """
    with database.factory() as session:
        stale_rows = list(
            session.scalars(
                select(MemeTextEmbedding).where(
                    MemeTextEmbedding.scope_id == scope_id,
                    MemeTextEmbedding.meme_id == UUID(str(meme_id)),
                    MemeTextEmbedding.image_sha256 == image_sha256,
                    MemeTextEmbedding.metadata_hash != metadata_hash,
                    MemeTextEmbedding.status == "ready",
                ).with_for_update()
            )
        )
        for stale in stale_rows:
            stale.status = "failed"
            stale.updated_at = utcnow()
        session.commit()


def _context_payload(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid") -> dict[str, object]:
    """构造可持久化的图片语境任务输入，不保存密钥或提示词。"""
    settings: Settings = request.app.state.settings
    runner: OpenCodeRunner = request.app.state.opencode
    metadata_service = _service(request, "metadata")
    relative = _service(request, "metadata").blob_store.relative(image)
    meme_id = str(metadata_service.meme_id_for_image(image))
    try:
        skill_hash = runner.skill_hash()
    except (OSError, OpenCodeError):
        skill_hash = None
    payload: dict[str, object] = {
        "meme_id": meme_id,
        "image_relative_path": relative,
        "image_sha256": expected_sha256 or metadata_service.image_sha256(image),
        "model": settings.opencode_model,
        "skill_hash": skill_hash,
        "settings_version": settings.settings_version,
        "agent_concurrency": settings.opencode_concurrency,
        "reverse_image_policy": reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_context_task(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid", schedule: bool = True) -> TaskRecord:
    """提交或复用同一图片内容的语境生成任务。"""
    runner: OpenCodeRunner = request.app.state.opencode
    if runner.executor_mode:
        if not runner.executor.configured:
            raise RuntimeError("agent_executor_not_configured")
        runtime = runner.runtime_probe()
        runtime_error = runtime.get("error_code")
        if isinstance(runtime_error, str) and runtime_error:
            raise RuntimeError(runtime_error)
        if not bool(runtime.get("verified")):
            raise RuntimeError("agent_runtime_unavailable")
    if reverse_image_policy not in {"forbid", "auto"}:
        raise RuntimeError("invalid_reverse_image_policy")
    if reverse_image_policy == "auto" and not _service(request, "reverse_image").available:
        raise RuntimeError("reverse_image_unavailable")
    return _service(request, "tasks").submit("meme_context_generation", _context_payload(request, image, auto_name=auto_name, batch_id=batch_id, expected_sha256=expected_sha256, reverse_image_policy=reverse_image_policy), schedule=schedule)


def _visual_payload(request: Request, image: Path, *, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid") -> dict[str, object]:
    """构造视觉任务可序列化 payload，模型身份始终来自服务端配置。"""
    settings: Settings = request.app.state.settings
    identity = identity_from_settings(settings)
    relative = _service(request, "metadata").blob_store.relative(image)
    metadata_service = _service(request, "metadata")
    meme_id = str(metadata_service.meme_id_for_image(image))
    payload: dict[str, object] = {
        "meme_id": meme_id,
        "image_relative_path": relative,
        "image_sha256": expected_sha256 or metadata_service.image_sha256(image),
        "visual_model": identity.model,
        "visual_dimensions": identity.dimensions,
        "preprocess_version": identity.preprocess_version,
        "settings_version": settings.settings_version,
        "reverse_image_policy": reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_visual_task(request: Request, image: Path, *, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid", schedule: bool = True) -> TaskRecord:
    """在图片 durable upload 提交后创建或复用异步视觉任务。"""
    return _service(request, "tasks").submit("visual_embedding_generation", _visual_payload(request, image, batch_id=batch_id, expected_sha256=expected_sha256, reverse_image_policy=reverse_image_policy), schedule=schedule)


def _context_enqueue_error(exc: Exception) -> str:
    """把任务提交异常转换为不暴露内部路径的稳定错误码。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc).split(":", 1)[0]
    return text if text in {"agent_backpressure", "agent_fairness_unavailable", "agent_executor_not_configured", "agent_executor_unavailable", "agent_executor_unauthorized", "agent_runtime_unavailable", "generation_policy_conflict", "processing_options_conflict", "reverse_image_unavailable", "invalid_reverse_image_policy", "invalid_auto_name", "opencode_workspace_provider_missing", "opencode_workspace_invalid", "opencode_workspace_mismatch"} else "context_enqueue_failed"


def _collection_payload(request: Request, environment, row) -> dict[str, object]:
    """构造不暴露 scope 的合集列表摘要。"""
    cover = environment.collections.cover(row.id)
    return {
        "collection_id": str(row.id),
        "name": row.name,
        "member_count": environment.collections.member_count(row.id),
        "cover_media_url": f"/media/{cover.id}" if cover else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _collection_error(exc: DatabaseError) -> HTTPException:
    """将合集 repository 错误映射为稳定 HTTP 契约。"""
    mapping = {
        "collection_not_found": (404, "collection_not_found", "合集不存在"),
        "collection_exists": (409, "collection_exists", "合集名称已存在"),
        "invalid_collection_name": (422, "invalid_collection_name", "合集名称必须为 1 至 100 个字符"),
        "meme_not_found": (404, "meme_not_found", "图片不存在"),
        "empty_members": (422, "empty_members", "至少选择一张图片"),
    }
    status, code, message = mapping.get(exc.code, (400, exc.code, "合集请求无效"))
    return _error(status, code, message)


def _collection_package_error(exc: CollectionPackageError) -> HTTPException:
    """将合集 ZIP 预检和导出错误映射为稳定 HTTP 响应。"""
    status = 413 if exc.code in {"archive_too_large", "file_too_large", "package_too_large", "member_count_exceeded", "manifest_too_large", "compression_ratio_exceeded", "image_frame_count_exceeded", "image_frame_pixels_exceeded", "image_total_pixels_exceeded"} else 409 if exc.code in {"collection_exists", "member_unreadable", "member_changed"} else 400
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
    return _error(status, exc.code, messages.get(exc.code, "合集 ZIP 请求无效"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化一次服务依赖，并在关闭时终止未完成任务。"""
    managed_factory = bool(getattr(app.state, "_scope_factory_managed", False))
    configured_factory = None if managed_factory else getattr(app.state, "service_factory", None)
    custom_factory = configured_factory is not None
    configured_resolver = getattr(app.state, "scope_resolver", None)
    local_mode = isinstance(configured_resolver, LocalScopeResolver) and configured_resolver.scope.scope_id == "local"
    settings = Settings.from_env()
    # 兼容宿主自定义 Settings 夹具；缺失的续跑字段按默认关闭处理。
    resume_enabled = bool(getattr(settings, "agent_resume_enabled", False))
    resume_max_attempts = int(getattr(settings, "agent_resume_max_attempts", 2))
    resume_backoff_seconds = int(getattr(settings, "agent_resume_backoff_seconds", 2))
    resume_max_backoff_seconds = int(getattr(settings, "agent_resume_max_backoff_seconds", 60))
    resume_timeout_seconds = int(getattr(settings, "agent_resume_timeout_seconds", 900))
    configured_policy = getattr(app.state, "operation_policy", None)
    if configured_policy is None and configured_factory is not None:
        configured_policy = getattr(configured_factory, "operation_policy", None)
    if configured_policy is None:
        configured_policy = AllowAllOperationPolicy() if local_mode else UnavailableOperationPolicy()
    if not all(callable(getattr(configured_policy, name, None)) for name in ("probe", "acquire", "commit", "release")):
        raise DatabaseError("operation_policy_unavailable")
    app.state.operation_policy = configured_policy
    app.state.operation_policy_gateway = OperationPolicyGateway(configured_policy, allow_all=isinstance(configured_policy, AllowAllOperationPolicy))
    app.state.operation_grants = GrantAssociationStore()
    callback_verifier = getattr(app.state, "callback_verifier", None)
    callback_issuer = getattr(app.state, "callback_issuer", None)
    if callback_verifier is None and configured_factory is not None:
        callback_verifier = getattr(configured_factory, "callback_verifier", None)
    if callback_issuer is None and configured_factory is not None:
        callback_issuer = getattr(configured_factory, "callback_issuer", None)
    if local_mode and callback_verifier is None and callback_issuer is None:
        try:
            # callback 根 secret 必须由部署显式提供；随机运行时凭据无法交给已启动
            # 的 Agent，也会让重启后的验证边界不可预测，因此不再自动生成。
            callback_credentials = HMACCallbackCredentials(
                getattr(settings, "agent_callback_secret", None),
                verification_keys=_callback_verification_keys(settings),
            )
        except CallbackError:
            callback_credentials = None
        if callback_credentials is not None:
            callback_verifier = callback_credentials
            callback_issuer = callback_credentials
    app.state.callback_verifier = callback_verifier
    app.state.callback_issuer = callback_issuer
    app.state.callback_registry = getattr(app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
    configured_agent_input_provider = getattr(app.state, "agent_input_provider", None)
    if configured_agent_input_provider is None and configured_factory is not None:
        configured_agent_input_provider = getattr(configured_factory, "agent_input_provider", None)
    settings.ensure_directories()
    configured_workspace_provider = getattr(app.state, "workspace_provider", None)
    if configured_workspace_provider is None and configured_factory is not None:
        configured_workspace_provider = getattr(configured_factory, "workspace_provider", None)
    if local_mode:
        if configured_workspace_provider is None:
            configured_workspace_provider = LocalWorkspaceProvider(
                settings.opencode_runtime_root,
                image_root=settings.image_root,
                skill_root=Path(__file__).resolve().parent / "skills" / "research-meme-context",
            )
    elif configured_workspace_provider is None:
        # 先允许 non-local 应用完成无业务副作用的生命周期装配；真正任务执行时
        # 由占位 provider 稳定拒绝，绝不静默回退 local。
        configured_workspace_provider = MissingWorkspaceProvider()
    app.state.workspace_provider = configured_workspace_provider
    try:
        engine = create_engine_for_settings(settings)
        expected_revision = getattr(app.state, "expected_schema_revision", settings.expected_database_revision)
        check_database(engine, expected_revision=expected_revision, require_local_installation=local_mode)
    except DatabaseError:
        # 启动门禁拒绝任何可能回退到旧 JSON 的业务请求；测试和生产都必须显式准备 PostgreSQL。
        raise
    app.state.settings = settings
    app.state.database = DatabaseResources(
        engine,
        image_root=settings.image_root,
        data_root=settings.data_root,
        settings=settings,
        require_local_scope=local_mode,
    )
    # grant 关联以 PostgreSQL 为跨进程事实来源；内存层只做同进程热点缓存。
    app.state.operation_grants = PersistentGrantAssociationStore(app.state.database)
    if local_mode:
        app.state.resolver = PathResolver(settings.image_root)
        preflight = app.state.database.flat_preflight(ScopeContext("local"))
        # 根目录孤立图片不会进入数据库图片库，完整性任务会报告它们；它们本身不应阻断主服务启动。
        # 非扁平记录、嵌套图片和已登记文件的不一致仍然阻断，避免受控媒体接口读到错误对象。
        app.state.storage_preflight = preflight
        if any(preflight.get(key) for key in STORAGE_PREFLIGHT_BLOCKING_KEYS):
            raise DatabaseError("flat_meme_storage_preflight_failed")
    else:
        # 适配宿主由自己的 resolver/factory 管理 scope，不探测或创建 local namespace。
        app.state.storage_preflight = None
    # 先按旧构造方式创建 runner，再安装 provider，兼容宿主测试夹具和旧扩展工厂。
    app.state.opencode = OpenCodeRunner(settings)
    app.state.opencode.workspace_provider = configured_workspace_provider
    try:
        app.state.agent_activity = OpenCodeActivityReader(settings.opencode_runtime_root)
    except Exception:  # noqa: BLE001
        # 活跃度是可选观测，runtime 配置异常不能阻止任务服务启动。
        app.state.agent_activity = None
    # 后端只保存推理客户端和 scope-bound 查询服务；视觉模型本体位于独立 CPU 容器。
    app.state.visual_inference = VisualInferenceClient(settings)
    factory: ScopeServiceFactory | Any | None = configured_factory
    shared_worker_executor: ThreadPoolExecutor | None = None
    worker_manager: PostgresTaskWorkerManager | None = None
    local_services: ScopeServices | None = None
    if factory is not None:
        # 适配宿主可以提供自有 factory；启动时只校验协议，不调用 for_scope(local)。
        required_methods = ("for_scope", "for_task", "start_all", "shutdown")
        if any(not callable(getattr(factory, name, None)) for name in required_methods):
            raise DatabaseError("scope_service_factory_invalid")
    else:
        shared_worker_executor = ThreadPoolExecutor(
            max_workers=max(2, settings.opencode_concurrency + 1),
            thread_name_prefix="mememeow-scope-worker",
        )
        worker_manager = PostgresTaskWorkerManager(
            app.state.database,
            agent_concurrency=settings.opencode_concurrency,
            scope_concurrency=getattr(settings, "agent_scope_concurrency", 1),
            agent_backpressure=settings.agent_backpressure,
            settings_version=settings.settings_version,
            lease_seconds=settings.worker_lease_seconds,
            max_attempts=settings.worker_max_attempts,
            executor=shared_worker_executor,
        )
        if local_mode:
            local_scope = ScopeContext("local")
            local_metadata = PostgresMetadataService(app.state.database, scope_id=local_scope)
            local_search = PostgresSearchService(settings, app.state.database, local_metadata, scope_id=local_scope)
            try:
                local_reverse_image = ReverseImageService(
                    settings,
                    app.state.database,
                    scope_id=local_scope,
                    operation_policy=app.state.operation_policy_gateway,
                    grant_store=app.state.operation_grants,
                )
            except TypeError as exc:
                if "operation_policy" not in str(exc) and "grant_store" not in str(exc):
                    raise
                # 兼容适配宿主尚未升级的轻量 facade 夹具；真实服务支持 policy 参数。
                local_reverse_image = ReverseImageService(settings, app.state.database, scope_id=local_scope)
            local_visual_search = VisualSearchService(settings, app.state.database, scope_id=local_scope)
            local_tasks = PostgresTaskService(
                app.state.database,
                scope_id=local_scope,
                agent_concurrency=settings.opencode_concurrency,
                scope_concurrency=getattr(settings, "agent_scope_concurrency", 1),
                agent_backpressure=settings.agent_backpressure,
                settings_version=settings.settings_version,
                lease_seconds=settings.worker_lease_seconds,
                max_attempts=settings.worker_max_attempts,
                resume_enabled=resume_enabled,
                resume_max_attempts=resume_max_attempts,
                resume_backoff_seconds=resume_backoff_seconds,
                resume_max_backoff_seconds=resume_max_backoff_seconds,
                resume_timeout_seconds=resume_timeout_seconds,
                worker_manager=worker_manager,
            )
            local_services = ScopeServices(
                local_scope,
                local_metadata,
                local_search,
                local_tasks,
                local_reverse_image,
                local_visual_search,
            )
    tasks = local_services.tasks if local_services is not None else None

    def services_for_task(payload: dict[str, object]) -> ScopeServices:
        """只从当前 claim 注入的 Task.scope_id 恢复后台服务环境。"""
        scope_id = payload.get("_claim_scope_id")
        if not isinstance(scope_id, str) or not scope_id:
            raise RuntimeError("task_scope_invalid")
        try:
            scope = ScopeContext(scope_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("task_scope_invalid") from exc
        if factory is None:
            raise RuntimeError("task_scope_unavailable")
        return validate_scope_services(scope, factory.for_scope(scope))

    def cache_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建缓存生成工作。"""
        claim = None
        if isinstance(payload.get("_claim_task_id"), str) and isinstance(payload.get("_claim_generation"), int) and isinstance(payload.get("_claim_owner"), str):
            claim = (str(payload["_claim_task_id"]), int(payload["_claim_generation"]), str(payload["_claim_owner"]))
        service = services_for_task(payload)
        override = getattr(app.state, "search_engine", None) if service.scope.scope_id == "local" else None
        if override is not None and override is not service.search and (claim is None or not isinstance(override, PostgresSearchService)):
            # 仅保留开源 local 单元夹具覆盖；non-local 任务始终使用其持久 scope service。
            return override.generate_cache(progress)
        if claim is None:
            result = service.search.generate_cache(progress)
        else:
            result = service.search.generate_cache(progress, claim=claim)
        return result

    def repair_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建数据库元数据完整性扫描工作。"""
        service = services_for_task(payload)
        result = service.metadata.repair(progress)
        service.search.invalidate_cache()
        return result

    def visual_handler(payload: dict[str, object], progress):
        """在事务外推理、事务内写向量并幂等提交 Agent 任务。"""
        service = services_for_task(payload)
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        if not isinstance(meme_id, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            _record, image = service.metadata.image_for_meme(meme_id)
            current_sha = service.metadata.image_sha256(image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        if progress:
            progress(0.1, "正在生成视觉向量")
        try:
            with image.open("rb") as handle:
                content = handle.read()
            response = app.state.visual_inference.embed(content, filename=image.name)
        except VisualEmbeddingError as exc:
            raise RuntimeError(f"{exc.code}: {exc}") from exc
        identity = identity_from_settings(app.state.settings)
        if response.get("model") != identity.model or int(response.get("dimensions", -1)) != identity.dimensions or response.get("preprocess_version") != identity.preprocess_version:
            raise RuntimeError("visual_model_identity_mismatch")
        with app.state.database.environment(service.scope) as environment:
            meme = environment.memes.get(meme_id, for_update=True)
            if meme is None:
                raise RuntimeError("target_changed")
            try:
                latest_image = service.metadata.blob_store.resolve(meme.storage_key)
                latest_sha = service.metadata.image_sha256(latest_image)
            except MetadataError as exc:
                raise RuntimeError("target_changed") from exc
            if meme.sha256 != expected_sha or latest_sha != expected_sha:
                raise RuntimeError("target_changed")
            environment.visual.upsert(
                meme.id,
                model=identity.model,
                preprocess_version=identity.preprocess_version,
                dimensions=identity.dimensions,
                image_sha256=expected_sha,
                embedding=response.get("embedding") or [],
            )
        # 视觉阶段无论属于完整 Job 还是 standalone，都只写视觉产物；后续
        # Agent/文本阶段必须由 ImageProcessingWorker 的 pipeline 控制面显式创建。
        if progress:
            progress(1.0, "视觉向量已保存")
        return {"meme_id": meme_id, "visual_model": identity.model, "dimensions": identity.dimensions, "preprocess_version": identity.preprocess_version}

    def context_handler(payload: dict[str, object], progress):
        """执行 Agent 候选校验、指纹复核与受保护数据库语境写回。"""
        service = services_for_task(payload)
        relative = payload.get("image_relative_path")
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        if not isinstance(meme_id, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            _record, image = service.metadata.image_for_meme(meme_id)
            relative = service.metadata.blob_store.relative(image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        try:
            current_sha = service.metadata.image_sha256(image)
        except MetadataError as exc:
            # Agent 运行期间图片可能被删除；这属于提交目标变化而非普通任务故障。
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        policy = str(payload.get("reverse_image_policy") or "forbid")
        payload["reverse_image_policy"] = policy if policy in {"forbid", "auto"} else "forbid"
        # Agent grant 与逻辑 Task 绑定；execution attempt 不重新 acquire。只有已
        # 持久关联的 grant 才能 commit，客户端无法通过 payload 伪造授权。
        grant = None
        grant_request = None
        grants = getattr(app.state, "operation_grants", None)
        gateway = getattr(app.state, "operation_policy_gateway", None)
        config_hash = str(payload.get("processing_config_hash") or hashlib.sha256(json.dumps({"model": payload.get("model"), "skill_hash": payload.get("skill_hash"), "settings_version": payload.get("settings_version"), "visual_model": payload.get("visual_model"), "visual_dimensions": payload.get("visual_dimensions"), "preprocess_version": payload.get("preprocess_version")}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        mode = payload.get("submission_mode")
        if mode == "standalone":
            logical_key = payload.get("agent_grant_key")
            if not isinstance(logical_key, str) or not logical_key.startswith("standalone-agent:"):
                raise RuntimeError("operation_grant_invalid")
            grant_source = "image-processing-standalone"
        else:
            revision = payload.get("job_revision") or "legacy"
            logical_key = f"agent:{meme_id}:{expected_sha}:{config_hash}:{payload['reverse_image_policy']}:r{revision}"
            grant_source = "image-processing"
        if grants is not None and gateway is not None:
            claim_task_id = payload.get("_claim_task_id")
            if not isinstance(claim_task_id, str) or not claim_task_id:
                raise RuntimeError("operation_grant_invalid")
            grant_request = gateway.request(service.scope, Operations.ANALYSIS_AGENT, logical_key, resource_id=meme_id, task_id=claim_task_id, source=grant_source, input_digest=expected_sha)
            association = grants.get(grant_request)
            if association is None and mode == "standalone":
                raise RuntimeError("operation_grant_invalid")
            if association is not None:
                if association.state not in {"acquired", "committed"}:
                    raise RuntimeError("operation_grant_invalid")
                grant = association.grant
                try:
                    if association.state == "acquired":
                        commit_result = gateway.commit(grant)
                        if not commit_result.ok:
                            raise OperationPolicyError(commit_result.reason or "operation_policy_unavailable", retry_at=commit_result.retry_at)
                        if callable(getattr(grants, "transition", None)):
                            grants.transition(grant, "committed")
                        association.state = "committed"
                except OperationPolicyError as exc:
                    raise RuntimeError(exc.code) from exc
        elif mode == "standalone":
            raise RuntimeError("operation_grant_invalid")
        callback_token: str | None = None
        claim_task_id = payload.get("_claim_task_id")
        claim_generation = payload.get("_claim_generation")
        claim_owner = payload.get("_claim_owner")
        claim_attempt = payload.get("_claim_attempt")
        issuer = getattr(app.state, "callback_issuer", None)
        if not isinstance(claim_task_id, str) or not isinstance(claim_generation, int) or not isinstance(claim_owner, str) or not isinstance(claim_attempt, int) or claim_generation < 1 or claim_attempt < 1 or not callable(getattr(issuer, "issue", None)):
            # Agent callback 必须绑定当前持久 claim；没有发行能力不能把内网地址当作授权。
            raise RuntimeError("agent_callback_unavailable")
        try:
            # 宿主 issuer 可以收紧有效期，但不能把核心两小时上限继续放大；未暴露
            # TTL 的旧 issuer 保留原有 120 秒契约，避免升级后突然收到超长 binding。
            callback_ttl_seconds = min(
                AGENT_CALLBACK_TOKEN_TTL_SECONDS,
                int(getattr(issuer, "ttl_seconds", 120)),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("agent_callback_unavailable") from exc
        if callback_ttl_seconds <= 0:
            raise RuntimeError("agent_callback_unavailable")
        with app.state.database.environment(service.scope) as environment:
            claimed_task = environment.tasks.get(claim_task_id)
            if claimed_task is None or getattr(claimed_task, "lease_expires_at", None) is None:
                raise RuntimeError("agent_callback_invalid_execution")
            try:
                binding = CallbackBinding(
                    task_id=claim_task_id,
                    scope_id=service.scope.scope_id,
                    claim_generation=claim_generation,
                    owner=claim_owner,
                    attempt=claim_attempt,
                    operation=",".join(sorted({"analysis.reverse_image_search", "analysis.visual_search"})),
                    target_sha256=expected_sha,
                    issuer=str(getattr(issuer, "issuer", "mememeow")),
                    audience=str(getattr(issuer, "audience", "mememeow-internal")),
                    expires_at=datetime.now(timezone.utc) + timedelta(seconds=callback_ttl_seconds),
                    key_id=str(getattr(issuer, "key_id", "default")),
                )
                validate_binding_task(binding, claimed_task)
                callback_token = issuer.issue(binding)
            except CallbackError as exc:
                raise RuntimeError(exc.code) from exc
        if grant is not None:
            # 计量提交与图片叶子 Task 的 attempt 绑定，恢复器据此禁止把已计量
            # 的外部执行窗口当作可安全重放的普通失败。
            mark_attempt = getattr(service.tasks, "_image_attempt_state", None)
            if callable(mark_attempt) and claimed_task is not None:
                mark_attempt(claimed_task, payload, "grant_committed")
        agent_image = image
        provider = getattr(app.state, "agent_input_provider", None)
        if service.scope.scope_id != "local" and callable(provider):
            try:
                try:
                    provided = provider(service.scope, image)
                except TypeError:
                    provided = provider(service.scope.scope_id, image)
                agent_image = Path(provided).expanduser()
                if agent_image.is_symlink() or not agent_image.is_file():
                    raise ValueError("agent_input_invalid")
                agent_image = agent_image.resolve()
                # provider 只能把同一任务图片映射到 Agent 可见的受控文件，不能借
                # 适配层替换成另一 scope 的内容或把任意文件交给 Runner。
                if service.metadata.image_sha256(agent_image) != expected_sha:
                    raise ValueError("agent_input_sha256_mismatch")
                # non-local provider 会在 runner 内按当前 selector 的 images_root
                # 重新解析 image_relative_path；不能用 local 的全局 /images 映射
                # 把宿主提供的 scope 视图误判成越界路径。
                if service.scope.scope_id == "local":
                    map_image_path = getattr(app.state.opencode, "map_image_path", None)
                    if callable(map_image_path):
                        map_image_path(agent_image)
            except (MetadataError, OSError, OpenCodeError, TypeError, ValueError) as exc:
                raise RuntimeError("agent_input_provider_unavailable") from exc
        # callback token 是 Agent 内部接口的授权凭据，Runner 不支持显式传递时必须
        # 让任务失败，不能以兼容调用的名义退回无凭据执行。
        try:
            try:
                workspace_context = TrustedWorkspaceContext(
                    task_id=claim_task_id,
                    attempt_id=f"claim-{claim_attempt}",
                    scope_id=service.scope.scope_id,
                    selector=str(payload.get("_workspace_selector")) if isinstance(payload.get("_workspace_selector"), str) else None,
                    session_id=payload.get("_resume_session_id") if isinstance(payload.get("_resume_session_id"), str) else None,
                    resume_of_attempt_id=payload.get("_resume_of_attempt_id") if isinstance(payload.get("_resume_of_attempt_id"), str) else None,
                    image_relative_path=relative if isinstance(relative, str) else None,
                )
            except WorkspaceResolutionError as exc:
                raise OpenCodeError(exc.code, str(exc)) from exc
            candidate, session_id = app.state.opencode.run(
                agent_image,
                progress,
                task_id=claim_task_id,
                reverse_image_policy=str(payload.get("reverse_image_policy") or "forbid"),
                callback_token=callback_token,
                resume_session_id=payload.get("_resume_session_id") if isinstance(payload.get("_resume_session_id"), str) else None,
                resume_of_attempt_id=payload.get("_resume_of_attempt_id") if isinstance(payload.get("_resume_of_attempt_id"), str) else None,
                processing_config_hash=config_hash,
                workspace_context=workspace_context,
            )
        except OpenCodeError as exc:
            # 续跑候选已由 Worker 按持久 session、scope、输入摘要和配置 hash
            # 校验；executor 的 HTTP/连接错误可能没有重复回传 session，此时沿用
            # 该受信候选，避免把合法续跑误收束为 unknown_execution。
            inherited_session_id = payload.get("_resume_session_id") if payload.get("_resume_available") is True else None
            if not normalize_identifier(inherited_session_id, kind="session"):
                inherited_session_id = None
            failure_session_id = getattr(exc, "session_id", None) or inherited_session_id
            decision = classify_resume_error(
                exc.code,
                session_id=failure_session_id,
                external_started=False,
                result_valid=False,
                target_unchanged=True,
                grant_state="committed" if grant is not None else None,
            )
            if not resume_enabled:
                # rollout 关闭时保留旧任务级 retry，但不得把 session 标成可续跑；
                # 否则开关热切换或任务详情会误把旧失败当成恢复目标。
                decision = ResumeDecision(False, "resume_disabled", False)
            else:
                # 当前 claim 的续跑次数和累计窗口属于服务端事实；额度耗尽时不能
                # 暂时把最后一次失败暴露为仍可恢复，避免队列状态与恢复边界分叉。
                raw_resume_attempts = payload.get("_resume_attempt_count", 0)
                resume_attempts = int(raw_resume_attempts) if isinstance(raw_resume_attempts, int) and not isinstance(raw_resume_attempts, bool) else 0
                resume_started_at = payload.get("_resume_started_at")
                if resume_attempts >= resume_max_attempts:
                    decision = ResumeDecision(False, "resume_budget_exhausted", False)
                elif isinstance(resume_started_at, datetime) and not within_total_timeout(resume_started_at, timeout_seconds=resume_timeout_seconds):
                    decision = ResumeDecision(False, "resume_budget_exhausted", False)
            payload["_resume_available"] = decision.available
            payload["_resume_reason"] = decision.reason
            if failure_session_id:
                payload["_resume_session_id"] = failure_session_id
            if getattr(exc, "executor_attempt_id", None):
                payload["_executor_attempt_id"] = exc.executor_attempt_id
            selector_reader = getattr(app.state.opencode, "workspace_for_task", None)
            if callable(selector_reader):
                selector = selector_reader(claim_task_id)
                if isinstance(selector, str):
                    payload["_workspace_selector"] = selector
            record_attempt = getattr(service.tasks, "record_agent_attempt", None)
            try:
                if callable(record_attempt):
                    recorded = record_attempt(
                        payload,
                        error={"error": exc.code, "message": str(exc), **({"http_status": exc.http_status} if getattr(exc, "http_status", None) else {})},
                        session_id=failure_session_id,
                        executor_attempt_id=getattr(exc, "executor_attempt_id", None),
                        workspace_selector=payload.get("_workspace_selector") if isinstance(payload.get("_workspace_selector"), str) else None,
                        resume_available=decision.available,
                        resume_reason=decision.reason,
                    )
                    if recorded is False:
                        raise RuntimeError("unknown_execution: Agent attempt 事实未能通过 claim fencing 保存")
                elif resume_enabled:
                    raise RuntimeError("unknown_execution: Agent attempt 持久化能力不可用")
            except Exception as persist_error:  # noqa: BLE001 - 外部执行后的持久化失败必须禁止重放
                raise RuntimeError("unknown_execution: Agent attempt 事实无法保存") from persist_error
            try:
                service.metadata.record_error(image, producer="research", model=app.state.settings.opencode_model, error=exc.code)
            except MetadataError:
                pass
            raise RuntimeError(f"{exc.code}: {exc}") from exc
        try:
            current_sha = service.metadata.image_sha256(image)
        except MetadataError as exc:
            # Agent 运行期间图片可能被删除；这属于提交目标变化而非普通任务故障。
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        payload["_resume_session_id"] = session_id
        attempt_reader = getattr(app.state.opencode, "executor_attempt_id_for", None)
        executor_attempt_id = attempt_reader(claim_task_id) if callable(attempt_reader) else getattr(app.state.opencode, "last_executor_attempt_id", None)
        if isinstance(executor_attempt_id, str):
            payload["_executor_attempt_id"] = executor_attempt_id
        selector_reader = getattr(app.state.opencode, "workspace_for_task", None)
        if callable(selector_reader):
            selector = selector_reader(claim_task_id)
            if isinstance(selector, str):
                payload["_workspace_selector"] = selector
        record_attempt = getattr(service.tasks, "record_agent_attempt", None)
        try:
            if callable(record_attempt):
                recorded = record_attempt(
                    payload,
                    session_id=session_id,
                    executor_attempt_id=executor_attempt_id,
                    workspace_selector=payload.get("_workspace_selector") if isinstance(payload.get("_workspace_selector"), str) else None,
                    resume_available=False,
                )
                if recorded is False:
                    raise RuntimeError("unknown_execution: Agent attempt 事实未能通过 claim fencing 保存")
            elif resume_enabled:
                raise RuntimeError("unknown_execution: Agent attempt 持久化能力不可用")
        except Exception as persist_error:  # noqa: BLE001 - 成功外部执行也不能在事实丢失时重放
            raise RuntimeError("unknown_execution: Agent attempt 事实无法保存") from persist_error
        claim = None
        if isinstance(payload.get("_claim_task_id"), str) and isinstance(payload.get("_claim_generation"), int) and isinstance(payload.get("_claim_owner"), str):
            claim = (str(payload["_claim_task_id"]), int(payload["_claim_generation"]), str(payload["_claim_owner"]))
        try:
            metadata = service.metadata.update_context(
                image,
                candidate,
                producer="research",
                model=app.state.settings.opencode_model,
                status="ready",
                error=None,
                expected_sha256=expected_sha,
                claim=claim,
                agent_context={
                    "task_id": str(payload.get("_claim_task_id") or ""),
                    "image_sha256": expected_sha,
                    "model": app.state.settings.opencode_model,
                    "skill_hash": payload.get("skill_hash"),
                    "processing_config_hash": config_hash,
                    "reverse_image_policy": payload["reverse_image_policy"],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except MetadataError as exc:
            if exc.code == "claim_expired":
                raise RuntimeError("target_changed") from exc
            raise RuntimeError("agent_output_schema_invalid") from exc
        mark_invalidated = getattr(service.search, "mark_cache_invalidated", None)
        if mark_invalidated:
            mark_invalidated(payload.get("batch_id"))
        else:
            service.search.invalidate_cache()
        result: dict[str, object] = {
            "image_relative_path": relative,
            "meme_id": meme_id,
            "session_id": session_id,
            "result_artifact": f"task-results/{payload.get('_claim_task_id', '')}/result.json.tmp",
            "reverse_image_policy": payload["reverse_image_policy"],
        }
        metadata_hash = service.metadata.embedding_record(image)["metadata_hash"]
        if mode == "standalone":
            # 独立 Agent 只使旧文本向量失效；这里不创建文本 Task，也不触碰
            # image_processing_jobs 的 reconcile 状态。
            if not isinstance(metadata_hash, str) or not metadata_hash:
                raise RuntimeError("target_changed")
            _invalidate_stale_text_embeddings(
                app.state.database,
                scope_id=service.scope.scope_id,
                meme_id=meme_id,
                image_sha256=expected_sha,
                metadata_hash=metadata_hash,
            )
        try:
            result["metadata_hash"] = service.metadata.embedding_record(image)["metadata_hash"]
        except MetadataError:
            result["metadata_hash"] = metadata_hash
        return result

    def auto_rename_handler(payload: dict[str, object], progress):
        """在当前图片 Task claim 内校验并执行服务端派生的安全重命名。"""
        service = services_for_task(payload)
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        expected_storage_key = payload.get("expected_storage_key")
        expected_revision = payload.get("expected_meme_revision")
        expected_title_fingerprint = payload.get("title_fingerprint")
        claim_task_id = payload.get("_claim_task_id")
        claim_generation = payload.get("_claim_generation")
        claim_attempt = payload.get("_claim_attempt")
        claim_owner = payload.get("_claim_owner")
        if not all(isinstance(value, str) and value for value in (meme_id, expected_sha, expected_storage_key, expected_title_fingerprint, claim_task_id, claim_owner)) or not isinstance(expected_revision, int) or not isinstance(claim_generation, int) or not isinstance(claim_attempt, int):
            raise RuntimeError("auto_rename_claim_expired")
        try:
            record, image = service.metadata.image_for_meme(meme_id)
            current_sha = service.metadata.image_sha256(image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha or record.sha256 != expected_sha:
            raise RuntimeError("target_changed")
        try:
            raw_context = record.meme_context if isinstance(record.meme_context, Mapping) else {}
            raw_title = raw_context.get("title") if isinstance(raw_context, Mapping) else None
            title = raw_title.strip() if isinstance(raw_title, str) else ""
        except (AttributeError, TypeError, ValueError) as exc:
            # 语境结构损坏只影响候选命名，不代表图片身份或执行权失效。
            raise RuntimeError("auto_rename_title_missing") from exc
        if not title:
            raise RuntimeError("auto_rename_title_missing")
        title_fingerprint = stable_input_digest(title)
        if title_fingerprint != expected_title_fingerprint:
            raise RuntimeError("target_changed")
        # 同一 SHA 的 storage key 已被用户手动改名时仅产生 warning；revision
        # 或语境单独漂移仍必须停止 Job，不能把 CAS race 统称为可恢复命名失败。
        if record.storage_key != expected_storage_key:
            if record.sha256.lower() == expected_sha.lower() and title_fingerprint == expected_title_fingerprint:
                raise RuntimeError("auto_rename_target_changed")
            raise RuntimeError("target_changed")
        if record.revision != expected_revision:
            raise RuntimeError("target_changed")
        try:
            target_name = _filename_from_title(title, image.suffix or record.extension)
        except ValueError as exc:
            raise RuntimeError("auto_rename_invalid_filename") from exc
        target = image.with_name(target_name)
        same_name = target == image
        try:
            renamed = service.metadata.storage.rename_if_current(
                record.id,
                target_key=service.metadata.blob_store.relative(target),
                expected_source_key=expected_storage_key,
                expected_sha256=expected_sha,
                expected_revision=expected_revision,
                task_id=claim_task_id,
                claim_generation=claim_generation,
                attempt=claim_attempt,
                claim_owner=claim_owner,
                expected_title_fingerprint=expected_title_fingerprint,
            )
        except DatabaseError as exc:
            if exc.code == "target_exists":
                raise RuntimeError("auto_rename_target_exists") from exc
            if exc.code == "target_changed":
                # CAS 同时检测 revision/语境/SHA；这些漂移不是同图人工改名，
                # 必须停止 Job，不能伪装成可恢复 warning。
                raise RuntimeError("target_changed") from exc
            if exc.code == "storage_key_changed":
                raise RuntimeError("auto_rename_target_changed") from exc
            if exc.code == "claim_expired":
                raise RuntimeError("auto_rename_claim_expired") from exc
            if exc.code in {"storage_operation_unknown", "storage_operation_missing"}:
                raise RuntimeError("auto_rename_unknown_execution") from exc
            if exc.code == "invalid_filename":
                raise RuntimeError("auto_rename_invalid_filename") from exc
            raise RuntimeError("auto_rename_unknown_execution") from exc
        except OSError as exc:
            raise RuntimeError("auto_rename_unknown_execution") from exc
        if renamed is None:
            # 文件移动后无法重新读取 Meme 记录时，副作用结果无法证明，不能把
            # ``None`` 当作普通命名失败或让任务服务退化成 task_failed。
            raise RuntimeError("auto_rename_unknown_execution")
        if payload.get("submission_mode") == "standalone":
            # 重命名改变 sidecar image.relative_path，独立阶段不创建文本 Task，
            # 但必须让重命名前绑定旧路径的 ready 向量退出检索候选。
            metadata_hash = ImageProcessingWorker._metadata_hash(renamed)
            if metadata_hash is None:
                raise RuntimeError("auto_rename_unknown_execution")
            _invalidate_stale_text_embeddings(
                app.state.database,
                scope_id=service.scope.scope_id,
                meme_id=meme_id,
                image_sha256=expected_sha,
                metadata_hash=metadata_hash,
            )
        if progress:
            progress(1.0, "文件名已经符合标题" if same_name else "自动重命名已完成")
        if not same_name:
            # 独立重命名改变了 storage_key，当前 scope 的检索缓存不能继续
            # 使用旧 metadata hash；任务 handler 只能通过已解析的 service 失效缓存。
            service.search.invalidate_cache()
        return {"meme_id": meme_id, "saved_filename": renamed.storage_key.rsplit("/", 1)[-1], "auto_named": not same_name}

    def text_embedding_handler(payload: dict[str, object], progress):
        """为统一图片处理 job 的当前语境生成单图文本向量。"""
        service = services_for_task(payload)
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        if not isinstance(meme_id, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            _record, image = service.metadata.image_for_meme(meme_id)
            embedding_record = service.metadata.embedding_record(image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        frozen_metadata_hash = payload.get("metadata_hash")
        if frozen_metadata_hash is not None and (
            not isinstance(frozen_metadata_hash, str)
            or len(frozen_metadata_hash) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in frozen_metadata_hash)
        ):
            raise RuntimeError("target_changed")
        current_metadata_hash = embedding_record.get("metadata_hash")
        if embedding_record.get("image_sha256") != expected_sha:
            raise RuntimeError("target_changed")
        if frozen_metadata_hash is not None and current_metadata_hash != frozen_metadata_hash:
            raise RuntimeError("target_changed")
        if not isinstance(current_metadata_hash, str) or not current_metadata_hash:
            raise RuntimeError("target_changed")
        frozen_metadata_hash = frozen_metadata_hash or current_metadata_hash
        if not embedding_record.get("indexable"):
            raise RuntimeError("query_embedding_not_ready")
        if progress:
            progress(0.1, "正在生成单图文本向量")
        embedder = getattr(service.search, "_embedding", None)
        if not callable(embedder):
            raise RuntimeError("embedding_not_configured")
        embedding_service = SingleImageEmbeddingService(
            app.state.database,
            scope_id=service.scope,
            model=app.state.settings.embedding_model,
            dimensions=1024,
            embedder=embedder,
        )
        embedding_service.upsert(
            meme_id,
            image_sha256=expected_sha,
            metadata_hash=frozen_metadata_hash,
            semantic_document=str(embedding_record.get("text") or ""),
        )
        # upsert 已在写事务内校验一次；写回后再次读取当前 Meme，确保并发语境或
        # storage key 变化不会被当作本次冻结 hash 的成功结果。
        try:
            _current_record, current_image = service.metadata.image_for_meme(meme_id)
            after_record = service.metadata.embedding_record(current_image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        if after_record.get("image_sha256") != expected_sha or after_record.get("metadata_hash") != frozen_metadata_hash:
            raise RuntimeError("target_changed")
        if progress:
            progress(1.0, "单图文本向量已保存")
        return {"meme_id": meme_id, "metadata_hash": frozen_metadata_hash, "embedding_model": app.state.settings.embedding_model}

    def register_handlers(services: ScopeServices | None = None) -> None:
        """向进程级 manager 注册处理器，并为 scope facade 安装批次收束回调。"""
        register = services.tasks.register if services is not None else getattr(worker_manager, "register", None)
        if not callable(register):
            return
        register("cache_generation", cache_handler)
        register("metadata_repair", repair_handler)
        register("visual_embedding_generation", visual_handler)
        register("meme_context_generation", context_handler)
        register("image_auto_rename", auto_rename_handler)
        if services is not None:
            register("text_embedding_generation", text_embedding_handler)
        # 图片处理阶段由 ImageProcessingWorker 的 job 状态推进；不再把视觉或
        # Agent 批次终态隐式转换成全库 cache_generation。显式 /generate-cache
        # 仍通过普通任务入口保留维护能力。

    def start_services(services: ScopeServices) -> None:
        """恢复指定 scope 的存储操作、过期 claim 和待处理任务。"""
        services.metadata.recover_storage(limit=500)
        services.tasks.start()

    factory = ScopeServiceFactory(
        app.state.database,
        settings,
        task_config={
            "agent_concurrency": settings.opencode_concurrency,
            "scope_concurrency": getattr(settings, "agent_scope_concurrency", 1),
            "agent_backpressure": settings.agent_backpressure,
            "settings_version": settings.settings_version,
            "lease_seconds": settings.worker_lease_seconds,
            "max_attempts": settings.worker_max_attempts,
            "resume_enabled": resume_enabled,
            "resume_max_attempts": resume_max_attempts,
            "resume_backoff_seconds": resume_backoff_seconds,
            "resume_max_backoff_seconds": resume_max_backoff_seconds,
            "resume_timeout_seconds": resume_timeout_seconds,
            "executor": shared_worker_executor,
            "operation_policy": app.state.operation_policy_gateway,
            "grant_store": app.state.operation_grants,
            "register_handlers": register_handlers,
            "start_services": start_services,
        },
        preloaded={"local": local_services} if local_services is not None else None,
        worker_manager=worker_manager,
    ) if factory is None else factory
    if worker_manager is not None:
        # 非 local 默认 factory 不预加载 scope；先注册全局 handler，避免启动恢复任务时
        # 在首次按 Task.scope_id 创建 facade 前被误判为 task_handler_missing。
        register_handlers()
    if local_services is not None:
        app.state.metadata = local_services.metadata
        app.state.search_engine = local_services.search
        app.state.reverse_image = local_services.reverse_image
        app.state.visual_search = local_services.visual_search
        register_handlers(local_services)
        local_services.metadata.recover_storage(limit=500)
    app.state.service_factory = factory
    app.state._scope_factory_managed = not custom_factory
    app.state.agent_input_provider = configured_agent_input_provider
    app.state.image_processing_task_handlers = {
        "visual_embedding_generation": visual_handler,
        "meme_context_generation": context_handler,
        "image_auto_rename": auto_rename_handler,
        "text_embedding_generation": text_embedding_handler,
    }
    app.state.image_processing_workers = {}
    app.state.image_processing_workers_lock = RLock()
    if local_services is not None:
        # local 入口显式启动逐图控制面；叶子 Task 仍由共享任务 Worker 执行。
        app.state.image_processing_workers[local_services.scope.scope_id] = ImageProcessingWorker(
            app.state.database,
            scope_id=local_services.scope,
            task_service=local_services.tasks,
            policy=app.state.operation_policy_gateway,
            grant_store=app.state.operation_grants,
            max_workers=validate_agent_concurrency(
                getattr(settings, "opencode_concurrency", 1),
                backpressure=getattr(settings, "agent_backpressure", None),
            ),
            task_handlers={
                "visual_embedding_generation": visual_handler,
                "meme_context_generation": context_handler,
                "image_auto_rename": auto_rename_handler,
                "text_embedding_generation": text_embedding_handler,
            },
        )
        if callable(getattr(app.state.database, "factory", None)):
            app.state.image_processing_workers[local_services.scope.scope_id].start()
    if tasks is not None:
        app.state.tasks = tasks
    app.state.task_scope_diagnostics = factory.start_all()
    started_extensions: list[ApplicationExtension] = []
    try:
        # 扩展启动发生在公共数据库、factory 和 Worker 全部就绪之后，适配器可以
        # 在这里绑定自己的运行时资源，而不让公共核心知道其具体业务语义。
        for extension in _extension_list(app):
            await _invoke_extension_hook(extension, "on_startup", app)
            started_extensions.append(extension)
        yield
    finally:
        for extension in reversed(started_extensions):
            await _invoke_extension_hook(extension, "on_shutdown", app)
        app.state.opencode.shutdown()
        for image_worker in list(getattr(app.state, "image_processing_workers", {}).values()):
            image_worker.shutdown()
        factory.shutdown()
        if worker_manager is not None:
            worker_manager.shutdown()
        if not custom_factory and getattr(app.state, "service_factory", None) is factory:
            # 默认工厂绑定本轮 engine；不能在下次 lifespan 中被误当作宿主注入对象复用。
            delattr(app.state, "service_factory")
        if shared_worker_executor is not None:
            # 共享任务线程仍可能持有数据库 session，连接池必须最后释放。
            shared_worker_executor.shutdown(wait=True, cancel_futures=True)
        engine.dispose()


_route_template = FastAPI(
    title="MemeMeow API",
    version="2.0.0",
    description="MemeMeow 图片检索、图片库和异步语境处理 API。模型密钥只在服务端环境中读取。",
    lifespan=lifespan,
    responses={400: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

# 路由定义继续使用模板应用注册；真正导出的 ``app`` 和宿主自定义应用由
# ``create_app`` 在模块末尾显式装配 resolver，避免模块全局保存可变当前 scope。
app = _route_template


def _request_scope(request: Request):
    """读取请求开始时解析并冻结的 scope，缺失时 fail-closed。"""
    scope = getattr(request.state, "scope", None)
    if scope is None:
        # 仅兼容不经过 ASGI 的旧领域单元测试；真实 HTTP 请求由 middleware
        # 预先写入 state，缺失时不会走这个分支。
        if not hasattr(request.app, "router") and hasattr(request.app.state, "settings"):
            return ScopeContext("local")
        raise ScopeResolutionError("请求尚未绑定 scope")
    return scope


def _request_services(request: Request) -> ScopeServices:
    """读取请求级 scope-bound 服务，不回退到另一个请求的 singleton。"""
    services = getattr(request.state, "services", None)
    scope = getattr(request.state, "scope", None)
    if not isinstance(scope, ScopeContext):
        raise ScopeResolutionError("请求尚未绑定 scope")
    if not isinstance(services, ScopeServices):
        raise ScopeResolutionError("请求尚未绑定 scope 服务")
    return validate_scope_services(scope, services)


def _environment(request: Request):
    """为当前请求创建绑定 scope 的短数据库事务。"""
    return request.app.state.database.environment(_request_scope(request))


def _service(request: Request, name: str):
    """返回当前请求的服务；local 测试夹具覆盖仍只在同 scope 下生效。"""
    # 领域函数的少量单元测试使用轻量 request stub，不经过 ASGI middleware；
    # 真实 HTTP 请求始终拥有 request.state.services 并走下面的 scope 校验。
    legacy_name = {"search": "search_engine", "visual_search": "visual_search", "reverse_image": "reverse_image", "metadata": "metadata", "tasks": "tasks"}.get(name, name)
    if not hasattr(request.app, "router") and (
        getattr(request, "state", None) is None or (
            getattr(request.state, "services", None) is None and hasattr(request.app.state, legacy_name)
        )
    ):
        return getattr(request.app.state, legacy_name)
    services = _request_services(request)
    value = getattr(services, name)
    # 仅为现有 local 测试夹具保留显式兼容入口；non-local 请求绝不读取 app.state
    # 上可能残留的 local service。
    if services.scope.scope_id == "local":
        override = getattr(request.app.state, legacy_name, None)
        if override is not None and getattr(override, "scope", services.scope).scope_id == "local":
            return override
    return value


def _processing_repository(request: Request) -> ImageProcessingRepository:
    """返回绑定当前请求 scope 的图片处理 job repository。"""
    return ImageProcessingRepository(request.app.state.database, _request_scope(request))


def _submit_processing_job_for_image(request: Request, record: Meme, image: Path, *, reverse_image_policy: object = None, auto_name: object = None, explicit_retry: bool = False, schedule: bool = True) -> ImageProcessingSnapshot:
    """把旧单阶段入口收敛到当前 scope 的统一图片处理 job。"""
    worker = _processing_worker(request)
    if worker is None:
        raise ImageProcessingError("image_processing_unavailable")
    options = _normalize_processing_options(request, reverse_image_policy=reverse_image_policy, auto_name=auto_name)
    embedding_record = _service(request, "metadata").embedding_record(image)
    return worker.submit(
        record.id,
        record.sha256,
        metadata_hash=embedding_record.get("metadata_hash") if isinstance(embedding_record, Mapping) else None,
        config=_processing_config(request),
        reverse_image_policy=options.reverse_image_policy,
        auto_name=options.auto_name,
        # 上传/普通单图提交只创建或复用当前 revision；终态失败只能通过显式
        # retry 接口或图片库批量入口创建新 revision，避免请求重试隐式重放外部执行。
        explicit_retry=explicit_retry,
        schedule=schedule,
    )


def _processing_worker(request: Request) -> ImageProcessingWorker | None:
    """按 scope 惰性获取图片处理 Worker；Worker 不共享可变 scope。"""
    workers = getattr(request.app.state, "image_processing_workers", None)
    if not isinstance(workers, dict):
        return None
    scope = _request_scope(request)
    lock = getattr(request.app.state, "image_processing_workers_lock", None)
    if lock is None:
        lock = request.app.state.image_processing_workers_lock = RLock()
    with lock:
        worker = workers.get(scope.scope_id)
        if worker is not None:
            return worker
        factory = getattr(request.app.state, "service_factory", None)
        if not callable(getattr(factory, "for_scope", None)):
            return None
        services = validate_scope_services(scope, factory.for_scope(scope))
        worker = ImageProcessingWorker(
            request.app.state.database,
            scope_id=scope,
            task_service=services.tasks,
            policy=getattr(request.app.state, "operation_policy_gateway", None),
            grant_store=getattr(request.app.state, "operation_grants", None),
            max_workers=validate_agent_concurrency(
                getattr(request.app.state.settings, "opencode_concurrency", 1),
                backpressure=getattr(request.app.state.settings, "agent_backpressure", None),
            ),
            task_handlers=getattr(request.app.state, "image_processing_task_handlers", None),
        )
        workers[scope.scope_id] = worker
        worker.start()
        return worker


def _processing_config(request: Request) -> dict[str, object]:
    """从服务端配置构造影响图片处理产物的稳定指纹输入。"""
    settings = request.app.state.settings
    identity = identity_from_settings(settings)
    try:
        skill_hash = request.app.state.opencode.skill_hash()
    except (OSError, OpenCodeError):
        skill_hash = None
    return {
        "agent_model": settings.opencode_model,
        "skill_hash": skill_hash,
        "settings_version": settings.settings_version,
        "visual_model": identity.model,
        "visual_dimensions": identity.dimensions,
        "preprocess_version": identity.preprocess_version,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": 1024,
    }


def _authenticate_callback_request(request: Request) -> None:
    """在 ASGI body 读取前验证 callback 服务凭据和注册路由声明。"""
    registry: CallbackRegistry | None = getattr(request.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
    registration = registry.get(request.url.path) if registry is not None else None
    verifier = getattr(request.app.state, "callback_verifier", None)
    if registration is None or verifier is None or not callable(getattr(verifier, "verify", None)):
        raise CallbackError("agent_callback_unavailable")
    token = request.headers.get("x-mememeow-callback") or request.headers.get("x-mememeow-callback-token")
    if not token:
        authorization = request.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not token:
        raise CallbackError()
    try:
        verify = verifier.verify
        try:
            parameters = inspect.signature(verify).parameters
        except (TypeError, ValueError) as exc:
            raise CallbackError("agent_callback_unavailable") from exc
        accepts_path = "path" in parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        # 只有签名明确支持 path 时才走兼容分支；verifier 内部的 TypeError
        # 必须被视为验证故障，不能被误当成旧接口重试。
        binding = verify(token, path=request.url.path) if accepts_path else verify(token)
    except CallbackError:
        raise
    except Exception as exc:  # noqa: BLE001 - verifier 故障必须 fail-closed
        raise CallbackError("agent_callback_unavailable") from exc
    if not isinstance(binding, CallbackBinding) or not binding.allows_any(registration.operations):
        raise CallbackError("agent_callback_invalid_execution")
    verify_content_length(request.headers, limit=registration.max_body_bytes)
    request.state.callback_header_request_id = validate_callback_headers(request.headers, binding)
    request.state.callback_binding = binding
    install_body_guard(request, limit=registration.max_body_bytes)


@app.middleware("http")
async def bind_request_scope(request: Request, call_next):
    """在任何数据库、文件或业务服务访问前解析一次可信 request scope。"""
    # Agent callback 没有用户 request scope；它们由既有服务间信任边界保护，并在路由
    # 内通过 task_id 从持久 Task.scope_id 恢复服务环境。公共路由仍必须走 resolver。
    if request.url.path in INTERNAL_SCOPE_CALLBACK_PATHS:
        # 轻量领域测试 stub 没有 ASGI headers；真实 Request 必须在解析 multipart
        # 之前通过 callback 服务身份验证。
        if hasattr(request, "headers"):
            try:
                _authenticate_callback_request(request)
            except CallbackError as exc:
                log_callback_rejection(request.url.path, exc, binding=getattr(request.state, "callback_binding", None))
                status = 413 if exc.code == "agent_callback_body_too_large" else 503 if exc.code == "agent_callback_unavailable" else 401
                return JSONResponse(status_code=status, content={"error": exc.code, "message": "内部执行凭据无效"})
        factory = getattr(request.app.state, "service_factory", None)
        if not callable(getattr(factory, "for_task", None)):
            return JSONResponse(status_code=503, content={"error": "scope_unavailable", "message": "请求 scope 当前不可用"})
        try:
            return await call_next(request)
        except CallbackError as exc:
            log_callback_rejection(request.url.path, exc, binding=getattr(request.state, "callback_binding", None))
            status = 413 if exc.code == "agent_callback_body_too_large" else 401
            return JSONResponse(status_code=status, content={"error": exc.code, "message": "内部执行凭据无效"})
    if _request_declares_scope(request):
        return JSONResponse(status_code=400, content={"error": "scope_selector_forbidden", "message": "请求不得提交范围选择字段"})
    if _extension_scope_exempt(request.app, request.url.path):
        try:
            for extension in _extension_list(request.app):
                await _invoke_extension_hook(extension, "authorize_exempt_request", request)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": "request_forbidden", "message": "请求未获授权"}
            return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
        except Exception as exc:  # noqa: BLE001 - 宿主可声明身份失败或可用性边界
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
            if status in {401, 403, 429, 503} and isinstance(code, str) and code:
                message = "需要有效会话" if status == 401 else "请求未获授权" if status in {403, 429} else "请求授权服务当前不可用"
                return JSONResponse(status_code=status, content={"error": code, "message": message})
            if not isinstance(exc, (DatabaseError, ValueError, RuntimeError)):
                raise
            return JSONResponse(status_code=503, content={"error": "request_authorization_unavailable", "message": "请求授权服务当前不可用"})
        return await call_next(request)
    try:
        scope = await resolve_scope_async(request)
        factory = getattr(request.app.state, "service_factory", None)
        if factory is None or not callable(getattr(factory, "for_scope", None)):
            raise ScopeResolutionError("应用未配置 scope service factory")
        services = validate_scope_services(scope, factory.for_scope(scope))
        for extension in _extension_list(request.app):
            await _invoke_extension_hook(extension, "authorize_request", request, scope, services)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) and "error" in exc.detail else {"error": "request_forbidden", "message": "请求未获授权"}
        return JSONResponse(status_code=exc.status_code, content=detail, headers=exc.headers)
    except ScopeResolutionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": "请求 scope 无法解析"})
    except Exception as exc:  # noqa: BLE001 - 适配器可声明稳定的身份/可用性边界
        status = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        if status in {401, 503} and isinstance(code, str) and code:
            message = "需要有效会话" if status == 401 else "请求 scope 当前不可用"
            return JSONResponse(status_code=status, content={"error": code, "message": message})
        if not isinstance(exc, (DatabaseError, ValueError, RuntimeError)):
            raise
        code = code if isinstance(code, str) and code else "scope_unavailable"
        return JSONResponse(status_code=503, content={"error": code, "message": "请求 scope 当前不可用"})
    request.state.scope = scope
    request.state.services = services
    return await call_next(request)

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """将 FastAPI 默认 422 统一转换为可识别的请求错误。"""
    status = 422 if request.url.path == "/collections" or request.url.path.startswith("/collections/") else 400
    code = "invalid_collection_request" if status == 422 else "invalid_request"
    return JSONResponse(status_code=status, content={"error": code, "message": "请求参数校验失败"})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """保证业务异常都使用 `{error, message}` 结构。"""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = exc.detail
    else:
        content = {"error": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


@app.get(OPERATION_POLICY_PATH, tags=["capabilities"])
async def operation_availability(request: Request, operation: str | None = Query(default=None)) -> dict[str, object]:
    """返回当前 scope 的 operation 可用性提示；该查询不建立 reservation。"""
    names = [operation] if operation else sorted(Operations.ALL)
    if any(name not in Operations.ALL for name in names):
        raise _error(400, "operation_unknown", "操作类型无效")
    gateway = _operation_gateway(request)
    values: list[dict[str, object]] = []
    for name in names:
        try:
            decision = gateway.probe(gateway.request(_request_scope(request), name, f"probe:{name}"))
        except OperationPolicyError as exc:
            item = {"operation": name, "available": False, "reason": exc.code}
            if exc.retry_at is not None:
                item["retry_at"] = exc.retry_at.isoformat() if isinstance(exc.retry_at, datetime) else str(exc.retry_at)
            values.append(item)
            continue
        item = {"operation": name, "available": bool(decision.allowed)}
        if not decision.allowed:
            item["reason"] = decision.reason if decision.reason in {"operation_forbidden", "operation_limit_exceeded", "operation_policy_unavailable"} else "operation_policy_unavailable"
        if decision.retry_at is not None:
            item["retry_at"] = decision.retry_at.isoformat() if isinstance(decision.retry_at, datetime) else str(decision.retry_at)
        values.append(item)
    return {"items": values}


@app.middleware("http")
async def access_policy(request: Request, call_next):
    """按环境配置执行白名单保护模式。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings and settings.protected_mode:
        path = request.url.path.rstrip("/") or "/"
        allowed = {p.rstrip("/") or "/" for p in settings.allowed_endpoints}
        if not any(path == p or (p and p != "/" and path.startswith(p + "/")) for p in allowed):
            return JSONResponse(status_code=403, content={"error": "protected", "message": "接口未在保护模式白名单中"})
    return await call_next(request)


@app.middleware("http")
async def access_rate_limit(request: Request, call_next):
    """按客户端 IP 执行简单内存限流。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings and settings.rate_limit_enabled:
        limiter = getattr(request.app.state, "limiter", None)
        if limiter is None:
            limiter = request.app.state.limiter = RateLimiter()
        client = request.client.host if request.client else "unknown"
        if not limiter.check(client, settings.rate_limit_requests, settings.rate_limit_window):
            return JSONResponse(status_code=429, content={"error": "rate_limited", "message": "请求过于频繁"}, headers={"Retry-After": str(settings.rate_limit_window)})
    return await call_next(request)


@app.get("/", tags=["system"])
async def root(request: Request):
    """返回服务基本状态。"""
    if (FRONTEND_DIST / "index.html").is_file() and "text/html" in request.headers.get("accept", ""):
        return FileResponse(FRONTEND_DIST / "index.html", media_type="text/html")
    return {"name": "MemeMeow", "version": app.version, "status": "ok"}


@app.get("/health", tags=["system"])
async def health(request: Request) -> dict[str, object]:
    """返回可用于容器探活的状态。"""
    settings = getattr(request.app.state, "settings", None)
    visual_client = getattr(request.app.state, "visual_inference", None)
    visual_status = visual_client.health() if visual_client is not None else {"available": False}
    return {
        "status": "ok" if getattr(request.app.state, "service_factory", None) is not None else "degraded",
        "visual_available": bool(visual_status.get("available")),
        "agent_resume_enabled": bool(getattr(settings, "agent_resume_enabled", False)),
        "storage_preflight": _storage_preflight_summary(getattr(request.app.state, "storage_preflight", None)),
    }


@app.get("/config", tags=["system"])
async def config_status(request: Request) -> dict[str, object]:
    """兼容旧 `/config` handler，并注入当前入口的 scope/service 解析。"""
    return await _config_status(request, request_scope=_request_scope, service=_service)


@app.post("/internal/reverse-image/search", tags=["internal"])
async def internal_reverse_image_search(
    request: Request,
    task_id: str = Form(..., min_length=1, max_length=255),
    image: UploadFile = File(...),
    request_id: str | None = Form(default=None, max_length=128),
    input_digest: str | None = Form(default=None, max_length=64),
    search_type: str = Form(default="all"),
    language: str = Form(default="zh-cn"),
    country: str | None = Form(default=None),
    query: str | None = Form(default=None),
    auto_crop: bool = Form(default=False),
    refresh: bool = Form(default=False),
) -> dict[str, object]:
    """验证当前 Agent claim 后执行供应商无关的内部反向图片检索。"""
    content = await image.read()
    return await _internal_reverse_image_search_http(
        request,
        task_id=task_id,
        content=content,
        filename=image.filename,
        request_id=request_id,
        input_digest=input_digest,
        search_type=search_type,
        language=language,
        country=country,
        query=query,
        auto_crop=auto_crop,
        refresh=refresh,
        binding=lambda received: getattr(received.state, "callback_binding", None),
        registration=lambda received: (
            getattr(received.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY).get(received.url.path)
            if getattr(received.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
            else None
        ),
        database=lambda received: received.app.state.database,
        scope_services=lambda received, scope: validate_scope_services(scope, received.app.state.service_factory.for_scope(scope)),
        error=_error,
    )


@app.post("/internal/visual-search/match", tags=["internal"])
async def internal_visual_search_match(request: Request, payload: VisualMatchRequest) -> dict[str, object]:
    """兼容内部视觉匹配 callback，并注入 binding、scope database 与 service。"""
    return await _internal_visual_search_match_http(
        request,
        payload,
        binding=lambda received: getattr(received.state, "callback_binding", None),
        registration=lambda received: (
            getattr(received.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY).get(received.url.path)
            if getattr(received.app.state, "callback_registry", DEFAULT_CALLBACK_REGISTRY)
            else None
        ),
        database=lambda received: received.app.state.database,
        scope_services=lambda received, scope: validate_scope_services(scope, received.app.state.service_factory.for_scope(scope)),
        error=_error,
    )


# Settings HTTP 路由独立注册到模板应用；直接展开 APIRouter 路由以保持既有路由表顺序，
# 并让模块级 app 和宿主 create_app() 都复用同一组 APIRoute 对象。
_route_template.router.routes.extend(settings_router.routes)


@app.post("/search", tags=["search"])
async def search_images(request: Request, payload: SearchRequest) -> dict[str, list[str]]:
    """兼容旧检索入口，并注入当前 scope 的 service、媒体和错误投影。"""
    return await _search_images(
        request,
        payload,
        service=_service,
        media_for_meme=_media_for_meme,
        error=_error,
    )


@app.post("/generate-cache", status_code=202, tags=["tasks"])
async def generate_cache(request: Request) -> dict[str, object]:
    """兼容旧缓存任务入口，并注入当前 scope 的 service/error 投影。"""
    return await _generate_cache(request, service=_service, error=_error)


def _cancel_agent_task(request: Request, task_id: str) -> None:
    """兼容旧任务取消入口，调用当前应用的 Agent 取消适配器。"""
    cancel = getattr(request.app.state.opencode, "cancel", None)
    if callable(cancel):
        cancel(task_id)


def _activity_payload(value: object) -> dict[str, object] | None:
    """兼容旧入口，委托公共任务 HTTP 模块投影活跃度。"""
    return _task_activity_payload(value)


def _read_agent_activity(request: Request, records: list[TaskRecord]) -> dict[str, object]:
    """兼容旧入口，委托公共任务 HTTP 模块读取活跃度。"""
    return _read_agent_activity_http(request, records)


def _task_summary(request: Request, record: TaskRecord, activities: Mapping[str, object] | None = None) -> dict[str, object]:
    """兼容旧入口，委托公共任务 HTTP 模块生成安全摘要。"""
    return _task_summary_http(
        request,
        record,
        activities,
        service=_service,
        processing_repository=_processing_repository,
    )


@app.get("/tasks", tags=["tasks"])
async def list_tasks(
    request: Request,
    status: list[str] = Query(default=[]),
    task_type: list[str] = Query(default=[]),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    """兼容旧任务列表入口，并注入当前 scope 的 service/repository。"""
    return await _list_tasks_http(
        request,
        status=status,
        task_type=task_type,
        cursor=cursor,
        limit=limit,
        service=_service,
        processing_repository=_processing_repository,
    )


@app.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(request: Request, task_id: str) -> dict[str, object]:
    """兼容旧任务详情入口，并注入当前 scope 的 service/repository/error。"""
    return await _get_task_http(
        request,
        task_id,
        service=_service,
        error=_error,
        processing_repository=_processing_repository,
    )


@app.post("/tasks/{task_id}/cancel", tags=["tasks"])
async def cancel_task(request: Request, task_id: str) -> dict[str, object]:
    """兼容旧任务取消入口，并注入当前 scope 的 service/repository/error。"""
    return await _cancel_task_http(
        request,
        task_id,
        service=_service,
        error=_error,
        processing_repository=_processing_repository,
        cancel_agent=_cancel_agent_task,
    )


@app.post("/tasks/{task_id}/retry", status_code=202, tags=["tasks"])
async def retry_task(request: Request, task_id: str) -> dict[str, object]:
    """兼容旧任务重试入口，并注入当前 scope 的 service/repository/error。"""
    return await _retry_task_http(
        request,
        task_id,
        service=_service,
        error=_error,
        processing_repository=_processing_repository,
    )


@app.post("/images/processing/unready", status_code=202, tags=["images", "tasks"])
async def process_unready_image_library_route(request: Request, payload: ProcessingBatchRequest) -> dict[str, object]:
    """将静态未就绪路由放在动态 job 路由之前，避免被 job_id 捕获。"""
    return await process_unready_image_library(request, payload)


@app.get("/image-processing", tags=["images", "tasks"], include_in_schema=False)
@app.get("/images/processing", tags=["images", "tasks"])
async def list_image_processing_jobs(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, object]:
    """兼容图片处理 Job 列表入口，并注入当前 scope repository。"""
    return await _list_image_processing_jobs_http(
        request,
        limit=limit,
        processing_repository=_processing_repository,
    )


@app.get("/image-processing/{job_id}", tags=["images", "tasks"], include_in_schema=False)
@app.get("/images/processing/{job_id}", tags=["images", "tasks"])
async def get_image_processing_job(request: Request, job_id: str) -> dict[str, object]:
    """兼容图片处理 Job 详情入口，并注入当前 scope repository/error。"""
    return await _get_image_processing_job_http(
        request,
        job_id,
        error=_error,
        processing_repository=_processing_repository,
    )


def _core_image_ready(request: Request, record: Meme, image: Path, policy: str) -> bool:
    """按当前图片、Agent 策略和文本模型判断三个核心产物是否有效。"""
    del image  # 物理身份由共享判定按当前 storage_key 重新解析并复核。
    latest = _processing_repository(request).latest_for_target(record.id, record.sha256)
    if latest is not None:
        if latest.reverse_image_policy != policy:
            return False
        # 产物可能仍然存在，但最新 Job/核心阶段已明确失败、阻止或执行状态
        # 未知；完整重试必须把这种目标重新纳入，而不是只看三张产物表。
        if latest.status in {"failed", "blocked", "unknown_execution"}:
            return False
        if any(
            stage.get("stage") in {"visual", "agent", "text_embedding"}
            and stage.get("status") in {"failed", "blocked", "unknown_execution"}
            for stage in latest.stages
        ):
            return False
        # 自动重命名 warning 是可选阶段的非阻塞结果，但目标/执行身份失效
        # 仍会停止 Job；即使异常历史行的顶层状态没有同步，也不能把它当作
        # 核心产物已经可以安全复用。
        auto_rename_stage = next((stage for stage in latest.stages if stage.get("stage") == "auto_rename"), None)
        if auto_rename_stage and auto_rename_stage.get("status") in {"failed", "blocked", "unknown_execution"}:
            return False
        if auto_rename_stage and auto_rename_stage.get("status") == "warning":
            warning_code = (auto_rename_stage.get("error") or {}).get("error") if isinstance(auto_rename_stage.get("error"), Mapping) else None
            if warning_code not in AUTO_RENAME_WARNING_ERRORS:
                return False
        if any(
            next((stage.get("status") for stage in latest.stages if stage.get("stage") == required), None) != "succeeded"
            for required in ("visual", "agent", "text_embedding")
        ):
            return False
    config = _processing_config(request)
    if not image_file_matches(request.app.state.database, _request_scope(request), record):
        return False
    metadata_hash = ImageProcessingWorker._metadata_hash(record)
    if metadata_hash is None or record.context_status != "ready":
        return False
    summary = (record.provenance or {}).get("agent_context")
    expected_config_hash = processing_config_hash(config)
    if (
        not isinstance(summary, Mapping)
        or summary.get("image_sha256") != record.sha256
        or summary.get("model") != config.get("agent_model")
        or summary.get("reverse_image_policy") != policy
        or summary.get("processing_config_hash") != expected_config_hash
        or ("skill_hash" in config and summary.get("skill_hash") != config.get("skill_hash"))
        or not summary.get("task_id")
        or not summary.get("completed_at")
    ):
        return False
    visual_identity = identity_from_settings(request.app.state.settings)
    try:
        with _environment(request) as environment:
            visual = environment.visual.get(record.id, model=visual_identity.model, preprocess_version=visual_identity.preprocess_version, dimensions=visual_identity.dimensions, image_sha256=record.sha256)
            if visual is None or visual.embedding is None:
                return False
            text_row = environment.uow.session.scalar(
                select(MemeTextEmbedding).where(
                    MemeTextEmbedding.scope_id == _request_scope(request).scope_id,
                    MemeTextEmbedding.meme_id == record.id,
                    MemeTextEmbedding.image_sha256 == record.sha256,
                    MemeTextEmbedding.metadata_hash == metadata_hash,
                    MemeTextEmbedding.embedding_model_version == config.get("embedding_model"),
                    MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                    MemeTextEmbedding.status == "ready",
                    MemeTextEmbedding.embedding.is_not(None),
                )
            )
            return text_row is not None
    except Exception:  # noqa: BLE001 - 就绪判断是安全边界，任一异常都必须 fail-closed
        return False


async def process_unready_image_library(request: Request, payload: ProcessingBatchRequest) -> dict[str, object]:
    """在当前 scope 内以游标枚举全部核心未就绪图片并逐图提交 Job。"""
    unknown = set(request.query_params)
    if unknown:
        raise _error(400, "invalid_request", "未就绪处理不接受分页、筛选或 scope 参数")
    try:
        options = _normalize_processing_options(request, reverse_image_policy=payload.reverse_image_policy, auto_name=payload.auto_name)
    except ImageProcessingError as exc:
        raise _error(503 if exc.code == "reverse_image_unavailable" else 400, exc.code, "图片处理选项无效或服务不可用") from exc
    worker = _processing_worker(request)
    if worker is None:
        raise _error(503, "image_processing_unavailable", "图片处理服务当前不可用")
    repository = _processing_repository(request)
    results: list[dict[str, object]] = []
    last_id: UUID | None = None
    while True:
        with _environment(request) as environment:
            statement = select(Meme).where(Meme.scope_id == _request_scope(request).scope_id)
            if last_id is not None:
                # storage_key 会被自动重命名阶段异步改变，不能作为分页游标；
                # Meme ID 不可变，才能保证一次请求不会重复或漏扫目标。
                statement = statement.where(Meme.id > last_id)
            rows = list(environment.uow.session.scalars(statement.order_by(Meme.id.asc()).limit(100)))
        if not rows:
            break
        for meme in rows:
            last_id = meme.id
            try:
                image = _service(request, "metadata").blob_store.resolve(meme.storage_key)
                if _core_image_ready(request, meme, image, options.reverse_image_policy):
                    continue
                latest = repository.latest_for_target(meme.id, meme.sha256)
                snapshot = worker.submit(
                    meme.id,
                    meme.sha256,
                    metadata_hash=ImageProcessingWorker._metadata_hash(meme),
                    config=_processing_config(request),
                    reverse_image_policy=options.reverse_image_policy,
                    auto_name=options.auto_name,
                    explicit_retry=latest is not None,
                    schedule=True,
                )
                reused = latest is not None and latest.job_id == snapshot.job_id
                results.append(
                    {
                        "meme_id": str(meme.id),
                        "processing_job_id": snapshot.job_id,
                        "status": snapshot.status,
                        "reused": reused,
                        "category": "reused" if reused else "submitted",
                    }
                )
            except ImageProcessingError as exc:
                category = "conflict" if exc.code in {"generation_policy_conflict", "processing_options_conflict"} else "failed"
                results.append({"meme_id": str(meme.id), "error": exc.code, "category": category})
            except Exception:  # noqa: BLE001 - 单图提交必须隔离异常并继续枚举其它图片
                results.append({"meme_id": str(meme.id), "error": "image_processing_failed", "category": "failed"})
    submitted = sum(1 for item in results if item.get("processing_job_id") and not item.get("reused"))
    reused = sum(1 for item in results if item.get("reused"))
    conflicts = sum(1 for item in results if item.get("category") == "conflict")
    failed = sum(1 for item in results if item.get("category") == "failed")
    return {"target_count": len(results), "submitted_count": submitted, "reused_count": reused, "conflict_count": conflicts, "failed_count": failed, "results": results}


@app.post("/image-processing/{job_id}/retry", status_code=202, tags=["images", "tasks"], include_in_schema=False)
@app.post("/images/processing/{job_id}/retry", status_code=202, tags=["images", "tasks"])
async def retry_image_processing_job(request: Request, job_id: str, payload: ProcessingRetryRequest | None = None) -> dict[str, object]:
    """兼容图片处理 Job 重试入口，并注入当前 scope 的控制依赖。"""
    return await _retry_image_processing_job_http(
        request,
        job_id,
        payload,
        error=_error,
        processing_repository=_processing_repository,
        processing_worker=_processing_worker,
        normalize_processing_options=_normalize_processing_options,
        processing_config=_processing_config,
    )


@app.post("/image-processing/stages", status_code=202, tags=["images", "tasks"], include_in_schema=False)
@app.post("/images/processing/stages", status_code=202, tags=["images", "tasks"], include_in_schema=False)
@app.post("/images/stages", status_code=202, tags=["images", "tasks"])
async def submit_image_stage(request: Request, payload: ImageStageSubmissionRequest) -> dict[str, object]:
    """兼容独立图片阶段入口，并注入当前 scope 的控制依赖。"""
    return await _submit_image_stage_http(
        request,
        payload,
        service=_service,
        error=_error,
        processing_worker=_processing_worker,
        normalize_processing_options=_normalize_processing_options,
        processing_config=_processing_config,
        task_summary=_task_summary,
        operation_error=_operation_http_error,
    )


@app.post("/images/stages/batch", status_code=202, tags=["images", "tasks"])
async def submit_image_stage_batch(request: Request, payload: ImageStageBatchRequest) -> dict[str, object]:
    """兼容批量独立图片阶段入口，并注入当前 scope 的控制依赖。"""
    return await _submit_image_stage_batch_http(
        request,
        payload,
        service=_service,
        error=_error,
        processing_worker=_processing_worker,
        normalize_processing_options=_normalize_processing_options,
        processing_config=_processing_config,
        task_summary=_task_summary,
    )


@app.get("/images", tags=["images"])
async def list_images(
    request: Request,
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """按文件名筛选并分页列出当前 scope 的扁平图片。"""
    unknown = set(request.query_params) - {"search", "page", "page_size"}
    if unknown:
        raise _error(400, "invalid_request", "图片列表不接受已废弃的目录参数")
    services = _request_services(request)
    with _environment(request) as environment:
        records = environment.memes.list(search=search, page=page, page_size=page_size)
        total = environment.memes.count(search=search)
    items = []
    visual_identity = identity_from_settings(request.app.state.settings)
    for record in records:
        try:
            image = services.metadata.blob_store.resolve(record.storage_key)
            identity = services.metadata._identity(image)
        except (DatabaseError, MetadataError):
            continue
        metadata_status = services.metadata.status(image)
        with _environment(request) as environment:
            visual_row = environment.visual.get(record.id, model=visual_identity.model, preprocess_version=visual_identity.preprocess_version, dimensions=visual_identity.dimensions, image_sha256=record.sha256)
        item = {"meme_id": str(record.id), "filename": record.storage_key, "extension": record.extension, "size": identity["size_bytes"], "media_url": f"/media/{record.id}", "metadata": metadata_status, "embedding_status": "ready" if services.search.has_cache() and metadata_status.get("status") in {"partial", "ready"} else "blocked" if metadata_status.get("status") == "repair_required" else "pending", "visual_embedding_status": "ready" if visual_row is not None else "pending"}
        latest_processing = _processing_repository(request).latest_for_target(record.id, record.sha256)
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


@app.post("/images/processing", status_code=202, tags=["images", "tasks"])
async def process_image_library(
    request: Request,
    payload: ProcessingBatchRequest,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=200),
) -> dict[str, object]:
    """分页枚举当前 scope 图片并显式重试可恢复的逐图处理 job。"""
    worker = _processing_worker(request)
    if worker is None:
        raise _error(503, "image_processing_unavailable", "图片处理服务当前不可用")
    try:
        options = _normalize_processing_options(request, reverse_image_policy=payload.reverse_image_policy, auto_name=payload.auto_name)
    except ImageProcessingError as exc:
        raise _error(503 if exc.code == "reverse_image_unavailable" else 400, exc.code, "图片处理选项无效或服务不可用") from exc
    repository = _processing_repository(request)
    results: list[dict[str, object]] = []
    with _environment(request) as environment:
        memes = environment.memes.list(page=page, page_size=page_size)
        total = environment.memes.count()
    for meme in memes:
        try:
            latest = repository.latest_for_target(meme.id, meme.sha256)
            image = _service(request, "metadata").blob_store.resolve(meme.storage_key)
            embedding_record = _service(request, "metadata").embedding_record(image)
            snapshot = worker.submit(
                meme.id,
                meme.sha256,
                metadata_hash=embedding_record.get("metadata_hash") if isinstance(embedding_record, Mapping) else None,
                config=_processing_config(request),
                reverse_image_policy=options.reverse_image_policy,
                auto_name=options.auto_name,
                explicit_retry=latest is not None and latest.status in {"failed", "blocked", "unknown_execution"},
            )
            results.append({"meme_id": str(meme.id), "job_id": snapshot.job_id, "processing_job_id": snapshot.job_id, "submission_mode": "pipeline", "status": snapshot.status, "reused": latest is not None and snapshot.job_id == latest.job_id})
        except ImageProcessingError as exc:
            results.append({"meme_id": str(meme.id), "error": exc.code})
        except (DatabaseError, MetadataError, RuntimeError):
            results.append({"meme_id": str(meme.id), "error": "image_processing_failed"})
    return {"results": results, "count": len(results), "total": total, "page": page, "page_size": page_size}


@app.get("/images/metadata", tags=["images"])
async def image_metadata(
    request: Request,
    meme_id: str | None = Query(default=None),
) -> dict[str, object]:
    """按稳定 ``meme_id`` 返回当前 scope 的数据库语境记录。"""
    if not meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        _record, image = _service(request, "metadata").image_for_meme(meme_id)
        metadata = _service(request, "metadata").load(image)
    except MetadataError as exc:
        status = 404 if exc.code == "metadata_missing" else 409
        code = "meme_not_found" if exc.code == "metadata_missing" else exc.code
        message = "图片不存在" if exc.code == "metadata_missing" else "图片元数据无法读取"
        raise _error(status, code, message) from exc
    payload = metadata.model_dump(mode="json", exclude_none=False)
    payload["meme_id"] = meme_id
    return payload


@app.get("/media/{meme_id}", tags=["images"])
async def media(request: Request, meme_id: str):
    """按当前 scope 的稳定 meme_id 读取经过指纹校验的图片。"""
    try:
        _record, path = _service(request, "metadata").image_for_meme(meme_id)
    except MetadataError as exc:
        raise _error(404, "meme_not_found", "图片不存在") from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/collections", tags=["collections"])
async def list_collections(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, object]:
    """分页列出当前请求 scope 的合集。"""
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise _error(400, "invalid_request", "合集列表不接受 scope 或 user 参数")
    with _environment(request) as environment:
        rows = environment.collections.list(page=page, page_size=page_size)
        return {"items": [_collection_payload(request, environment, row) for row in rows], "total": environment.collections.count(), "page": page, "page_size": page_size}


@app.post("/collections", status_code=201, tags=["collections"])
async def create_collection(request: Request, payload: CollectionRequest) -> dict[str, object]:
    """创建当前请求 scope 的空合集。"""
    try:
        with _environment(request) as environment:
            row = environment.collections.create(payload.name)
            return _collection_payload(request, environment, row)
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/collections/import", tags=["collections"])
async def import_collection(request: Request) -> dict[str, object]:
    """预检单个合集 ZIP 后创建新合集，并逐图片报告导入结果。"""
    try:
        form = await _parse_upload_form(request, max_files=2, max_request_bytes=MAX_ARCHIVE_COMPRESSED_BYTES)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "request_too_large":
            raise _collection_package_error(CollectionPackageError("archive_too_large")) from exc
        if isinstance(exc.detail, dict) and exc.detail.get("error") == "too_many_files":
            raise _error(400, "file_required", "必须上传一个合集 ZIP 文件") from exc
        raise
    if set(form.keys()) - {"file"}:
        raise _error(400, "invalid_request", "合集导入只接受一个 file ZIP 字段")
    values = form.getlist("file")
    uploads = [item for item in values if hasattr(item, "filename") and hasattr(item, "read")]
    if len(values) != 1 or len(uploads) != 1:
        raise _error(400, "file_required", "必须上传一个合集 ZIP 文件")
    upload = uploads[0]
    if not str(upload.filename or "").lower().endswith(".zip"):
        raise _error(400, "unsupported_package", "合集导入只接受 ZIP 文件")
    try:
        content, too_large = await _read_upload_content(upload, max_upload_size=MAX_ARCHIVE_COMPRESSED_BYTES)
        if too_large:
            raise CollectionPackageError("archive_too_large")
        package = preflight_archive(content, max_file_size=min(int(request.app.state.settings.max_upload_size), DEFAULT_MAX_FILE_SIZE), max_total_size=MAX_TOTAL_UNCOMPRESSED_BYTES)
    except CollectionPackageError as exc:
        raise _collection_package_error(exc) from exc
    finally:
        await upload.close()
    collection_name = package.manifest.collection.name
    try:
        with _environment(request) as environment:
            if environment.collections.by_name(collection_name) is not None:
                raise CollectionPackageError("collection_exists")
    except CollectionPackageError as exc:
        raise _collection_package_error(exc) from exc
    try:
        with _environment(request) as environment:
            collection = environment.collections.create(collection_name)
            collection_id = collection.id
            existing_by_name: dict[str, object] = {}
            for record in environment.memes.list_all():
                valid = _service(request, "metadata").blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes)
                existing_by_name[record.storage_key] = {"meme": record, "sha256": record.sha256 if valid else "__changed__"}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc

    results: list[dict[str, object]] = []
    meme_id_map: dict[str, str] = {}
    created_count = 0
    for package_member in package.members:
        member = package_member.manifest
        result: dict[str, object] = {"source_meme_id": member.source_meme_id, "filename": member.filename_at_export, "ok": False}
        try:
            target = resolve_import_filename(member.filename_at_export, member.sha256, existing_by_name)
            if target.existing_meme is not None:
                target_id = str(getattr(target.existing_meme, "id", target.existing_meme))
                with _environment(request) as environment:
                    environment.collections.add_members(collection_id, [target_id])
                result.update({"ok": True, "status": "reused", "target_meme_id": target_id, "saved_filename": target.filename})
            else:
                try:
                    import_grant = _acquire_operation(
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
                    target_id, target_path = _service(request, "metadata").upload_bytes(package_member.content, target_key=target.filename)
                except (MetadataError, OSError) as exc:
                    # 只有明确知道 durable 写入尚未开始时才能归还上传 reservation。
                    if isinstance(exc, MetadataError) and exc.code in {"target_exists", "invalid_filename", "invalid_image", "staging_conflict", "staging_write_failed"}:
                        try:
                            _release_operation(request, import_grant)
                        except OperationPolicyError:
                            pass
                    raise
                try:
                    _commit_operation(request, import_grant)
                except OperationPolicyError:
                    # 文件和 Meme 已经提交，不能把未知计量状态误报为导入失败。
                    pass
                target_id = str(target_id)
                created_count += 1
                with _environment(request) as environment:
                    environment.collections.add_members(collection_id, [target_id])
                existing_by_name[target.filename] = {"meme": target_id, "sha256": member.sha256}
                result.update({"ok": True, "status": "imported", "target_meme_id": target_id, "saved_filename": target_path.name})
                processing_worker = None
                try:
                    processing_worker = _processing_worker(request)
                    if processing_worker is None:
                        task = _submit_visual_task(request, target_path, expected_sha256=member.sha256, schedule=True)
                        result.update({"visual_task_id": task.task_id, "visual_job_id": task.task_id, "metadata_job_id": task.task_id, "metadata_job_status": task.status, "visual_task_status": task.status})
                except (OSError, RuntimeError, MetadataError) as exc:
                    # Meme、文件和合集关系已有效提交，任务失败只能作为可重试的逐项告警。
                    error = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
                    result["visual_task_error"] = error
                    result["metadata_job_error"] = error
                try:
                    worker = processing_worker
                    if worker is not None:
                        processing = worker.submit(
                            target_id,
                            member.sha256,
                            config=_processing_config(request),
                            reverse_image_policy="forbid",
                            schedule=False,
                        )
                        worker.schedule(processing.job_id)
                        # 旧合集客户端把视觉任务和语境任务都作为一个可轮询
                        # 标识读取；统一控制面现在返回父 Job，因此这些字段
                        # 兼容地指向同一个 Job，不再伪造额外叶子任务。
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
            meme_id_map[member.source_meme_id] = str(result["target_meme_id"])
        except CollectionPackageError as exc:
            result["error"] = exc.code
        except (DatabaseError, MetadataError, OSError, RuntimeError) as exc:
            result["error"] = getattr(exc, "code", "import_failed")
        results.append(result)
    if created_count:
        _invalidate_search(request)
    failed_count = sum(1 for item in results if not item.get("ok"))
    warning_count = sum(1 for item in results if item.get("metadata_job_error"))
    status = "succeeded" if not failed_count and not warning_count else "partial"
    return {"collection_id": str(collection_id), "name": collection_name, "status": status, "partial": status == "partial", "results": results, "meme_id_map": meme_id_map}


@app.get("/collections/{collection_id}", tags=["collections"])
async def get_collection(request: Request, collection_id: str, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, object]:
    """返回合集元数据和当前文件信息的分页成员。"""
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise _error(400, "invalid_request", "合集详情不接受 scope 或 user 参数")
    try:
        with _environment(request) as environment:
            row = environment.collections.get(collection_id)
            if row is None:
                raise DatabaseError("collection_not_found")
            members = []
            for item, meme in environment.collections.members(row.id, page=page, page_size=page_size):
                metadata_service = _service(request, "metadata")
                metadata_status = metadata_service.status(metadata_service.blob_store.resolve(meme.storage_key))
                members.append({"meme_id": str(meme.id), "filename": meme.storage_key, "extension": meme.extension, "size": meme.size_bytes, "media_url": f"/media/{meme.id}", "metadata": metadata_status})
            payload = _collection_payload(request, environment, row)
            payload["members"] = members
            payload["total"] = environment.collections.member_count(row.id)
            payload["page"] = page
            payload["page_size"] = page_size
            return payload
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.get("/collections/{collection_id}/export", tags=["collections"])
async def export_collection(request: Request, collection_id: str):
    """动态读取当前合集并生成完整 ZIP，响应结束后清理临时归档。"""
    try:
        with _environment(request) as environment:
            row = environment.collections.get(collection_id)
            if row is None:
                raise DatabaseError("collection_not_found")
            members = environment.collections.members_for_export(row.id)
            collection_name = row.name
    except DatabaseError as exc:
        raise _collection_error(exc) from exc
    temp_dir = Path(tempfile.mkdtemp(prefix=".collection-export-", dir=request.app.state.settings.data_root))
    archive_path: Path | None = None
    try:
        archive_path = build_export_archive(collection_name, members, _service(request, "metadata").blob_store, temp_root=temp_dir, max_file_size=min(int(request.app.state.settings.max_upload_size), DEFAULT_MAX_FILE_SIZE), max_total_size=MAX_TOTAL_UNCOMPRESSED_BYTES, max_archive_size=MAX_ARCHIVE_COMPRESSED_BYTES)
    except CollectionPackageError as exc:
        cleanup_archive(temp_dir)
        raise _collection_package_error(exc) from exc
    except (OSError, DatabaseError) as exc:
        cleanup_archive(temp_dir)
        raise _error(409, "member_unreadable", "合集成员图片无法读取") from exc
    download_name = safe_download_filename(collection_name)
    content_disposition = f"attachment; filename=\"collection.zip\"; filename*=UTF-8''{quote(download_name, safe='')}"
    return FileResponse(archive_path, media_type="application/zip", headers={"Content-Disposition": content_disposition}, background=BackgroundTask(cleanup_archive, archive_path))


@app.patch("/collections/{collection_id}", tags=["collections"])
async def rename_collection(request: Request, collection_id: str, payload: CollectionRequest) -> dict[str, object]:
    """重命名合集，不改变成员关系。"""
    try:
        with _environment(request) as environment:
            row = environment.collections.rename(collection_id, payload.name)
            return _collection_payload(request, environment, row)
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.delete("/collections/{collection_id}", tags=["collections"])
async def delete_collection(request: Request, collection_id: str) -> dict[str, object]:
    """删除合集及关系，不删除图片文件。"""
    try:
        with _environment(request) as environment:
            environment.collections.delete(collection_id)
            return {"collection_id": collection_id, "deleted": True}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/collections/{collection_id}/items", tags=["collections"])
async def add_collection_items(request: Request, collection_id: str, payload: CollectionItemsRequest) -> dict[str, object]:
    """原子批量加入图片并返回幂等计数。"""
    try:
        with _environment(request) as environment:
            added, existing, total = environment.collections.add_members(collection_id, payload.meme_ids)
            return {"collection_id": collection_id, "added_count": added, "existing_count": existing, "member_count": total}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.delete("/collections/{collection_id}/items/{meme_id}", tags=["collections"])
async def remove_collection_item(request: Request, collection_id: str, meme_id: str) -> dict[str, object]:
    """幂等移除合集成员。"""
    try:
        with _environment(request) as environment:
            total = environment.collections.remove_member(collection_id, meme_id)
            return {"collection_id": collection_id, "meme_id": meme_id, "removed": True, "member_count": total}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/images/rename", tags=["images"])
async def rename_image(request: Request, payload: RenameRequest) -> dict[str, str]:
    """按稳定 meme_id 重命名图片并同步数据库 storage_key。"""
    metadata_service = _service(request, "metadata")
    if not payload.meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        _record, source = metadata_service.image_for_meme(payload.meme_id)
    except MetadataError as exc:
        raise _error(404, "meme_not_found", "图片不存在") from exc
    clean = _safe_filename(payload.new_name)
    if Path(clean).suffix.lower() != source.suffix.lower():
        clean = f"{Path(clean).stem}{source.suffix.lower()}"
    if any(char in payload.new_name for char in "/\\") or any(ord(char) < 32 for char in payload.new_name):
        raise _error(400, "invalid_filename", "文件名非法")
    try:
        validate_business_storage_key(clean)
    except ValueError as exc:
        raise _error(400, "invalid_filename", "文件名非法") from exc
    target = metadata_service.blob_store.resolve(clean, must_exist=False)
    if target.exists() and target != source:
        raise _error(409, "file_exists", "目标文件已存在")
    try:
        metadata = metadata_service.rename_by_id(payload.meme_id, target)
    except MetadataError as exc:
        if exc.code == "target_exists":
            raise _error(409, "file_exists", "目标文件已存在")
        raise _error(500, "metadata_rename_failed", "图片元数据同步失败")
    _invalidate_search(request)
    return {"meme_id": payload.meme_id, "filename": Path(metadata.image.relative_path).name, "media_url": f"/media/{payload.meme_id}"}


@app.post("/images/delete", tags=["images"])
async def delete_image(request: Request, payload: DeleteRequest) -> dict[str, object]:
    """按稳定 meme_id 隔离文件后删除数据库记录。"""
    if not payload.meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        record, image = _service(request, "metadata").image_for_meme(payload.meme_id)
    except MetadataError as exc:
        raise _error(404, "meme_not_found", "图片不存在") from exc
    try:
        delete_grant = _acquire_operation(request, Operations.IMAGE_DELETE, f"delete:{payload.meme_id}:{getattr(record, 'revision', 0)}", resource_id=payload.meme_id, source="api")
    except OperationPolicyError as exc:
        raise _operation_http_error(exc) from exc
    try:
        _service(request, "metadata").remove_by_id(payload.meme_id)
    except MetadataError as exc:
        if exc.code in {"meme_not_found", "file_not_found", "target_exists", "invalid_storage_key"}:
            try:
                _release_operation(request, delete_grant)
            except OperationPolicyError:
                pass
        raise _error(500, exc.code, "图片及其元数据删除失败") from exc
    try:
        _commit_operation(request, delete_grant)
    except OperationPolicyError:
        # 删除已经完成，未知 policy 状态交由宿主恢复，不反向报告为未删除。
        pass
    _invalidate_search(request)
    return {"meme_id": payload.meme_id, "deleted": True}


def _idempotent_upload_result(
    request: Request,
    metadata_service: PostgresMetadataService,
    record: Meme,
    image: Path,
    *,
    original: str,
    reverse_image_policy: str,
    auto_name: bool,
) -> dict[str, object]:
    """构造已存在 durable 图片的幂等成功结果并复用当前处理状态。"""
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
    worker = _processing_worker(request)
    snapshot = worker.jobs.latest_for_target(record.id, record.sha256) if worker is not None else None
    if snapshot is None:
        try:
            snapshot = _submit_processing_job_for_image(
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


@app.post("/images/upload", tags=["images"])
async def upload_images(
    request: Request,
) -> dict[str, object]:
    """解析扁平上传表单并逐文件校验保存，批量中的失败不会回滚成功文件。"""
    settings: Settings = request.app.state.settings
    configured_file_limit = int(getattr(settings, "max_files_per_request", MAX_UPLOAD_FILES_PER_REQUEST))
    file_limit = max(1, min(configured_file_limit, MAX_UPLOAD_FILES_PER_REQUEST))
    max_request_bytes = getattr(settings, "max_request_bytes", None)
    if max_request_bytes is not None:
        max_request_bytes = int(max_request_bytes)
    try:
        # 多解析一个文件是为了把恰好越过业务上限的请求映射为稳定错误；更大的
        # 请求仍由框架 parser 在读取过量文件时拒绝，不进入任何 durable 写入路径。
        form = await _parse_upload_form(request, max_files=file_limit + 1, max_request_bytes=max_request_bytes)
    except HTTPException as exc:
        if exc.status_code == 400 and "Too many files" in str(exc.detail):
            raise _error(413, "too_many_files", "单个上传请求最多包含 20 个文件") from exc
        raise
    unknown = set(form.keys()) - {"auto_name", "files", "reverse_image_policy"}
    if unknown:
        raise _error(400, "invalid_request", "上传不接受已废弃的目标目录字段")
    try:
        auto_name = _parse_multipart_bool(form.get("auto_name"), default=False)
        options = _normalize_processing_options(request, reverse_image_policy=form.get("reverse_image_policy"), auto_name=auto_name)
    except ImageProcessingError as exc:
        status = 503 if exc.code == "reverse_image_unavailable" else 400
        raise _error(status, exc.code, "图片处理选项无效或服务不可用") from exc
    reverse_image_policy = options.reverse_image_policy
    auto_name = options.auto_name
    files = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "read")]
    if not files:
        raise _error(400, "files_required", "必须上传图片文件")
    if len(files) > file_limit:
        raise _error(413, "too_many_files", "单个上传请求最多包含 20 个文件")
    metadata_service = _service(request, "metadata")
    # multipart parser 已在返回 form 前按所有文件 part 校验总预算；这里不再把文件
    # 全部预读到列表，只保留当前文件的受限内容，顺序完成校验和 durable 处理。
    results = []
    upload_batch_id = uuid4().hex if len(files) > 1 else None
    upload_task_ids: list[str] = []
    unified_processing_worker_used = False
    for upload in files:
        # 释放上一项的内容缓冲后再触碰下一个 spool，避免请求内文件字节累积。
        content = b""
        content, too_large = await _read_upload_content(upload, max_upload_size=settings.max_upload_size)
        original = upload.filename or "image"
        clean = _safe_filename(original)
        if Path(clean).suffix.lower() not in SUPPORTED_EXTENSIONS:
            results.append({"filename": original, "ok": False, "error": "unsupported_format"})
            continue
        if too_large:
            results.append({"filename": original, "ok": False, "error": "file_too_large"})
            continue
        try:
            validate_business_storage_key(clean)
        except ValueError:
            results.append({"filename": original, "ok": False, "error": "invalid_filename"})
            continue
        try:
            validate_image_content(content, Path(clean).suffix.lower())
        except ImagePreflightError as exc:
            results.append({"filename": original, "ok": False, "error": exc.code})
            continue
        content_digest = sha256_bytes(content)
        try:
            existing = metadata_service.find_existing_upload(clean, sha256=content_digest, size_bytes=len(content))
        except MetadataError as exc:
            if exc.code == "upload_reconciliation_required":
                results.append({"filename": original, "ok": False, "error": exc.code})
                continue
            results.append({"filename": original, "ok": False, "error": "file_exists"})
            continue
        if existing is not None:
            existing_record, existing_path = existing
            results.append(_idempotent_upload_result(request, metadata_service, existing_record, existing_path, original=original, reverse_image_policy=reverse_image_policy, auto_name=auto_name))
            continue
        target = metadata_service.blob_store.resolve(clean, must_exist=False)
        grant = None
        try:
            upload_key = f"upload:{content_digest}:{clean}"
            grant = _acquire_operation(request, Operations.IMAGE_UPLOAD, upload_key, resource_id=clean, source="upload", input_digest=content_digest)
        except OperationPolicyError as exc:
            results.append({"filename": original, "ok": False, **exc.payload()})
            continue
        try:
            meme_id, saved_path = metadata_service.upload_bytes(content, target_key=clean)
        except (OSError, MetadataError) as exc:
            if isinstance(exc, MetadataError) and exc.code == "target_exists":
                # 并发首个提交可能已经完成 durable 落位；重新验证三方事实后
                # 将竞争请求收束为幂等成功，不重复 acquire operation 或创建 Meme。
                try:
                    existing = metadata_service.find_existing_upload(clean, sha256=content_digest, size_bytes=len(content))
                except MetadataError:
                    existing = None
                if existing is not None:
                    existing_record, existing_path = existing
                    try:
                        _release_operation(request, grant)
                    except OperationPolicyError:
                        # 竞争请求可能已经由另一执行者提交同一 grant；此时
                        # durable 事实优先，保留 policy 的未知状态供恢复器处理。
                        pass
                    results.append(_idempotent_upload_result(request, metadata_service, existing_record, existing_path, original=original, reverse_image_policy=reverse_image_policy, auto_name=auto_name))
                    continue
            # 只有明确知道 durable 写入未开始时才能 release；普通 I/O 异常保留
            # reservation，避免把未知副作用错误地返还给宿主额度。
            if isinstance(exc, MetadataError) and exc.code in {"target_exists", "invalid_filename", "invalid_image", "staging_conflict", "staging_write_failed"} and grant is not None:
                try:
                    _release_operation(request, grant)
                except OperationPolicyError:
                    pass
            results.append({"filename": original, "ok": False, "error": "metadata_write_failed"})
            continue
        if grant is not None:
            try:
                _commit_operation(request, grant)
            except OperationPolicyError:
                # 文件和 Meme 已经 durable，不能回滚或 release；保留成功事实。
                pass
        meme_id = str(meme_id)
        target = saved_path
        result = {"meme_id": meme_id, "filename": original, "ok": True, "saved_filename": target.name, "media_url": f"/media/{meme_id}", "auto_named": False}
        processing_worker = None
        visual_task_id_for_processing: str | None = None
        try:
            processing_worker = _processing_worker(request)
            if processing_worker is not None:
                unified_processing_worker_used = True
            if processing_worker is None:
                # 没有统一图片控制面时保留旧视觉入口；生产 PostgreSQL 路径
                # 会先建立完整 Job，再由 pipeline Worker 创建 visual 叶子。
                task = _submit_visual_task(request, target, batch_id=upload_batch_id, reverse_image_policy=reverse_image_policy, schedule=upload_batch_id is None)
                result["visual_task_id"] = task.task_id
                result["visual_job_id"] = task.task_id
                result["metadata_job_id"] = task.task_id
                result["visual_task_status"] = task.status
                if upload_batch_id:
                    upload_task_ids.append(task.task_id)
        except (OSError, RuntimeError, MetadataError) as exc:
            # 图片和 pending Meme 记录已有效提交，视觉任务失败可由显式阶段重试恢复。
            result["visual_task_error"] = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
            result["metadata_job_error"] = result["visual_task_error"]
        result["metadata_status"] = metadata_service.status(target)["status"]
        result["auto_name"] = auto_name
        result["reverse_image_policy"] = reverse_image_policy
        try:
            worker = processing_worker
            if worker is not None:
                embedding_record = metadata_service.embedding_record(target)
                processing = worker.submit(
                    meme_id,
                    metadata_service.image_sha256(target),
                    metadata_hash=embedding_record.get("metadata_hash") if isinstance(embedding_record, Mapping) else None,
                    config=_processing_config(request),
                    reverse_image_policy=reverse_image_policy,
                    auto_name=auto_name,
                    schedule=False,
                )
                worker.schedule(processing.job_id)
                # 上传 API 继续提供旧字段，但其值明确是完整 Job 标识；
                # 阶段层级和真实叶子任务通过图片处理状态接口读取。
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
            # 图片入库事实已经完成；控制面异常只作为可重试诊断返回。
            result["processing_job_error"] = getattr(exc, "code", "image_processing_unavailable")
        _invalidate_search(request)
        results.append(result)
    # 统一图片 Worker 使用逐图增量向量，不再为上传批次创建旧 cache_generation。
    if upload_batch_id and upload_task_ids and not unified_processing_worker_used:
        _service(request, "tasks").seal_batch(upload_batch_id)
        for task_id in upload_task_ids:
            _service(request, "tasks").schedule(task_id)
    return {"batch_id": upload_batch_id, "results": results}


@app.post("/images/context", status_code=202, tags=["images", "tasks"])
async def generate_context(request: Request, payload: ContextRequest) -> dict[str, object]:
    """兼容图片语境入口，并注入当前 scope 目标和处理 Job callback。"""
    return await _generate_context_http(
        request,
        payload,
        service=_service,
        environment=_environment,
        submit_processing_job=_submit_processing_job_for_image,
        error=_error,
        operation_error=_operation_http_error,
    )


@app.post("/images/context/batch", tags=["images", "tasks"])
async def generate_context_batch(request: Request, payload: ContextBatchRequest) -> dict[str, object]:
    """兼容批量图片语境入口，并注入当前 scope 任务 callback。"""
    return await _generate_context_batch_http(
        request,
        payload,
        service=_service,
        submit_processing_job=_submit_processing_job_for_image,
        error=_error,
        enqueue_error=_context_enqueue_error,
    )


@app.post("/images/visual-embedding", status_code=202, tags=["images", "tasks"])
async def generate_visual_embedding(request: Request, payload: ContextRequest) -> dict[str, object]:
    """兼容视觉向量入口，并注入当前 scope 处理 Job callback。"""
    return await _generate_visual_embedding_http(
        request,
        payload,
        service=_service,
        submit_processing_job=_submit_processing_job_for_image,
        error=_error,
        enqueue_error=_context_enqueue_error,
    )


@app.post("/images/visual-embedding/batch", status_code=202, tags=["images", "tasks"])
async def generate_visual_embedding_batch(request: Request, payload: ContextBatchRequest) -> dict[str, object]:
    """兼容批量视觉向量入口，并注入当前 scope 处理 Job callback。"""
    return await _generate_visual_embedding_batch_http(
        request,
        payload,
        service=_service,
        submit_processing_job=_submit_processing_job_for_image,
        enqueue_error=_context_enqueue_error,
    )


@app.post("/images/metadata/repair", status_code=202, tags=["images", "tasks"])
async def repair_metadata(request: Request) -> dict[str, object]:
    """兼容 metadata repair 入口，并注入当前 scope task service。"""
    return await _repair_metadata_http(request, task_service=lambda received: _service(received, "tasks"))


def create_app(*, scope_resolver, service_factory: ScopeServiceFactory | None = None, operation_policy=None, callback_issuer=None, callback_verifier=None, agent_input_provider: Callable[[ScopeContext, Path], str | Path] | None = None, workspace_provider=None, extensions: Sequence[ApplicationExtension] | None = None) -> FastAPI:
    """创建显式绑定 scope resolver 的 FastAPI 应用。

    ``scope_resolver`` 是必填参数；适配宿主可注入自己的可信 resolver、兼容的
    service factory 和 non-local Agent 输入 provider。未传 resolver 直接抛出稳定错误，
    绝不静默安装 local fallback。
    """
    if scope_resolver is None:
        raise ScopeResolutionError("应用必须显式配置 scope resolver")
    if not any(callable(getattr(scope_resolver, name, None)) for name in ("resolve", "resolve_scope")) and not callable(scope_resolver):
        raise ScopeResolutionError("scope resolver 不可调用")
    if isinstance(scope_resolver, LocalScopeResolver) and scope_resolver.scope.scope_id != "local":
        raise ScopeResolutionError("local resolver 只能绑定 local scope")
    if operation_policy is not None and not all(callable(getattr(operation_policy, name, None)) for name in ("probe", "acquire", "commit", "release")):
        raise OperationPolicyError("operation_policy_unavailable")
    configured_extensions = tuple(extensions or ())
    created = FastAPI(
        title=_route_template.title,
        version=_route_template.version,
        description=_route_template.description,
        lifespan=lifespan,
        responses={400: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
    )
    # APIRoute/StaticFiles 对象只保存不可变路由元数据，复制引用不会共享 scope 状态。
    created.router.routes.extend(_route_template.router.routes)
    created.exception_handlers.update(_route_template.exception_handlers)
    for middleware in reversed(_route_template.user_middleware):
        created.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)
    created.state.extensions = configured_extensions
    created.state.expose_scope = True
    for extension in configured_extensions:
        register_routes = getattr(extension, "register_routes", None)
        if callable(register_routes):
            register_routes(created)
    created.state.scope_resolver = scope_resolver
    # policy、callback issuer/verifier 与 scope resolver 分属不同信任边界，均只从
    # 同一个 keyword-only factory 进入应用，不使用模块级可变依赖。
    created.state.operation_policy = operation_policy if operation_policy is not None else (AllowAllOperationPolicy() if isinstance(scope_resolver, LocalScopeResolver) else None)
    created.state.callback_issuer = callback_issuer
    created.state.callback_verifier = callback_verifier
    if service_factory is not None:
        created.state.service_factory = service_factory
        created.state._scope_factory_managed = False
    if agent_input_provider is not None:
        created.state.agent_input_provider = agent_input_provider
    if workspace_provider is not None:
        created.state.workspace_provider = workspace_provider
    return created


# 开源模块级入口显式安装 local；其他宿主必须调用 create_app 并提供自己的 resolver。
app = create_app(scope_resolver=LocalScopeResolver("local"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8275, reload=False)
