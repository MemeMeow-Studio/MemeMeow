"""MemeMeow FastAPI 应用入口。

路由层只处理 HTTP 契约和安全边界，搜索、持久任务与 OpenCode 服务在 lifespan 中初始化。
"""

from __future__ import annotations

import mimetypes
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, StrictInt

from backend.config import Settings, update_dotenv_concurrency
from backend.collection_packages import (
    CollectionPackageError,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    build_export_archive,
    cleanup_archive,
    preflight_archive,
    resolve_import_filename,
    safe_download_filename,
    sha256_bytes,
)
from backend.errors import ErrorBody
from backend.metadata import MetadataError
from backend.database import DatabaseError, DatabaseResources, check_database, create_engine_for_settings
from backend.pg_services import PostgresMetadataService, PostgresSearchService, PostgresTaskService
from backend.paths import PathResolver, SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.rate_limiter import RateLimiter
from backend.opencode import OpenCodeError, OpenCodeRunner
from backend.opencode_activity import AgentActivity, OpenCodeActivityReader
from backend.reverse_image import ReverseImageError, ReverseImageRequest, ReverseImageService
from backend.tasks import TaskRecord
from backend.visual import VisualEmbeddingError, VisualInferenceClient, VisualSearchError, VisualSearchService, identity_from_settings


STORAGE_PREFLIGHT_BLOCKING_KEYS = ("non_flat_keys", "nested_images", "missing_files", "mismatched")


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
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")


class ContextBatchRequest(BaseModel):
    """批量补齐既有图片语境的请求。"""

    items: list[ContextRequest] = Field(default_factory=list, max_length=500)
    include_unready: bool = True
    reverse_image_policy: str = Field(default="forbid", pattern="^(forbid|auto)$")


class VisualMatchRequest(BaseModel):
    """Agent 视觉匹配请求；scope 和查询图片只能由 task_id 推导。"""

    task_id: str = Field(min_length=1, max_length=255)
    top_k: StrictInt = Field(default=20, ge=1, le=50)
    exclude_self: bool = True


