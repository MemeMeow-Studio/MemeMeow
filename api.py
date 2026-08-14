"""MemeMeow FastAPI 应用入口。

路由层只处理 HTTP 契约和安全边界，搜索、持久任务与 OpenCode 服务在 lifespan 中初始化。
"""

from __future__ import annotations

import mimetypes
import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from backend.config import Settings
from backend.errors import ErrorBody
from backend.metadata import MetadataError
from backend.database import DatabaseError, DatabaseResources, check_database, create_engine_for_settings
from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService
from backend.paths import PathResolver, SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.rate_limiter import RateLimiter
from backend.opencode import OpenCodeError, OpenCodeRunner
from backend.tasks import TaskRecord


class SearchRequest(BaseModel):
    """规范检索请求。"""

    query: str = Field(min_length=1, max_length=500)
    n_results: StrictInt = Field(default=5, ge=1, le=30)
    llm_enhance: bool = False


class RenameRequest(BaseModel):
    """图片重命名请求。"""

    meme_id: str | None = None
    new_name: str = Field(min_length=1, max_length=255)


class DeleteRequest(BaseModel):
    """按稳定 meme_id 删除图片的请求。"""

    meme_id: str | None = None


class ContextRequest(BaseModel):
    """按稳定 meme_id 创建图片语境任务的请求。"""

    meme_id: str | None = None


class ContextBatchRequest(BaseModel):
    """批量补齐既有图片语境的请求。"""

    items: list[ContextRequest] = Field(default_factory=list, max_length=500)
    include_unready: bool = True


class CollectionRequest(BaseModel):
    """合集创建和重命名请求。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class CollectionItemsRequest(BaseModel):
    """合集批量成员请求；空数组在 API 边界拒绝。"""

    model_config = ConfigDict(extra="forbid")
    meme_ids: list[str] = Field(min_length=1, max_length=500)


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造统一错误异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


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


def _media_for_meme(request: Request, meme_id: str) -> str | None:
    """将当前 scope 的稳定 meme_id 映射为受控媒体 URL。"""
    try:
        _record, image = request.app.state.metadata.image_for_meme(meme_id)
        return f"/media/{meme_id}"
    except (MetadataError, ValueError):
        return None


def _invalidate_search(request: Request) -> None:
    """通知检索服务；PostgreSQL generation 不在进程内缓存。"""
    invalidate = getattr(request.app.state.search_engine, "invalidate_cache", None)
    if invalidate:
        invalidate()


def _context_payload(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None) -> dict[str, object]:
    """构造可持久化的图片语境任务输入，不保存密钥或提示词。"""
    settings: Settings = request.app.state.settings
    runner: OpenCodeRunner = request.app.state.opencode
    relative = request.app.state.resolver.relative(image)
    meme_id = str(request.app.state.metadata.meme_id_for_image(image))
    try:
        skill_hash = runner.skill_hash()
    except (OSError, OpenCodeError):
        skill_hash = None
    payload: dict[str, object] = {
        "meme_id": meme_id,
        "image_relative_path": relative,
        "image_sha256": expected_sha256 or request.app.state.metadata.image_sha256(image),
        "auto_name": auto_name,
        "model": settings.opencode_model,
        "skill_hash": skill_hash,
        "settings_version": settings.settings_version,
        "agent_concurrency": settings.opencode_concurrency,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_context_task(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None) -> TaskRecord:
    """提交或复用同一图片内容的语境生成任务。"""
    return request.app.state.tasks.submit("meme_context_generation", _context_payload(request, image, auto_name=auto_name, batch_id=batch_id, expected_sha256=expected_sha256))


def _context_enqueue_error(exc: Exception) -> str:
    """把任务提交异常转换为不暴露内部路径的稳定错误码。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc).split(":", 1)[0]
    return text if text == "agent_backpressure" else "context_enqueue_failed"


