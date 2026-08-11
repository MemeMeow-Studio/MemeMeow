"""MemeMeow FastAPI 应用入口。

路由层只处理 HTTP 契约和安全边界，搜索、VLM 等现有服务在 lifespan 中初始化。
"""

from __future__ import annotations

import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictInt

from backend.config import Settings
from backend.errors import ErrorBody
from backend.metadata import MetadataError, MetadataService
from backend.paths import PathResolver, SUPPORTED_EXTENSIONS
from backend.rate_limiter import RateLimiter
from backend.search import SearchService
from backend.labeling import LabelingService
from backend.tasks import TaskManager


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


class DescribeRequest(BaseModel):
    """单张图片描述请求。"""

    directory: str = ""
    filename: str = Field(min_length=1, max_length=255)


class BatchLabelRequest(BaseModel):
    """批量预生成描述请求。"""

    items: list[DescribeRequest] = Field(min_length=1, max_length=500)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """初始化一次服务依赖，并在关闭时终止未完成任务。"""
    settings = Settings.from_env()
    settings.ensure_directories()
    app.state.settings = settings
    app.state.resolver = PathResolver(settings.image_root)
    app.state.metadata = MetadataService(settings.image_root)
    app.state.tasks = TaskManager()
    app.state.search_engine = SearchService(settings, app.state.metadata)
    app.state.labeling = LabelingService(settings)
    try:
        yield
    finally:
        app.state.tasks.shutdown()


app = FastAPI(
    title="MemeMeow API",
    version="2.0.0",
    description="MemeMeow 图片检索、图片库和标注 API。模型密钥只在服务端环境中读取。",
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
    return request.app.state.settings.status()


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

    def run(progress):
        engine.generate_cache(progress)

    record = request.app.state.tasks.submit("cache_generation", run)
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


@app.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(request: Request, task_id: str) -> dict[str, object]:
    """查询进程内任务状态。"""
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise _error(404, "task_not_found", "任务不存在或服务已重启")
    return record.as_dict()


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
        items.append(
            {
                "directory": directory,
                "filename": path.name,
                "extension": path.suffix.lower(),
                "size": path.stat().st_size,
                "media_url": resolver.media_url(path),
                "metadata": request.app.state.metadata.status(path),
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
        auto_name_error = None
        auto_named = False
        if auto_name:
            try:
                candidates = await _describe_path(request, target)
                candidate = _safe_filename(candidates[0])
                candidate = f"{Path(candidate).stem}{target.suffix.lower()}"
                renamed = target.parent / candidate
                request.app.state.metadata.apply_visual_candidates(target, candidates, settings.vlm_model)
                if not renamed.exists() or renamed == target:
                    request.app.state.metadata.rename(target, renamed)
                    target = renamed
                    auto_named = True
            except Exception as exc:  # noqa: BLE001
                auto_name_error = str(exc)
        result = {"filename": original, "ok": True, "saved_filename": target.name, "media_url": resolver.media_url(target), "auto_named": auto_named}
        if auto_name_error:
            result["auto_name_error"] = "auto_name_failed"
        result["metadata_status"] = request.app.state.metadata.status(target)["status"]
        _invalidate_search(request)
        results.append(result)
    return {"directory": directory, "results": results}


async def _describe_path(request: Request, path: Path) -> list[str]:
    """调用当前应用的 VLM 服务，供单张和上传自动命名共用。"""
    try:
        return request.app.state.labeling.describe(path)
    except Exception as exc:  # noqa: BLE001
        code = "configuration_missing" if "vlm_not_configured" in str(exc) else "vlm_failed"
        message = "VLM 配置未完成" if code == "configuration_missing" else "VLM 描述生成失败"
        try:
            request.app.state.metadata.record_error(path, producer="vision", model=request.app.state.settings.vlm_model, error=code)
        except MetadataError:
            pass
        raise _error(503, code, message) from exc


@app.post("/images/describe", tags=["labeling"])
async def describe_image(request: Request, payload: DescribeRequest) -> dict[str, object]:
    """为图片生成候选描述，不修改图片文件。"""
    path = request.app.state.resolver.resolve_file(payload.directory, payload.filename)
    try:
        candidates = await _describe_path(request, path)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    try:
        request.app.state.metadata.apply_visual_candidates(path, candidates, request.app.state.settings.vlm_model)
    except MetadataError as exc:
        return JSONResponse(status_code=500, content={"error": exc.code, "message": "图片元数据写入失败"})
    _invalidate_search(request)
    return {"directory": payload.directory, "filename": payload.filename, "status": "succeeded", "candidates": candidates, "metadata_status": "partial"}


@app.post("/images/label-batch", status_code=202, tags=["labeling", "tasks"])
async def label_batch(request: Request, payload: BatchLabelRequest) -> dict[str, object]:
    """提交批量 VLM 预生成任务，单张失败不会阻塞其他图片。"""
    resolver: PathResolver = request.app.state.resolver
    paths = [resolver.resolve_file(item.directory, item.filename) for item in payload.items]

    def run(progress):
        results = []
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            try:
                candidates = request.app.state.labeling.describe(path)
                request.app.state.metadata.apply_visual_candidates(path, candidates, request.app.state.settings.vlm_model)
                results.append({"filename": resolver.relative(path), "ok": True})
            except Exception as exc:  # noqa: BLE001
                try:
                    request.app.state.metadata.record_error(path, producer="vision", model=request.app.state.settings.vlm_model, error=str(exc))
                except MetadataError:
                    pass
                results.append({"filename": resolver.relative(path), "ok": False, "error": str(exc)})
            progress(index / total, f"正在描述 {index}/{total}")
        _invalidate_search(request)
        return {"results": results}

    record = request.app.state.tasks.submit("batch_labeling", run)
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


@app.post("/images/metadata/repair", status_code=202, tags=["images", "tasks"])
async def repair_metadata(request: Request) -> dict[str, object]:
    """提交幂等的 sidecar 初始化和修复任务。"""
    metadata: MetadataService = request.app.state.metadata

    def run(progress):
        result = metadata.repair(progress)
        _invalidate_search(request)
        return result

    record = request.app.state.tasks.submit("metadata_repair", run)
    return {"task_id": record.task_id, "task_type": record.task_type, "status": record.status}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8275, reload=False)