class CollectionRequest(BaseModel):
    """合集创建和重命名请求。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class CollectionItemsRequest(BaseModel):
    """合集批量成员请求；空数组在 API 边界拒绝。"""

    model_config = ConfigDict(extra="forbid")
    meme_ids: list[str] = Field(min_length=1, max_length=500)


class ConcurrencyUpdateRequest(BaseModel):
    """后端设置页唯一允许持久化的安全参数。"""

    opencode_concurrency: StrictInt = Field(ge=1, le=8, validation_alias=AliasChoices("opencode_concurrency", "agent_concurrency", "concurrency", "value"))


def _error(status: int, code: str, message: str) -> HTTPException:
    """构造统一错误异常。"""
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _storage_preflight_summary(report: Mapping[str, object] | None) -> dict[str, object]:
    """生成不包含文件名的存储预检摘要，供健康检查和配置接口诊断。"""
    report = report or {}
    blocking = {
        key: len(value) if isinstance(value, (list, tuple, set, dict)) else 0
        for key in STORAGE_PREFLIGHT_BLOCKING_KEYS
        if (value := report.get(key))
    }
    orphan_files = report.get("orphan_files")
    return {
        "status": "warning" if orphan_files else "ok",
        "orphan_files": len(orphan_files) if isinstance(orphan_files, (list, tuple, set, dict)) else 0,
        "blocking_errors": blocking,
    }


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


def _context_payload(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid") -> dict[str, object]:
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
        "reverse_image_policy": reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_context_task(request: Request, image: Path, *, auto_name: bool = False, batch_id: str | None = None, expected_sha256: str | None = None, reverse_image_policy: str = "forbid", schedule: bool = True) -> TaskRecord:
    """提交或复用同一图片内容的语境生成任务。"""
    runner: OpenCodeRunner = request.app.state.opencode
    if runner.executor_mode or runner.docker_mode:
        runtime = runner.runtime_probe()
        if runtime.get("image_root_match") is False:
            raise RuntimeError("agent_image_root_mismatch")
        if not bool(runtime.get("verified")):
            raise RuntimeError("agent_runtime_unavailable")
    if reverse_image_policy not in {"forbid", "auto"}:
        raise RuntimeError("invalid_reverse_image_policy")
    if reverse_image_policy == "auto" and not request.app.state.reverse_image.available:
        raise RuntimeError("reverse_image_unavailable")
    return request.app.state.tasks.submit("meme_context_generation", _context_payload(request, image, auto_name=auto_name, batch_id=batch_id, expected_sha256=expected_sha256, reverse_image_policy=reverse_image_policy), schedule=schedule)


def _visual_payload(request: Request, image: Path, *, batch_id: str | None = None, expected_sha256: str | None = None, auto_name: bool = False, reverse_image_policy: str = "forbid") -> dict[str, object]:
    """构造视觉任务可序列化 payload，模型身份始终来自服务端配置。"""
    settings: Settings = request.app.state.settings
    identity = identity_from_settings(settings)
    relative = request.app.state.resolver.relative(image)
    meme_id = str(request.app.state.metadata.meme_id_for_image(image))
    payload: dict[str, object] = {
        "scope_id": "local",
        "meme_id": meme_id,
        "image_relative_path": relative,
        "image_sha256": expected_sha256 or request.app.state.metadata.image_sha256(image),
        "visual_model": identity.model,
        "visual_dimensions": identity.dimensions,
        "preprocess_version": identity.preprocess_version,
        "settings_version": settings.settings_version,
        "auto_name": auto_name,
        "reverse_image_policy": reverse_image_policy if reverse_image_policy in {"forbid", "auto"} else "forbid",
    }
    if batch_id:
        payload["batch_id"] = batch_id
    return payload


def _submit_visual_task(request: Request, image: Path, *, batch_id: str | None = None, expected_sha256: str | None = None, auto_name: bool = False, reverse_image_policy: str = "forbid", schedule: bool = True) -> TaskRecord:
    """在图片 durable upload 提交后创建或复用异步视觉任务。"""
    return request.app.state.tasks.submit("visual_embedding_generation", _visual_payload(request, image, batch_id=batch_id, expected_sha256=expected_sha256, auto_name=auto_name, reverse_image_policy=reverse_image_policy), schedule=schedule)


def _context_enqueue_error(exc: Exception) -> str:
    """把任务提交异常转换为不暴露内部路径的稳定错误码。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    text = str(exc).split(":", 1)[0]
    return text if text in {"agent_backpressure", "generation_policy_conflict", "reverse_image_unavailable", "invalid_reverse_image_policy"} else "context_enqueue_failed"


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
    status = 413 if exc.code in {"file_too_large", "package_too_large", "member_count_exceeded", "manifest_too_large"} else 409 if exc.code in {"collection_exists", "member_unreadable", "member_changed"} else 400
    messages = {
        "invalid_zip": "合集 ZIP 无法读取",
        "manifest_missing": "合集 ZIP 缺少 manifest.json",
        "manifest_invalid": "合集 manifest 无效",
        "unsupported_package_version": "不支持的合集包格式版本",
        "manifest_entries_mismatch": "合集 manifest 与 ZIP 文件不一致",
        "sha256_mismatch": "图片 SHA-256 校验失败",
        "size_mismatch": "图片大小校验失败",
        "invalid_zip_path": "ZIP 路径非法",
        "unsafe_zip_entry": "ZIP 包含不安全条目",
        "duplicate_zip_entry": "ZIP 包含重复条目",
        "invalid_image": "包内图片无法解码",
        "unsupported_format": "包内图片格式不受支持",
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
    # 根目录孤立图片不会进入数据库图片库，完整性任务会报告它们；它们本身不应阻断主服务启动。
    # 非扁平记录、嵌套图片和已登记文件的不一致仍然阻断，避免受控媒体接口读到错误对象。
    app.state.storage_preflight = preflight
    if any(preflight.get(key) for key in STORAGE_PREFLIGHT_BLOCKING_KEYS):
        raise DatabaseError("flat_meme_storage_preflight_failed")
    app.state.metadata = PostgresMetadataService(app.state.database)
    app.state.search_engine = PostgresSearchService(settings, app.state.database, app.state.metadata)
    app.state.opencode = OpenCodeRunner(settings)
    try:
        app.state.agent_activity = OpenCodeActivityReader(settings.opencode_runtime_root)
    except Exception:  # noqa: BLE001
        # 活跃度是可选观测，runtime 配置异常不能阻止任务服务启动。
        app.state.agent_activity = None
    app.state.reverse_image = ReverseImageService(settings, app.state.database)
    # 后端只保存推理客户端和 scope-bound 查询服务；视觉模型本体位于独立 CPU 容器。
    app.state.visual_inference = VisualInferenceClient(settings)
    app.state.visual_search = VisualSearchService(settings, app.state.database)
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

    def visual_handler(payload: dict[str, object], progress):
        """在事务外推理、事务内写向量并幂等提交 Agent 任务。"""
        meme_id = payload.get("meme_id")
        expected_sha = payload.get("image_sha256")
        if not isinstance(meme_id, str) or not isinstance(expected_sha, str):
            raise RuntimeError("target_changed")
        try:
            _record, image = app.state.metadata.image_for_meme(meme_id)
            current_sha = app.state.metadata.image_sha256(image)
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
        agent_task_id: str | None = None
        with app.state.database.environment("local") as environment:
            meme = environment.memes.get(meme_id, for_update=True)
            if meme is None:
                raise RuntimeError("target_changed")
            try:
                latest_image = app.state.metadata.blob_store.resolve(meme.storage_key)
                latest_sha = app.state.metadata.image_sha256(latest_image)
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
            # 已有当前 SHA 的有效 Agent provenance 时不再次研究；否则向量和任务
            # 在同一个事务中写入，视觉任务只有在该事务提交后才会报告成功。
            provenance = dict(meme.provenance or {})
            summary = provenance.get("agent_context")
            if not environment.visual.agent_ready(meme) or not isinstance(summary, dict) or summary.get("model") != app.state.settings.opencode_model:
                # 视觉阶段的显式重试只重算向量；同一图片版本已有 Agent 任务时，
                # 无论其处于活动还是终态，都交给用户单独重试，避免自动复制研究。
                existing_agent = environment.tasks.context_task_for_target(meme.id, expected_sha)
                if existing_agent is not None:
                    agent_task_id = existing_agent.id
                else:
                    try:
                        skill_hash = app.state.opencode.skill_hash()
                    except (OSError, OpenCodeError):
                        skill_hash = None
                    agent_payload: dict[str, object] = {
                        "scope_id": "local",
                        "meme_id": str(meme.id),
                        "image_relative_path": app.state.resolver.relative(latest_image),
                        "image_sha256": expected_sha,
                        "auto_name": bool(payload.get("auto_name")),
                        "model": app.state.settings.opencode_model,
                        "skill_hash": skill_hash,
                        "settings_version": app.state.settings.settings_version,
                        "agent_concurrency": app.state.settings.opencode_concurrency,
                        "reverse_image_policy": str(payload.get("reverse_image_policy") or "forbid"),
                    }
                    if isinstance(payload.get("batch_id"), str) and payload["batch_id"]:
                        agent_payload["batch_id"] = payload["batch_id"]
                    agent_payload.update({"visual_model": identity.model, "visual_dimensions": identity.dimensions, "preprocess_version": identity.preprocess_version})
                    try:
                        agent_task = environment.tasks.submit(
                            task_type="meme_context_generation",
                            payload=agent_payload,
                            lane="agent",
                            dedupe_key=f"context:{meme.id}:{expected_sha}",
                            settings_version=app.state.settings.settings_version,
                            max_attempts=app.state.settings.worker_max_attempts,
                            lane_backpressure=app.state.settings.agent_backpressure,
                        )
                    except DatabaseError as exc:
                        raise RuntimeError(exc.code) from exc
                    if isinstance(payload.get("batch_id"), str) and payload["batch_id"]:
                        environment.tasks.add_batch_item(payload["batch_id"], agent_task.id)
                    agent_task_id = agent_task.id
        if agent_task_id:
            app.state.tasks.schedule(agent_task_id)
        if progress:
            progress(1.0, "视觉向量已保存，Agent 任务已幂等提交")
        return {"meme_id": meme_id, "visual_model": identity.model, "dimensions": identity.dimensions, "preprocess_version": identity.preprocess_version, "agent_task_id": agent_task_id}

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
        policy = str(payload.get("reverse_image_policy") or "forbid")
        payload["reverse_image_policy"] = policy if policy in {"forbid", "auto"} else "forbid"
        try:
            candidate, session_id = app.state.opencode.run(image, progress, task_id=str(payload.get("_claim_task_id") or ""), reverse_image_policy=str(payload.get("reverse_image_policy") or "forbid"))
        except TypeError as exc:
            # 兼容旧版 Runner 测试夹具/宿主适配器尚未接受策略关键字的签名。
            if "reverse_image_policy" not in str(exc):
                raise
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
            metadata = app.state.metadata.update_context(
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
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except MetadataError as exc:
            if exc.code == "claim_expired":
                raise RuntimeError("target_changed") from exc
            raise RuntimeError("agent_output_schema_invalid") from exc
        mark_invalidated = getattr(app.state.search_engine, "mark_cache_invalidated", None)
        if mark_invalidated:
            mark_invalidated(payload.get("batch_id"))
        else:
            app.state.search_engine.invalidate_cache()
        if not payload.get("batch_id") and not payload.get("batch_ids"):
            # 单图 Agent 没有批次封口，成功写回后直接复用唯一 cache generation。
            try:
                cache_task = tasks.submit("cache_generation", {})
                tasks.schedule(cache_task.task_id)
            except Exception:
                # 文本索引失败不改变 Agent 成功事实，用户可显式重试 cache 阶段。
                pass
        result: dict[str, object] = {
            "image_relative_path": relative,
            "meme_id": meme_id,
            "session_id": session_id,
            "result_artifact": f"task-results/{payload.get('_claim_task_id', '')}/result.json.tmp",
            "auto_named": False,
            "reverse_image_policy": payload["reverse_image_policy"],
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
    tasks.register("visual_embedding_generation", visual_handler)
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
        "status": "ok" if getattr(request.app.state, "search_engine", None) is not None else "degraded",
        "visual_available": bool(visual_status.get("available")),
        "storage_preflight": _storage_preflight_summary(getattr(request.app.state, "storage_preflight", None)),
    }


@app.get("/config", tags=["system"])
async def config_status(request: Request) -> dict[str, object]:
    """返回脱敏配置状态，绝不返回完整密钥。"""
    status = request.app.state.settings.status()
    # embedding 缓存属于运行时状态，供前端判断当前是否可以直接检索。
    engine = getattr(request.app.state, "search_engine", None)
    status["embedding_cache_ready"] = bool(engine and engine.has_cache())
    status["database_ready"] = True
    status["scope_id"] = "local"
    status["storage_preflight"] = _storage_preflight_summary(getattr(request.app.state, "storage_preflight", None))
    status["reverse_image_available"] = bool(getattr(getattr(request.app.state, "reverse_image", None), "available", False))
    visual_client = getattr(request.app.state, "visual_inference", None)
    if visual_client is not None:
        status["visual_available"] = bool(visual_client.health().get("available"))
    runtime = request.app.state.opencode.runtime_probe()
    # /config 只暴露固定标识和布尔探针，不返回宿主绝对路径、诊断原文或任何凭据。
    status["runtime_ready"] = bool(runtime.get("verified"))
    status["agent_runtime"] = {
        key: runtime[key]
        for key in (
            "mode",
            "container_name",
            "container_running",
            "image_root_match",
            "runtime_root_ready",
            "workspace_ready",
            "executable_ready",
            "skills_ready",
            "dependencies_ready",
            "mounts_ready",
            "non_root",
            "network_ready",
            "docker_socket_absent",
            "concurrency",
            "verified",
        )
        if key in runtime
    }
    return status


@app.post("/internal/reverse-image/search", tags=["internal"])
async def internal_reverse_image_search(
    request: Request,
    task_id: str = Form(..., min_length=1, max_length=255),
    image: UploadFile = File(...),
    request_id: str | None = Form(default=None, max_length=128),
    search_type: str = Form(default="all"),
    language: str = Form(default="zh-cn"),
    country: str | None = Form(default=None),
    query: str | None = Form(default=None),
    auto_crop: bool = Form(default=False),
    refresh: bool = Form(default=False),
) -> dict[str, object]:
    """无认证内部检索接口；策略、scope 和任务状态全部由服务端读取。"""
    content = await image.read()
    service: ReverseImageService = request.app.state.reverse_image
    try:
        return service.search(ReverseImageRequest(image=content, filename=image.filename or "image", task_id=task_id, request_id=request_id, search_type=search_type, language=language, country=country, query=query, auto_crop=auto_crop, refresh=refresh))
    except ReverseImageError as exc:
        raise _error(exc.status_code, exc.code, str(exc)) from exc
    except DatabaseError as exc:
        status = 404 if exc.code == "meme_not_found" else 409 if exc.code in {"usage_request_conflict", "usage_event_conflict"} else 503
        raise _error(status, exc.code, "反向图片请求无法完成") from exc


@app.post("/internal/visual-search/match", tags=["internal"])
async def internal_visual_search_match(request: Request, payload: VisualMatchRequest) -> dict[str, object]:
    """按运行中 Agent 任务推导 scope 和查询图片的本地视觉匹配接口。"""
    service: VisualSearchService = request.app.state.visual_search
    try:
        return service.match(task_id=payload.task_id, top_k=payload.top_k, exclude_self=payload.exclude_self)
    except VisualSearchError as exc:
        raise _error(exc.status_code, exc.code, str(exc)) from exc
    except DatabaseError as exc:
        status = 409 if exc.code in {"query_embedding_not_ready", "visual_model_identity_mismatch"} else 404 if exc.code in {"meme_not_found", "task_not_found"} else 503
        raise _error(status, exc.code, "视觉匹配无法完成") from exc


def _backend_settings_status(request: Request) -> dict[str, object]:
    """构造设置页脱敏状态，运行时探针仅返回布尔和固定标识。"""
    settings: Settings = request.app.state.settings
    runner: OpenCodeRunner = request.app.state.opencode
    engine = getattr(request.app.state, "search_engine", None)
    status = settings.backend_status(
        cache_ready=bool(engine and engine.has_cache()),
        runtime_ready=bool(runner.runtime_probe().get("verified")),
    )
    visual_client = getattr(request.app.state, "visual_inference", None)
    if visual_client is not None:
        status.setdefault("readonly", {})["visual_available"] = bool(visual_client.health().get("available"))
        status.setdefault("read_only", {})["visual_available"] = bool(visual_client.health().get("available"))
    return status


@app.get("/backend/settings", tags=["system"])
@app.get("/settings", tags=["system"], include_in_schema=False)
async def backend_settings(request: Request) -> dict[str, object]:
    """返回后端设置三类字段及当前/待重启配置。"""
    return _backend_settings_status(request)


def _authorize_settings(request: Request, token: str | None) -> None:
    """验证设置管理凭据；未启用或错误凭据统一返回 403。"""
    configured = request.app.state.settings.settings_admin_token
    if not configured or not token or not secrets.compare_digest(str(token), str(configured)):
        raise _error(403, "settings_forbidden", "后端设置管理未授权")


async def _update_backend_settings(request: Request, payload: ConcurrencyUpdateRequest, token: str | None) -> dict[str, object]:
    """授权后原子更新 dotenv 的并发字段，当前进程只返回待重启状态。"""
    _authorize_settings(request, token)
    settings: Settings = request.app.state.settings
    if os.environ.get("MEMEMEOW_OPENCODE_CONCURRENCY") is not None:
        raise _error(409, "settings_environment_override", "并发数量由进程环境变量覆盖，不能写入 .env")
    try:
        update_dotenv_concurrency(settings.dotenv_path, payload.opencode_concurrency)
    except ValueError as exc:
        raise _error(400, "settings_update_invalid", str(exc)) from exc
    except OSError as exc:
        raise _error(409, "settings_update_failed", "配置文件无法安全更新") from exc
    result = _backend_settings_status(request)
    result["saved"] = True
    result["restart_required"] = payload.opencode_concurrency != settings.opencode_concurrency
    result["pending"] = {"opencode_concurrency": payload.opencode_concurrency}
    return result


@app.patch("/backend/settings", tags=["system"])
@app.patch("/settings", tags=["system"], include_in_schema=False)
async def update_backend_settings(request: Request, payload: ConcurrencyUpdateRequest, x_settings_admin_token: str | None = Header(default=None), x_mememeow_settings_token: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> dict[str, object]:
    """受保护更新接口；只接受 Agent 并发数量。"""
    x_settings_admin_token = x_settings_admin_token or x_mememeow_settings_token
    if not x_settings_admin_token and authorization and authorization.lower().startswith("bearer "):
        x_settings_admin_token = authorization[7:].strip()
    return await _update_backend_settings(request, payload, x_settings_admin_token)


@app.post("/backend/settings", tags=["system"], include_in_schema=False)
@app.post("/backend/settings/concurrency", tags=["system"])
async def update_backend_concurrency(request: Request, payload: ConcurrencyUpdateRequest, x_settings_admin_token: str | None = Header(default=None), x_mememeow_settings_token: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> dict[str, object]:
    """兼容设置页显式并发路径的受保护更新接口。"""
    x_settings_admin_token = x_settings_admin_token or x_mememeow_settings_token
    if not x_settings_admin_token and authorization and authorization.lower().startswith("bearer "):
        x_settings_admin_token = authorization[7:].strip()
    return await _update_backend_settings(request, payload, x_settings_admin_token)


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


def _activity_payload(value: object) -> dict[str, object] | None:
    """将 reader 领域值收敛为完整的三个公开活跃度字段。"""
    if isinstance(value, AgentActivity):
        return value.as_dict()
    if not isinstance(value, Mapping):
        return None
    completed = value.get("agent_completed_turns", value.get("completed_turns"))
    running = value.get("agent_turn_running", value.get("turn_running"))
    last_activity = value.get("agent_last_activity_at", value.get("last_activity_at"))
    if isinstance(completed, bool) or not isinstance(completed, int) or completed < 0:
        return None
    if not isinstance(running, bool) or not isinstance(last_activity, str) or not last_activity:
        return None
    return {
        "agent_completed_turns": completed,
        "agent_turn_running": running,
        "agent_last_activity_at": last_activity,
    }


def _read_agent_activity(request: Request, records: list[TaskRecord]) -> dict[str, object]:
    """为当前任务页执行一次有界活跃度批量读取，失败时返回空映射。"""
    task_ids = [record.task_id for record in records if record.task_type == "meme_context_generation"]
    reader = getattr(request.app.state, "agent_activity", None)
    if not task_ids or reader is None:
        return {}
    read_many = getattr(reader, "read_many", None)
    if not callable(read_many):
        read_many = getattr(reader, "read", None)
    if not callable(read_many):
        return {}
    try:
        values = read_many(task_ids)
    except Exception:  # noqa: BLE001
        # SQLite 观测不能改变任务 API 的成功/失败语义。
        return {}
    if not isinstance(values, Mapping):
        return {}
    return {str(task_id): value for task_id, value in values.items() if isinstance(task_id, str)}


def _task_summary(request: Request, record: TaskRecord, activities: Mapping[str, object] | None = None) -> dict[str, object]:
    """将任务转换为安全摘要，并按需装配完整活跃度字段。"""
    if activities is None:
        activities = _read_agent_activity(request, [record])
    data = record.as_dict(include_payload=False)
    payload = record.payload
    if record.task_type == "meme_context_generation":
        # 只暴露可观察策略；完整 payload 仍留在后端数据库和 Worker 边界内。
        data["reverse_image_policy"] = str(payload.get("reverse_image_policy") or "forbid")
        activity = _activity_payload(activities.get(record.task_id))
        if activity is not None:
            data.update(activity)
    elif record.task_type == "visual_embedding_generation":
        data["visual"] = {
            "model": payload.get("visual_model"),
            "dimensions": payload.get("visual_dimensions"),
            "preprocess_version": payload.get("preprocess_version"),
        }
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
    activities = _read_agent_activity(request, records)
    return {"items": [_task_summary(request, record, activities) for record in records], "next_cursor": next_cursor}


@app.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(request: Request, task_id: str) -> dict[str, object]:
    """查询持久任务详情。"""
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise _error(404, "task_not_found", "任务不存在")
    return _task_summary(request, record)


@app.post("/tasks/{task_id}/cancel", tags=["tasks"])
async def cancel_task(request: Request, task_id: str) -> dict[str, object]:
    """取消单个未完成任务，不停止共享 Agent 容器或其他 session。"""
    record = request.app.state.tasks.get(task_id)
    if record is None:
        raise _error(404, "task_not_found", "任务不存在")
    if record.status in {"succeeded", "failed"}:
        return _task_summary(request, record)
    if not request.app.state.tasks.cancel(task_id):
        record = request.app.state.tasks.get(task_id)
        if record is None:
            raise _error(404, "task_not_found", "任务不存在")
    if record.task_type == "meme_context_generation":
        cancel = getattr(request.app.state.opencode, "cancel", None)
        if callable(cancel):
            cancel(task_id)
        else:
            # 兼容未升级的 runner 夹具；生产 Compose 路径始终使用 cancel() 的 executor 协议。
            request.app.state.opencode._terminate_container_session(task_id)
    current = request.app.state.tasks.get(task_id)
    return _task_summary(request, current or record)


@app.post("/tasks/{task_id}/retry", status_code=202, tags=["tasks"])
async def retry_task(request: Request, task_id: str) -> dict[str, object]:
    """只重试当前失败阶段；视觉/Agent/文本任务不会隐式级联。"""
    try:
        record = request.app.state.tasks.retry(task_id)
    except RuntimeError as exc:
        code = str(exc).split(":", 1)[0]
        if code == "task_not_found":
            raise _error(404, code, "任务不存在") from exc
        if code == "task_not_failed":
            raise _error(409, code, "只有失败任务可以重试") from exc
        if code == "agent_backpressure":
            raise _error(429, code, "Agent 等待队列已满，请稍后重试") from exc
        raise _error(409, code, "任务重试失败") from exc
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
    visual_identity = identity_from_settings(request.app.state.settings)
    for record in records:
        try:
            image = request.app.state.metadata.blob_store.resolve(record.storage_key)
            identity = request.app.state.metadata._identity(image)
        except (DatabaseError, MetadataError):
            continue
        metadata_status = request.app.state.metadata.status(image)
        with request.app.state.database.environment("local") as environment:
            visual_row = environment.visual.get(record.id, model=visual_identity.model, preprocess_version=visual_identity.preprocess_version, dimensions=visual_identity.dimensions, image_sha256=record.sha256)
        items.append({"meme_id": str(record.id), "filename": record.storage_key, "extension": record.extension, "size": identity["size_bytes"], "media_url": f"/media/{record.id}", "metadata": metadata_status, "embedding_status": "ready" if request.app.state.search_engine.has_cache() and metadata_status.get("status") in {"partial", "ready"} else "blocked" if metadata_status.get("status") == "repair_required" else "pending", "visual_embedding_status": "ready" if visual_row is not None else "pending"})
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
        return {"items": [_collection_payload(request, environment, row) for row in rows], "total": environment.collections.count(), "page": page, "page_size": page_size}


@app.post("/collections", status_code=201, tags=["collections"])
async def create_collection(request: Request, payload: CollectionRequest) -> dict[str, object]:
    """创建当前 local scope 的空合集。"""
    try:
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.create(payload.name)
            return _collection_payload(request, environment, row)
    except DatabaseError as exc:
        raise _collection_error(exc) from exc


@app.post("/collections/import", tags=["collections"])
async def import_collection(request: Request) -> dict[str, object]:
    """预检单个合集 ZIP 后创建新合集，并逐图片报告导入结果。"""
    form = await request.form()
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
        package = preflight_archive(await upload.read(), max_file_size=request.app.state.settings.max_upload_size, max_total_size=MAX_TOTAL_UNCOMPRESSED_BYTES)
    except CollectionPackageError as exc:
        raise _collection_package_error(exc) from exc
    collection_name = package.manifest.collection.name
    try:
        with request.app.state.database.environment("local") as environment:
            if environment.collections.by_name(collection_name) is not None:
                raise CollectionPackageError("collection_exists")
    except CollectionPackageError as exc:
        raise _collection_package_error(exc) from exc
    try:
        with request.app.state.database.environment("local") as environment:
            collection = environment.collections.create(collection_name)
            collection_id = collection.id
            existing_by_name: dict[str, object] = {}
            for record in environment.memes.list_all():
                valid = request.app.state.metadata.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes)
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
                with request.app.state.database.environment("local") as environment:
                    environment.collections.add_members(collection_id, [target_id])
                result.update({"ok": True, "status": "reused", "target_meme_id": target_id, "saved_filename": target.filename})
            else:
                target_id, target_path = request.app.state.metadata.upload_bytes(package_member.content, target_key=target.filename)
                target_id = str(target_id)
                created_count += 1
                with request.app.state.database.environment("local") as environment:
                    environment.collections.add_members(collection_id, [target_id])
                existing_by_name[target.filename] = {"meme": target_id, "sha256": member.sha256}
                result.update({"ok": True, "status": "imported", "target_meme_id": target_id, "saved_filename": target_path.name})
                try:
                    # 合集导入和普通上传共享视觉前置，Agent 只能由视觉任务成功路径创建。
                    task = _submit_visual_task(request, target_path, expected_sha256=member.sha256)
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
                    error = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
                    result["visual_task_error"] = error
                    result["metadata_job_error"] = error
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
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.get(collection_id)
            if row is None:
                raise DatabaseError("collection_not_found")
            members = []
            for item, meme in environment.collections.members(row.id, page=page, page_size=page_size):
                metadata_status = request.app.state.metadata.status(request.app.state.metadata.blob_store.resolve(meme.storage_key))
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
        with request.app.state.database.environment("local") as environment:
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
        archive_path = build_export_archive(collection_name, members, request.app.state.metadata.blob_store, temp_root=temp_dir, max_file_size=request.app.state.settings.max_upload_size, max_total_size=MAX_TOTAL_UNCOMPRESSED_BYTES)
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
        with request.app.state.database.environment("local") as environment:
            row = environment.collections.rename(collection_id, payload.name)
            return _collection_payload(request, environment, row)
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
    unknown = set(form.keys()) - {"auto_name", "files", "reverse_image_policy"}
    if unknown:
        raise _error(400, "invalid_request", "上传不接受已废弃的目标目录字段")
    auto_name = str(form.get("auto_name", "false")).lower() in {"1", "true", "yes", "on"}
    reverse_image_policy = str(form.get("reverse_image_policy", "forbid"))
    if reverse_image_policy not in {"forbid", "auto"}:
        raise _error(400, "invalid_reverse_image_policy", "反向图片策略只能是 forbid 或 auto")
    if reverse_image_policy == "auto" and not request.app.state.reverse_image.available:
        raise _error(503, "reverse_image_unavailable", "反向图片服务尚未配置")
    files = [item for item in form.getlist("files") if hasattr(item, "filename") and hasattr(item, "read")]
    if not files:
        raise _error(400, "files_required", "必须上传图片文件")
    resolver: PathResolver = request.app.state.resolver
    settings: Settings = request.app.state.settings
    results = []
    upload_batch_id = uuid4().hex if len(files) > 1 else None
    upload_task_ids: list[str] = []
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
            task = _submit_visual_task(request, target, batch_id=upload_batch_id, auto_name=auto_name, reverse_image_policy=reverse_image_policy, schedule=upload_batch_id is None)
            result["visual_task_id"] = task.task_id
            result["visual_job_id"] = task.task_id
            # 兼容旧前端字段；其语义现在表示视觉阶段任务。
            result["metadata_job_id"] = task.task_id
            result["visual_task_status"] = task.status
            if upload_batch_id:
                upload_task_ids.append(task.task_id)
        except (OSError, RuntimeError, MetadataError) as exc:
            # 图片和 pending Meme 记录已有效提交，视觉任务失败可由显式阶段重试恢复。
            result["visual_task_error"] = _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"
            result["metadata_job_error"] = result["visual_task_error"]
        result["metadata_status"] = request.app.state.metadata.status(target)["status"]
        _invalidate_search(request)
        results.append(result)
    if upload_batch_id and upload_task_ids:
        request.app.state.tasks.seal_batch(upload_batch_id)
        for task_id in upload_task_ids:
            request.app.state.tasks.schedule(task_id)
    return {"batch_id": upload_batch_id, "results": results}


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
        task = _submit_context_task(request, image, expected_sha256=getattr(locals().get("record", None), "sha256", None), reverse_image_policy=payload.reverse_image_policy)
    except MetadataError as exc:
        # 任务提交必须先记录目标指纹；文件在请求和排队之间消失时仍返回稳定 404。
        if exc.code in {"image_unreadable", "metadata_image_mismatch", "metadata_missing"}:
            raise _error(404, "meme_not_found", "图片不存在") from exc
        raise _error(409, exc.code, "图片当前不可处理") from exc
    except RuntimeError as exc:
        if str(exc) == "agent_backpressure":
            raise _error(429, "agent_backpressure", "Agent 等待队列已满，请稍后重试") from exc
        if str(exc) == "generation_policy_conflict":
            raise _error(409, "generation_policy_conflict", "同一图片已有不同反向图片策略的活动任务") from exc
        if str(exc) == "reverse_image_unavailable":
            raise _error(503, "reverse_image_unavailable", "反向图片服务尚未配置") from exc
        if str(exc) == "invalid_reverse_image_policy":
            raise _error(400, "invalid_reverse_image_policy", "反向图片策略只能是 forbid 或 auto") from exc
        if str(exc) == "agent_runtime_unavailable":
            raise _error(503, "agent_runtime_unavailable", "共享 Agent 运行时不可用，请先启动容器") from exc
        if str(exc) == "agent_image_root_mismatch":
            raise _error(503, "agent_image_root_mismatch", "Agent 图片根目录配置与容器挂载不一致") from exc
        if str(exc) == "generation_policy_conflict":
            raise _error(409, "generation_policy_conflict", "同一图片已有不同反向图片策略的活动任务") from exc
        if str(exc) == "reverse_image_unavailable":
            raise _error(503, "reverse_image_unavailable", "反向图片服务尚未配置") from exc
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
            task = _submit_context_task(request, image, batch_id=batch_id, expected_sha256=record.sha256, reverse_image_policy=payload.reverse_image_policy, schedule=False)
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


@app.post("/images/visual-embedding", status_code=202, tags=["images", "tasks"])
async def generate_visual_embedding(request: Request, payload: ContextRequest) -> dict[str, object]:
    """为既有图片显式提交或复用当前视觉模型的向量任务。"""
    if not payload.meme_id:
        raise _error(400, "meme_id_required", "必须提供 meme_id")
    try:
        record, image = request.app.state.metadata.image_for_meme(payload.meme_id)
    except MetadataError as exc:
        code = "meme_not_found" if exc.code in {"metadata_missing", "image_unreadable"} else exc.code
        raise _error(404 if code == "meme_not_found" else 409, code, "图片不存在或内容已变化") from exc
    try:
        task = _submit_visual_task(request, image, expected_sha256=record.sha256)
    except (MetadataError, RuntimeError) as exc:
        raise _error(409, _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed", "视觉任务提交失败") from exc
    return {"task_id": task.task_id, "task_type": task.task_type, "status": task.status}


@app.post("/images/visual-embedding/batch", status_code=202, tags=["images", "tasks"])
async def generate_visual_embedding_batch(request: Request, payload: ContextBatchRequest) -> dict[str, object]:
    """为既有图片提交可并发交错的视觉回填批次。"""
    batch_id = uuid4().hex
    task_ids: list[str] = []
    results: list[dict[str, object]] = []
    for item in payload.items:
        if not item.meme_id:
            results.append({"meme_id": None, "error": "meme_id_required"})
            continue
        try:
            record, image = request.app.state.metadata.image_for_meme(item.meme_id)
            task = _submit_visual_task(request, image, batch_id=batch_id, expected_sha256=record.sha256, schedule=False)
            task_ids.append(task.task_id)
            results.append({"meme_id": item.meme_id, "task_id": task.task_id, "status": task.status})
        except (MetadataError, RuntimeError) as exc:
            results.append({"meme_id": item.meme_id, "error": _context_enqueue_error(exc) if isinstance(exc, RuntimeError) else "visual_enqueue_failed"})
    if task_ids:
        request.app.state.tasks.seal_batch(batch_id)
        for task_id in task_ids:
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