def _collection_payload(environment, row) -> dict[str, object]:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化一次服务依赖，并在关闭时终止未完成任务。"""
    settings = Settings.from_env()
    settings.ensure_directories()
    try:
        engine = create_engine_for_settings(settings)
        check_database(engine, expected_revision=settings.expected_database_revision)
    except DatabaseError:
        # 启动门禁拒绝任何可能回退到旧 JSON 的业务请求；测试和生产都必须显式准备 PostgreSQL。
        raise
    app.state.settings = settings
    app.state.resolver = PathResolver(settings.image_root)
    app.state.database = DatabaseResources(engine, image_root=settings.image_root, data_root=settings.data_root, settings=settings)
    preflight = app.state.database.flat_preflight("local")
    if any(preflight.get(key) for key in ("non_flat_keys", "nested_images", "orphan_files", "missing_files", "mismatched")):
        raise DatabaseError("flat_meme_storage_preflight_failed")
    app.state.metadata = PostgresMetadataService(app.state.database)
    app.state.search_engine = PostgresSearchService(settings, app.state.database, app.state.metadata)
    app.state.opencode = OpenCodeRunner(settings)
    tasks = PostgresTaskService(
        app.state.database,
        agent_concurrency=settings.opencode_concurrency,
        agent_backpressure=settings.agent_backpressure,
        settings_version=settings.settings_version,
        lease_seconds=settings.worker_lease_seconds,
        max_attempts=settings.worker_max_attempts,
    )

    def cache_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建缓存生成工作。"""
        claim = None
        if isinstance(payload.get("_claim_task_id"), str) and isinstance(payload.get("_claim_generation"), int) and isinstance(payload.get("_claim_owner"), str):
            claim = (str(payload["_claim_task_id"]), int(payload["_claim_generation"]), str(payload["_claim_owner"]))
        if claim is None or not isinstance(app.state.search_engine, PostgresSearchService):
            result = app.state.search_engine.generate_cache(progress)
        else:
            result = app.state.search_engine.generate_cache(progress, claim=claim)
        return result

    def repair_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建数据库元数据完整性扫描工作。"""
        result = app.state.metadata.repair(progress)
        app.state.search_engine.invalidate_cache()
        return result

    def context_handler(payload: dict[str, object], progress):
        """执行 Agent 候选校验、指纹复核与受保护数据库语境写回。"""
        relative = payload.get("image_relative_path")
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        if not isinstance(meme_id, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            _record, image = app.state.metadata.image_for_meme(meme_id)
            relative = app.state.database.blob_store.relative(image)
        except MetadataError as exc:
            raise RuntimeError("target_changed") from exc
        try:
            current_sha = app.state.metadata.image_sha256(image)
        except MetadataError as exc:
            # Agent 运行期间图片可能被删除；这属于提交目标变化而非普通任务故障。
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        try:
            candidate, session_id = app.state.opencode.run(image, progress, task_id=str(payload.get("_claim_task_id") or ""))
        except OpenCodeError as exc:
            try:
                app.state.metadata.record_error(image, producer="research", model=app.state.settings.opencode_model, error=exc.code)
            except MetadataError:
                pass
            raise RuntimeError(f"{exc.code}: {exc}") from exc
        try:
            current_sha = app.state.metadata.image_sha256(image)
        except MetadataError as exc:
            # Agent 运行期间图片可能被删除；这属于提交目标变化而非普通任务故障。
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        claim = None
        if isinstance(payload.get("_claim_task_id"), str) and isinstance(payload.get("_claim_generation"), int) and isinstance(payload.get("_claim_owner"), str):
            claim = (str(payload["_claim_task_id"]), int(payload["_claim_generation"]), str(payload["_claim_owner"]))
        try:
            metadata = app.state.metadata.update_context(image, candidate, producer="research", model=app.state.settings.opencode_model, status="ready", error=None, expected_sha256=expected_sha, claim=claim)
        except MetadataError as exc:
            if exc.code == "claim_expired":
                raise RuntimeError("target_changed") from exc
            raise RuntimeError("agent_output_schema_invalid") from exc
        mark_invalidated = getattr(app.state.search_engine, "mark_cache_invalidated", None)
        if mark_invalidated:
            mark_invalidated(payload.get("batch_id"))
        else:
            app.state.search_engine.invalidate_cache()
        result: dict[str, object] = {
            "image_relative_path": relative,
            "meme_id": meme_id,
            "session_id": session_id,
            "auto_named": False,
        }
        metadata_hash = app.state.metadata.embedding_record(image)["metadata_hash"]
        if payload.get("auto_name") and metadata.meme_context.title:
            try:
                target = image.parent / _filename_from_title(metadata.meme_context.title, image.suffix)
                if target != image:
                    app.state.metadata.rename_by_id(meme_id, target)
                    result["auto_named"] = True
                    result["saved_filename"] = target.name
                    result["image_relative_path"] = app.state.resolver.relative(target)
            except (MetadataError, ValueError, OSError):
                result["auto_name_error"] = "auto_name_failed"
        # 自动命名可能改变 storage_key，最终哈希必须从实际提交路径读取。
        final_image = image
        if isinstance(result.get("saved_filename"), str):
            final_image = image.with_name(str(result["saved_filename"]))
        try:
            result["metadata_hash"] = app.state.metadata.embedding_record(final_image)["metadata_hash"]
        except MetadataError:
            result["metadata_hash"] = metadata_hash
        return result

    tasks.register("cache_generation", cache_handler)
    tasks.register("metadata_repair", repair_handler)
    tasks.register("meme_context_generation", context_handler)

    def finalize_context_batch(batch_id: str):
        """批次所有语境任务收束后只提交一个去重的缓存生成任务。"""
        try:
            tasks.submit("cache_generation", {})
        except Exception:
            # 缓存任务的唯一约束/背压失败不能改变已完成的语境任务，后续可显式重试。
            pass

    tasks.set_batch_finalizer(finalize_context_batch)
    app.state.tasks = tasks
    app.state.metadata.recover_storage(limit=500)
    tasks.start()
    try:
        yield
    finally:
        app.state.opencode.shutdown()
        app.state.tasks.shutdown()
        engine.dispose()


app = FastAPI(
    title="MemeMeow API",
    version="2.0.0",
    description="MemeMeow 图片检索、图片库和异步语境处理 API。模型密钥只在服务端环境中读取。",
    lifespan=lifespan,
    responses={400: {"model": ErrorBody}, 403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """将 FastAPI 默认 422 统一转换为可识别的请求错误。"""
    is_collection = request.url.path == "/collections" or request.url.path.startswith("/collections/")
    return JSONResponse(status_code=422 if is_collection else 400, content={"error": "invalid_collection_request" if is_collection else "invalid_request", "message": "请求参数校验失败"})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """保证业务异常都使用 `{error, message}` 结构。"""
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = exc.detail
    else:
        content = {"error": "http_error", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)


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
async def health(request: Request) -> dict[str, str]:
    """返回可用于容器探活的状态。"""
    return {"status": "ok" if getattr(request.app.state, "search_engine", None) is not None else "degraded"}


@app.get("/config", tags=["system"])
async def config_status(request: Request) -> dict[str, object]:
    """返回脱敏配置状态，绝不返回完整密钥。"""
    status = request.app.state.settings.status()
    # embedding 缓存属于运行时状态，供前端判断当前是否可以直接检索。
    engine = getattr(request.app.state, "search_engine", None)
    status["embedding_cache_ready"] = bool(engine and engine.has_cache())
    status["database_ready"] = True
    status["scope_id"] = "local"
    return status


@app.post("/search", tags=["search"])
async def search_images(request: Request, payload: SearchRequest) -> dict[str, list[str]]:
    """执行唯一规范语义检索入口。"""
    query = payload.query.strip()
    if not query:
        raise _error(400, "invalid_query", "query 不能为空")
    engine = getattr(request.app.state, "search_engine", None)
    if engine is None:
        raise _error(503, "service_unavailable", "检索服务未初始化")
    if not engine.has_cache():
        raise _error(503, "cache_not_ready", "检索缓存尚未就绪")
    settings: Settings = request.app.state.settings
    try:
        results = engine.search(query, payload.n_results, api_key=settings.embedding_api_key, use_llm=payload.llm_enhance)
    except Exception as exc:  # noqa: BLE001
        if "embedding_not_configured" in str(exc):
            raise _error(503, "configuration_missing", "嵌入模型配置未完成")
        if not payload.llm_enhance:
            raise _error(500, "search_failed", "检索失败")
        try:
            results = engine.search(query, payload.n_results, api_key=settings.embedding_api_key, use_llm=False)
        except Exception as fallback_exc:  # noqa: BLE001
            if "embedding_not_configured" in str(fallback_exc):
                raise _error(503, "configuration_missing", "嵌入模型配置未完成")
            raise _error(500, "search_failed", "检索失败")
    resolver: PathResolver = request.app.state.resolver
    mapped: list[str] = []
    for item in results or []:
        media = _media_for_meme(request, str(item)) if isinstance(item, str) else None
        if media and media not in mapped:
            mapped.append(media)
        if len(mapped) >= payload.n_results:
            break
    return {"results": mapped}


@app.post("/generate-cache", status_code=202, tags=["tasks"])
async def generate_cache(request: Request) -> dict[str, object]:
    """提交缓存生成任务；同类任务重复提交返回已有任务。"""
    engine = getattr(request.app.state, "search_engine", None)
    if engine is None:
        raise _error(503, "service_unavailable", "检索服务未初始化")

    record = request.app.state.tasks.submit("cache_generation", {})
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


def _task_summary(request: Request, record: TaskRecord) -> dict[str, object]:
    """将任务转换为列表安全摘要，并附加稳定 Meme 媒体引用。"""
    data = record.as_dict(include_payload=False)
    payload = record.payload
    meme_id = payload.get("meme_id")
    if isinstance(meme_id, str):
        try:
            _meme_record, image = request.app.state.metadata.image_for_meme(meme_id)
            data["image"] = {"meme_id": meme_id, "media_url": f"/media/{meme_id}", "filename": image.name}
        except MetadataError:
            data["image"] = {"meme_id": meme_id}
    return data


@app.get("/tasks", tags=["tasks"])
async def list_tasks(
    request: Request,
    status: list[str] = Query(default=[]),
    task_type: list[str] = Query(default=[]),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, object]:
    """按筛选条件分页列出任务安全摘要。"""
    records, next_cursor = request.app.state.tasks.list(statuses=set(status) or None, task_types=set(task_type) or None, cursor=cursor, limit=limit)
    return {"items": [_task_summary(request, record) for record in records], "next_cursor": next_cursor}


@app.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(request: Request, task_id: str) -> dict[str, object]:
    """查询持久任务详情。"""
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise _error(404, "task_not_found", "任务不存在")
    return _task_summary(request, record)



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
    with request.app.state.database.environment("local") as environment:
        records = environment.memes.list(search=search, page=page, page_size=page_size)
        total = environment.memes.count(search=search)
    items = []
    for record in records:
        try:
            image = request.app.state.metadata.blob_store.resolve(record.storage_key)
            identity = request.app.state.metadata._identity(image)
        except (DatabaseError, MetadataError):
            continue
        metadata_status = request.app.state.metadata.status(image)
        items.append({"meme_id": str(record.id), "filename": record.storage_key, "extension": record.extension, "size": identity["size_bytes"], "media_url": f"/media/{record.id}", "metadata": metadata_status, "embedding_status": "ready" if request.app.state.search_engine.has_cache() and metadata_status.get("status") in {"partial", "ready"} else "blocked" if metadata_status.get("status") == "repair_required" else "pending"})
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.get("/images/metadata", tags=["images"])
async def image_metadata(
    request: Request,
    meme_id: str | None = Query(default=None),
) -> dict[str, object]:
    """按稳定 ``meme_id`` 返回当前 scope 的数据库语境记录。"""
    if not meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        _record, image = request.app.state.metadata.image_for_meme(meme_id)
        metadata = request.app.state.metadata.load(image)
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
        _record, path = request.app.state.metadata.image_for_meme(meme_id)
    except MetadataError as exc:
        raise _error(404, "meme_not_found", "图片不存在") from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.get("/collections", tags=["collections"])
async def list_collections(request: Request, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, object]:
    """分页列出固定 local scope 的合集。"""
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise _error(400, "invalid_request", "合集列表不接受 scope 或 user 参数")
    with request.app.state.database.environment("local") as environment:
        rows = environment.collections.list(page=page, page_size=page_size)
        return {"items": [_collection_payload(environment, row) for row in rows], "total": environment.collections.count(), "page": page, "page_size": page_size}


@app.post("/collections", status_code=201, tags=["collections"])
async def create_collection(request: Request, payload: CollectionRequest) -> dict[str, object]:
    """创建当前 local scope 的空合集。"""
    try:
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.create(payload.name)
            return _collection_payload(environment, row)
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.get("/collections/{collection_id}", tags=["collections"])
async def get_collection(request: Request, collection_id: str, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=100)) -> dict[str, object]:
    """返回合集元数据和当前文件信息的分页成员。"""
    unknown = set(request.query_params) - {"page", "page_size"}
    if unknown:
        raise _error(400, "invalid_request", "合集详情不接受 scope 或 user 参数")
    try:
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.get(collection_id)
            if row is None:
                raise DatabaseError("collection_not_found")
            members = []
            for _item, meme in environment.collections.members(row.id, page=page, page_size=page_size):
                try:
                    image = request.app.state.metadata.blob_store.resolve(meme.storage_key)
                    metadata_status = request.app.state.metadata.status(image)
                except (DatabaseError, MetadataError):
                    metadata_status = {"status": "repair_required", "error": "image_unreadable"}
                embedding_status = "ready" if request.app.state.search_engine.has_cache() and metadata_status.get("status") in {"partial", "ready"} else "blocked" if metadata_status.get("status") == "repair_required" else "pending"
                members.append({"meme_id": str(meme.id), "filename": meme.storage_key, "extension": meme.extension, "size": meme.size_bytes, "media_url": f"/media/{meme.id}", "metadata": metadata_status, "embedding_status": embedding_status})
            payload = _collection_payload(environment, row)
            payload.update({"members": members, "total": environment.collections.member_count(row.id), "page": page, "page_size": page_size})
            return payload
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.patch("/collections/{collection_id}", tags=["collections"])
async def rename_collection(request: Request, collection_id: str, payload: CollectionRequest) -> dict[str, object]:
    """重命名合集，不改变成员关系。"""
    try:
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.rename(collection_id, payload.name)
            return _collection_payload(environment, row)
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.delete("/collections/{collection_id}", tags=["collections"])
async def delete_collection(request: Request, collection_id: str) -> dict[str, object]:
    """删除合集及关系，不删除图片文件。"""
    try:
        with request.app.state.database.environment("local") as environment:
            environment.collections.delete(collection_id)
            return {"collection_id": collection_id, "deleted": True}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/collections/{collection_id}/items", tags=["collections"])
async def add_collection_items(request: Request, collection_id: str, payload: CollectionItemsRequest) -> dict[str, object]:
    """原子批量加入图片并返回幂等计数。"""
    try:
        with request.app.state.database.environment("local") as environment:
            added, existing, total = environment.collections.add_members(collection_id, payload.meme_ids)
            return {"collection_id": collection_id, "added_count": added, "existing_count": existing, "member_count": total}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.delete("/collections/{collection_id}/items/{meme_id}", tags=["collections"])
async def remove_collection_item(request: Request, collection_id: str, meme_id: str) -> dict[str, object]:
    """幂等移除合集成员。"""
    try:
        with request.app.state.database.environment("local") as environment:
            total = environment.collections.remove_member(collection_id, meme_id)
            return {"collection_id": collection_id, "meme_id": meme_id, "removed": True, "member_count": total}
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/images/rename", tags=["images"])
async def rename_image(request: Request, payload: RenameRequest) -> dict[str, str]:
    """按稳定 meme_id 重命名图片并同步数据库 storage_key。"""
    resolver: PathResolver = request.app.state.resolver
    if not payload.meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        _record, source = request.app.state.metadata.image_for_meme(payload.meme_id)
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
    target = source.parent / clean
    if target.exists() and target != source:
        raise _error(409, "file_exists", "目标文件已存在")
    try:
        metadata = request.app.state.metadata.rename_by_id(payload.meme_id, target)
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
        record, image = request.app.state.metadata.image_for_meme(payload.meme_id)
    except MetadataError as exc:
        raise _error(404, "meme_not_found", "图片不存在") from exc
    try:
        request.app.state.metadata.remove_by_id(payload.meme_id)
    except MetadataError as exc:
        raise _error(500, exc.code, "图片及其元数据删除失败") from exc
    _invalidate_search(request)
    return {"meme_id": payload.meme_id, "deleted": True}


@app.post("/images/upload", tags=["images"])
async def upload_images(
    request: Request,
) -> dict[str, object]:
    """解析扁平上传表单并逐文件校验保存，批量中的失败不会回滚成功文件。"""
    form = await request.form()
    unknown = set(form.keys()) - {"auto_name", "files"}
    if unknown:
        raise _error(400, "invalid_request", "上传不接受已废弃的目标目录字段")
    auto_name = str(form.get("auto_name", "false")).lower() in {"1", "true", "yes", "on"}
    files = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "read")]
    if not files:
        raise _error(400, "files_required", "必须上传图片文件")
    resolver: PathResolver = request.app.state.resolver
    settings: Settings = request.app.state.settings
    results = []
    for upload in files:
        original = upload.filename or "image"
        clean = _safe_filename(original)
        if Path(clean).suffix.lower() not in SUPPORTED_EXTENSIONS:
            results.append({"filename": original, "ok": False, "error": "unsupported_format"})
            continue
        content = await upload.read()
        if len(content) > settings.max_upload_size:
            results.append({"filename": original, "ok": False, "error": "file_too_large"})
            continue
        try:
            validate_business_storage_key(clean)
        except ValueError:
            results.append({"filename": original, "ok": False, "error": "invalid_filename"})
            continue
        target = resolver.root / clean
        if target.exists():
            results.append({"filename": original, "ok": False, "error": "file_exists"})
            continue
        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "GIF"}:
                    raise ValueError("unsupported image format")
                image.verify()
        except Exception:  # noqa: BLE001
            results.append({"filename": original, "ok": False, "error": "invalid_image"})
            continue
        try:
            meme_id, saved_path = request.app.state.metadata.upload_bytes(content, target_key=clean)
        except (OSError, MetadataError):
            results.append({"filename": original, "ok": False, "error": "metadata_write_failed"})
            continue
        meme_id = str(meme_id)
        target = saved_path
        result = {"meme_id": meme_id, "filename": original, "ok": True, "saved_filename": target.name, "media_url": f"/media/{meme_id}", "auto_named": False}
        try:
            task = _submit_context_task(request, target, auto_name=auto_name)
            result["metadata_job_id"] = task.task_id
            result["metadata_job_status"] = task.status
        except (OSError, RuntimeError, MetadataError) as exc:
            # 图片和 pending Meme 记录已有效提交，语境任务可由批量补齐恢复。
            result["metadata_job_error"] = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "metadata_enqueue_failed"
        result["metadata_status"] = request.app.state.metadata.status(target)["status"]
        _invalidate_search(request)
        results.append(result)
    return {"results": results}


@app.post("/images/context", status_code=202, tags=["images", "tasks"])
async def generate_context(request: Request, payload: ContextRequest) -> dict[str, object]:
    """为单张图片显式创建或复用语境生成任务。"""
    if payload.meme_id:
        try:
            record, image = request.app.state.metadata.image_for_meme(payload.meme_id)
        except MetadataError as exc:
            if exc.code == "metadata_image_mismatch":
                raise _error(409, "target_changed", "图片内容已变化") from exc
            # 只要数据库 Meme 仍存在，排队请求必须可创建；Worker 会在 claim 后以 target_changed 失败。
            try:
                with request.app.state.database.environment("local") as environment:
                    record = environment.memes.get(payload.meme_id)
            except Exception:
                record = None
            if record is None:
                raise _error(404, "meme_not_found", "图片不存在") from exc
            image = request.app.state.metadata.blob_store.root / record.storage_key
    else:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        task = _submit_context_task(request, image, expected_sha256=getattr(locals().get("record", None), "sha256", None))
    except MetadataError as exc:
        # 任务提交必须先记录目标指纹；文件在请求和排队之间消失时仍返回稳定 404。
        if exc.code in {"image_unreadable", "metadata_image_mismatch", "metadata_missing"}:
            raise _error(404, "meme_not_found", "图片不存在") from exc
        raise _error(409, exc.code, "图片当前不可处理") from exc
    except RuntimeError as exc:
        if str(exc) == "agent_backpressure":
            raise _error(429, "agent_backpressure", "Agent 等待队列已满，请稍后重试") from exc
        raise
    return {"task_id": task.task_id, "task_type": task.task_type, "status": task.status}


@app.post("/images/context/batch", tags=["images", "tasks"])
async def generate_context_batch(request: Request, payload: ContextBatchRequest) -> dict[str, object]:
    """批量为缺失或未就绪图片提交独立语境任务，单项失败不影响其余项。"""
    batch_id = uuid4().hex
    if payload.items:
        paths = [(item.meme_id, None) for item in payload.items]
    else:
        paths = []
    results = []
    scheduled_task_ids: list[str] = []
    for meme_id, _filename in paths:
        try:
            if not meme_id:
                raise MetadataError("meme_id_required")
            record, image = request.app.state.metadata.image_for_meme(meme_id)
            state = request.app.state.metadata.status(image)["status"]
            # 显式 include_unready 请求代表强制重试；仅在调用方未开启时跳过已就绪记录。
            if not payload.include_unready and state not in {"pending", "partial", "repair_required"}:
                results.append({"meme_id": meme_id, "skipped": "already_ready"})
                continue
            if state == "repair_required":
                request.app.state.metadata.create_pending(image)
            task = _submit_context_task(request, image, batch_id=batch_id, expected_sha256=record.sha256)
            scheduled_task_ids.append(task.task_id)
            results.append({"meme_id": meme_id, "task_id": task.task_id, "status": task.status})
        except (HTTPException, MetadataError, OSError, RuntimeError) as exc:
            error_code = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else getattr(exc, "code", "context_enqueue_failed")
            results.append({"meme_id": meme_id, "error": error_code})
    # 先把整个批次的成员提交完成，再唤醒 Worker，避免首个快速任务在后续成员
    # 入批前误判“全部完成”而提前收束 finalizer。
    if scheduled_task_ids:
        request.app.state.tasks.seal_batch(batch_id)
    for task_id in scheduled_task_ids:
        request.app.state.tasks.schedule(task_id)
    return {"batch_id": batch_id, "results": results}


@app.post("/images/metadata/repair", status_code=202, tags=["images", "tasks"])
async def repair_metadata(request: Request) -> dict[str, object]:
    """提交幂等的数据库元数据完整性扫描和修复任务。"""
    record = request.app.state.tasks.submit("metadata_repair", {})
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8275, reload=False)
