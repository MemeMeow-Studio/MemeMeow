"""MemeMeow FastAPI 应用入口。

路由层只处理 HTTP 契约和安全边界，搜索、持久任务与 OpenCode 服务在 lifespan 中初始化。
"""

from __future__ import annotations

import mimetypes
import os
import re
import secrets
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, Field, StrictInt

from backend.config import Settings, update_dotenv_concurrency
from backend.errors import ErrorBody
from backend.metadata import MetadataError, MetadataService
from backend.paths import PathResolver, SUPPORTED_EXTENSIONS
from backend.rate_limiter import RateLimiter
from backend.search import SearchService
from backend.opencode import OpenCodeError, OpenCodeRunner
from backend.tasks import PersistentTaskService, TaskRecord


class SearchRequest(BaseModel):
    """规范检索请求。"""

    query: str = Field(min_length=1, max_length=500)
    n_results: StrictInt = Field(default=5, ge=1, le=30)
    llm_enhance: bool = False


class CreateDirectoryRequest(BaseModel):
    """创建图片目录请求。"""

    name: str = Field(min_length=1, max_length=100)
    parent: str = ""


class RenameRequest(BaseModel):
    """图片重命名请求。"""

    directory: str = ""
    filename: str = Field(min_length=1, max_length=255)
    new_name: str = Field(min_length=1, max_length=255)


class DeleteRequest(BaseModel):
    """删除图片及其 sidecar 的请求。"""

    directory: str = ""
    filename: str = Field(min_length=1, max_length=255)


class ContextRequest(BaseModel):
    """单张图片语境生成或重试请求。"""

    directory: str = ""
    filename: str = Field(min_length=1, max_length=255)


class ContextBatchRequest(BaseModel):
    """批量补齐既有图片语境的请求。"""

    items: list[ContextRequest] = Field(default_factory=list, max_length=500)
    include_unready: bool = True


class ConcurrencyUpdateRequest(BaseModel):
    """后端设置页唯一允许持久化的安全参数。"""

    opencode_concurrency: StrictInt = Field(ge=1, le=8, validation_alias=AliasChoices("opencode_concurrency", "agent_concurrency", "concurrency", "value"))


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
    """从自然语言标题派生安全文件名，并为同目录 sidecar 预留字节长度。"""
    stem = unicodedata.normalize("NFKC", title).strip()
    stem = re.sub(r"[\x00-\x1f/\\:*?\"<>|]", "_", stem).strip(" .")
    if not stem:
        raise ValueError("empty_title")
    suffix = suffix.lower()
    max_stem_bytes = 255 - len(f"{suffix}.json".encode("utf-8"))
    stem = stem.encode("utf-8")[:max_stem_bytes].decode("utf-8", errors="ignore").rstrip(" .")
    if not stem:
        raise ValueError("empty_title")
    return f"{stem}{suffix}"


def _media_for_path(resolver: PathResolver, value: Any) -> str | None:
    """把搜索服务返回的路径或旧格式二元组映射为受控媒体 URL。"""
    candidate = value[0] if isinstance(value, (tuple, list)) else value
    try:
        path = Path(str(candidate)).expanduser().resolve()
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS or not path.is_file():
            return None
        return resolver.media_url(path)
    except (OSError, ValueError):
        return None


def _invalidate_search(request: Request) -> None:
    """通知检索服务丢弃受图片元数据变化影响的进程内索引。"""
    invalidate = getattr(request.app.state.search_engine, "invalidate_cache", None)
    if invalidate:
        invalidate()


def _context_payload(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None) -> dict[str, object]:
    """构造可持久化的图片语境任务输入，不保存密钥或提示词。"""
    settings: Settings = request.app.state.settings
    runner: OpenCodeRunner = request.app.state.opencode
    relative = request.app.state.resolver.relative(image)
    try:
        skill_hash = runner.skill_hash()
    except (OSError, OpenCodeError):
        skill_hash = None
    payload: dict[str, object] = {
        "image_relative_path": relative,
        "image_sha256": request.app.state.metadata.image_sha256(image),
        "auto_name": auto_name,
        "model": settings.opencode_model,
        "skill_hash": skill_hash,
        "settings_version": settings.settings_version,
        "agent_concurrency": settings.opencode_concurrency,
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_context_task(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None) -> TaskRecord:
    """提交或复用同一图片内容的语境生成任务。"""
    return request.app.state.tasks.submit("meme_context_generation", _context_payload(request, image, auto_name=auto_name, batch_id=batch_id))


def _context_enqueue_error(exc: Exception) -> str:
    """把任务提交异常转换为不暴露内部路径的稳定错误码。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc).split(":", 1)[0]
    return text if text == "agent_backpressure" else "context_enqueue_failed"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化一次服务依赖，并在关闭时终止未完成任务。"""
    settings = Settings.from_env()
    settings.ensure_directories()
    app.state.settings = settings
    app.state.resolver = PathResolver(settings.image_root)
    app.state.metadata = MetadataService(settings.image_root)
    app.state.search_engine = SearchService(settings, app.state.metadata)
    app.state.opencode = OpenCodeRunner(settings)
    tasks = PersistentTaskService(
        settings.data_root / "tasks",
        agent_concurrency=settings.opencode_concurrency,
        agent_backpressure=settings.agent_backpressure,
        settings_version=settings.settings_version,
    )

    def cache_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建缓存生成工作。"""
        result = app.state.search_engine.generate_cache(progress)
        return result

    def repair_handler(payload: dict[str, object], progress):
        """从持久任务 payload 重建 sidecar 修复工作。"""
        result = app.state.metadata.repair(progress)
        app.state.search_engine.invalidate_cache()
        return result

    def context_handler(payload: dict[str, object], progress):
        """执行 Agent 候选校验、指纹复核与受保护 sidecar 写回。"""
        relative = payload.get("image_relative_path")
        expected_sha = payload.get("image_sha256")
        if not isinstance(relative, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            image = app.state.resolver.resolve_file(str(Path(relative).parent), Path(relative).name)
        except HTTPException as exc:
            raise RuntimeError("target_changed") from exc
        try:
            current_sha = app.state.metadata.image_sha256(image)
        except MetadataError as exc:
            # Agent 运行期间图片可能被删除；这属于提交目标变化而非普通任务故障。
            raise RuntimeError("target_changed") from exc
        if current_sha != expected_sha:
            raise RuntimeError("target_changed")
        try:
            candidate, session_id = app.state.opencode.run(image, progress)
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
        try:
            metadata = app.state.metadata.update_context(image, candidate, producer="research", model=app.state.settings.opencode_model, status="ready", error=None)
        except MetadataError as exc:
            raise RuntimeError("agent_output_schema_invalid") from exc
        mark_invalidated = getattr(app.state.search_engine, "mark_cache_invalidated", None)
        if mark_invalidated:
            mark_invalidated(payload.get("batch_id"))
        else:
            app.state.search_engine.invalidate_cache()
        result: dict[str, object] = {
            "image_relative_path": relative,
            "session_id": session_id,
            "auto_named": False,
        }
        sidecar_hash = app.state.metadata.embedding_record(image)["metadata_hash"]
        if payload.get("auto_name") and metadata.meme_context.title:
            try:
                target = image.parent / _filename_from_title(metadata.meme_context.title, image.suffix)
                if target != image:
                    app.state.metadata.rename(image, target)
                    result["auto_named"] = True
                    result["saved_filename"] = target.name
                    result["image_relative_path"] = app.state.resolver.relative(target)
            except (MetadataError, ValueError, OSError):
                result["auto_name_error"] = "auto_name_failed"
        # 自动命名可能改变 sidecar 的 relative_path，最终哈希必须从实际提交路径读取。
        final_image = image
        if isinstance(result.get("saved_filename"), str):
            final_image = image.with_name(str(result["saved_filename"]))
        try:
            result["sidecar_hash"] = app.state.metadata.embedding_record(final_image)["metadata_hash"]
        except MetadataError:
            result["sidecar_hash"] = sidecar_hash
        return result

    tasks.register("cache_generation", cache_handler)
    tasks.register("metadata_repair", repair_handler)
    tasks.register("meme_context_generation", context_handler)

    def finalize_context_batch(batch_id: str):
        """批次所有语境任务收束后只提交一个去重的缓存生成任务。"""
        tasks.submit("cache_generation", {})

    tasks.set_batch_finalizer(finalize_context_batch)
    app.state.tasks = tasks
    tasks.start()
    try:
        yield
    finally:
        app.state.opencode.shutdown()
        app.state.tasks.shutdown()


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
    return JSONResponse(status_code=400, content={"error": "invalid_request", "message": "请求参数校验失败"})


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
        media = _media_for_path(resolver, item)
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
    """将任务转换为列表安全摘要，不返回完整 payload 或运行日志。"""
    data = record.as_dict(include_payload=False)
    payload = record.payload
    related = payload.get("image_relative_path")
    if isinstance(related, str):
        try:
            image = request.app.state.resolver.resolve_file(str(Path(related).parent), Path(related).name)
            data["image"] = {"relative_path": related, "media_url": request.app.state.resolver.media_url(image), "filename": image.name}
        except HTTPException:
            data["image"] = {"relative_path": related}
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
    directory: str = Query(default=""),
    search: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """列出受控目录中的图片和子目录。"""
    resolver: PathResolver = request.app.state.resolver
    folder = resolver.resolve_directory(directory)
    items = []
    directories = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if path.is_dir():
            directories.append(path.name)
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS or not path.is_file():
            continue
        if search and search.casefold() not in path.name.casefold():
            continue
        metadata_status = request.app.state.metadata.status(path)["status"]
        embedding_record = request.app.state.metadata.embedding_record(path)
        if metadata_status == "repair_required":
            embedding_status = "blocked"
        elif request.app.state.search_engine.has_cache():
            embedding_status = "ready"
        else:
            embedding_status = "pending"
        items.append(
            {
                "directory": directory,
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size": path.stat().st_size,
                "media_url": resolver.media_url(path),
                "metadata": request.app.state.metadata.status(path),
                "embedding_status": embedding_status,
            }
        )
    start = (page - 1) * page_size
    return {"directory": directory, "directories": directories, "items": items[start : start + page_size], "total": len(items), "page": page, "page_size": page_size}


@app.get("/images/directories", tags=["images"])
async def list_directories(request: Request, parent: str = Query(default="")) -> dict[str, list[str]]:
    """列出指定图片目录下的一级子目录。"""
    folder = request.app.state.resolver.resolve_directory(parent)
    return {"parent": parent, "directories": sorted((p.name for p in folder.iterdir() if p.is_dir()), key=str.casefold)}


@app.get("/media/{file_path:path}", tags=["images"])
async def media(request: Request, file_path: str):
    """通过受控标识返回图片内容。"""
    path = request.app.state.resolver.resolve_file(str(Path(file_path).parent), Path(file_path).name)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@app.post("/images/directories", status_code=201, tags=["images"])
async def create_directory(request: Request, payload: CreateDirectoryRequest) -> dict[str, str]:
    """创建单层图片目录，不覆盖现有目录。"""
    if not re.fullmatch(r"[^/\\\x00-\x1f\x7f]+", payload.name) or payload.name in {".", ".."}:
        raise _error(400, "invalid_directory", "目录名非法")
    parent = request.app.state.resolver.resolve_directory(payload.parent)
    target = parent / payload.name
    if target.exists():
        raise _error(409, "directory_exists", "目录已存在")
    target.mkdir()
    relative = request.app.state.resolver.relative(target)
    return {"directory": relative}


@app.post("/images/rename", tags=["images"])
async def rename_image(request: Request, payload: RenameRequest) -> dict[str, str]:
    """在原目录内安全重命名图片并拒绝覆盖。"""
    resolver: PathResolver = request.app.state.resolver
    source = resolver.resolve_file(payload.directory, payload.filename)
    clean = _safe_filename(payload.new_name)
    if Path(clean).suffix.lower() != source.suffix.lower():
        clean = f"{Path(clean).stem}{source.suffix.lower()}"
    target = source.parent / clean
    if target.exists() and target != source:
        raise _error(409, "file_exists", "目标文件已存在")
    try:
        request.app.state.metadata.rename(source, target)
    except MetadataError as exc:
        if exc.code == "target_exists":
            raise _error(409, "file_exists", "目标文件已存在")
        raise _error(500, "metadata_rename_failed", "图片元数据同步失败")
    _invalidate_search(request)
    return {"directory": payload.directory, "filename": target.name, "media_url": resolver.media_url(target)}


@app.post("/images/delete", tags=["images"])
async def delete_image(request: Request, payload: DeleteRequest) -> dict[str, object]:
    """删除图片并同步删除同目录 sidecar。"""
    resolver: PathResolver = request.app.state.resolver
    image = resolver.resolve_file(payload.directory, payload.filename)
    try:
        request.app.state.metadata.remove(image)
    except MetadataError as exc:
        raise _error(500, exc.code, "图片及其元数据删除失败") from exc
    _invalidate_search(request)
    return {"directory": payload.directory, "filename": payload.filename, "deleted": True}


@app.post("/images/upload", tags=["images"])
async def upload_images(
    request: Request,
    directory: str = Form(default=""),
    auto_name: bool = Form(default=False),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    """逐文件校验并保存图片，批量中的失败不会回滚成功文件。"""
    resolver: PathResolver = request.app.state.resolver
    folder = resolver.resolve_directory(directory)
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
        target = folder / clean
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
            target.write_bytes(content)
            request.app.state.metadata.create_pending(target)
        except (OSError, MetadataError):
            target.unlink(missing_ok=True)
            results.append({"filename": original, "ok": False, "error": "metadata_write_failed"})
            continue
        result = {"filename": original, "ok": True, "saved_filename": target.name, "media_url": resolver.media_url(target), "auto_named": False}
        try:
            task = _submit_context_task(request, target, auto_name=auto_name)
            result["metadata_job_id"] = task.task_id
            result["metadata_job_status"] = task.status
        except (OSError, RuntimeError, MetadataError) as exc:
            # 图片和 pending sidecar 已有效提交，语境任务可由批量补齐恢复。
            result["metadata_job_error"] = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "metadata_enqueue_failed"
        result["metadata_status"] = request.app.state.metadata.status(target)["status"]
        _invalidate_search(request)
        results.append(result)
    return {"directory": directory, "results": results}


@app.post("/images/context", status_code=202, tags=["images", "tasks"])
async def generate_context(request: Request, payload: ContextRequest) -> dict[str, object]:
    """为单张图片显式创建或复用语境生成任务。"""
    image = request.app.state.resolver.resolve_file(payload.directory, payload.filename)
    try:
        task = _submit_context_task(request, image)
    except RuntimeError as exc:
        if str(exc) == "agent_backpressure":
            raise _error(429, "agent_backpressure", "Agent 等待队列已满，请稍后重试") from exc
        raise
    return {"task_id": task.task_id, "task_type": task.task_type, "status": task.status}


@app.post("/images/context/batch", tags=["images", "tasks"])
async def generate_context_batch(request: Request, payload: ContextBatchRequest) -> dict[str, object]:
    """批量为缺失或未就绪图片提交独立语境任务，单项失败不影响其余项。"""
    resolver: PathResolver = request.app.state.resolver
    batch_id = uuid4().hex
    if payload.items:
        paths = [(item.directory, item.filename) for item in payload.items]
    else:
        paths = [
            (("" if path.relative_to(request.app.state.metadata.root).parent == Path(".") else path.relative_to(request.app.state.metadata.root).parent.as_posix()), path.name)
            for path in request.app.state.metadata.root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
    results = []
    for directory, filename in paths:
        try:
            image = resolver.resolve_file(directory, filename)
            state = request.app.state.metadata.status(image)["status"]
            if payload.include_unready and state not in {"pending", "partial", "repair_required"}:
                results.append({"filename": resolver.relative(image), "skipped": "already_ready"})
                continue
            if state == "repair_required":
                request.app.state.metadata.create_pending(image)
            task = _submit_context_task(request, image, batch_id=batch_id)
            results.append({"filename": resolver.relative(image), "task_id": task.task_id, "status": task.status})
        except (HTTPException, MetadataError, OSError, RuntimeError) as exc:
            results.append({"filename": filename, "error": _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else getattr(exc, "code", "context_enqueue_failed")})
    return {"batch_id": batch_id, "results": results}


@app.post("/images/metadata/repair", status_code=202, tags=["images", "tasks"])
async def repair_metadata(request: Request) -> dict[str, object]:
    """提交幂等的 sidecar 初始化和修复任务。"""
    metadata: MetadataService = request.app.state.metadata

    record = request.app.state.tasks.submit("metadata_repair", {})
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8275, reload=False)
