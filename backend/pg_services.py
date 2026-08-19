"""PostgreSQL 版元数据、文件生命周期和检索服务适配器。

领域 schema 与旧服务共用 ``MemeContext``/``SidecarMetadata`` 类型，但运行时不读写
sidecar 文件；数据库记录是唯一结构化事实，BlobStore 只保存图片字节。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import UUID, uuid4

import numpy as np
from openai import OpenAI
from sqlalchemy import select
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import threading

from backend.database import (
    EMBEDDING_DIMENSIONS,
    BlobStore,
    DataEnvironment,
    DatabaseError,
    DatabaseResources,
    ImageProcessingAttempt,
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    ScopeContext,
    SearchGeneration,
    StorageCoordinator,
    StorageOperation,
    Task,
    TaskBatch,
    TaskLaneSlot,
    UnitOfWork,
    utcnow,
)
from backend.agent_resume import (
    append_error_history,
    append_task_error_history,
    agent_failure_requires_unknown,
    bounded_backoff,
    classify_resume_error,
    normalize_config_hash,
    normalize_identifier,
    sanitize_error,
    sanitize_error_history,
    within_total_timeout,
)
from backend.metadata import (
    CONTEXT_STATUSES,
    MAX_SEMANTIC_DOCUMENT_LENGTH,
    SCHEMA_VERSION,
    EMBEDDING_FIELDS,
    ImageIdentity,
    MemeContext,
    MetadataError,
    Provenance,
    SidecarMetadata,
    semantic_document,
)
from backend.operation_policy import GrantAssociationStore, OperationPolicyError, OperationPolicyGateway, Operations
from backend.opencode_workspace import SELECTOR_RE
from backend.paths import SUPPORTED_EXTENSIONS
from backend.tasks import IMAGE_PROCESSING_TASK_TYPES, TaskRecord, TERMINAL, STABLE_TASK_ERRORS
from backend.scope import validate_scope_services
from backend.visual import VisualEmbeddingError, VisualInferenceClient, identity_from_settings


logger = logging.getLogger(__name__)
# 任务 payload 只承载业务输入；范围事实始终来自持久 Task.scope_id。
UNTRUSTED_SCOPE_FIELDS = frozenset({"scope_id", "scope-id", "user_id", "user-id"})


def _iso(value: datetime | str | None) -> str:
    """将数据库时间转换为旧领域模型接受的 UTC ISO 字符串。"""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat() if isinstance(value, datetime) else str(value)


class PostgresMetadataService:
    """以 scope-bound repository 管理 Meme 元数据和图片指纹。

    ``scope_id`` 的 local 默认值只为开源旧夹具保留；应用运行时由
    ``ScopeServiceFactory`` 显式传入 scope。
    """

    def __init__(self, resources: DatabaseResources, *, scope_id: str | ScopeContext = "local"):
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(self.scope.scope_id)
        self.root = self.blob_store.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage = StorageCoordinator(resources, scope_id=self.scope)

    def image_sha256(self, image: Path) -> str:
        """计算受控图片 SHA-256；不可读文件转换为稳定 MetadataError。"""
        digest = hashlib.sha256()
        try:
            with image.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise MetadataError("image_unreadable") from exc
        return digest.hexdigest()

    def _relative(self, image: Path) -> str:
        """将图片解析为当前 scope 内的 POSIX storage_key。"""
        try:
            return self.blob_store.relative(image)
        except (ValueError, OSError) as exc:
            raise MetadataError("path_forbidden") from exc

    def _identity(self, image: Path) -> dict[str, object]:
        """读取图片路径、扩展名、大小和 SHA 指纹。"""
        try:
            stat = image.stat()
        except OSError as exc:
            raise MetadataError("image_unreadable") from exc
        return {"relative_path": self._relative(image), "extension": image.suffix.lower(), "size_bytes": stat.st_size, "sha256": self.image_sha256(image)}

    @staticmethod
    def _base_context() -> dict[str, object]:
        """创建尚未研究图片的最小语境。"""
        return {"title": None, "summary": "", "subjects": [], "visible_text": [], "references": [], "meaning": None, "keywords": [], "search_queries": [], "uncertainties": ["尚未完成图片语境研究"], "source_urls": []}

    def _to_sidecar(self, record: Meme, image: Path | None = None) -> SidecarMetadata:
        """将数据库 Meme 映射为稳定领域对象，供 API/Agent 复用。"""
        context = MemeContext.model_validate(record.meme_context or {})
        provenance = Provenance.model_validate(record.provenance or {})
        payload: dict[str, object] = {
            "schema_version": record.metadata_schema_version,
            "image": {"relative_path": record.storage_key, "extension": record.extension, "size_bytes": record.size_bytes, "sha256": record.sha256},
            "context_status": record.context_status,
            "meme_context": context.model_dump(mode="json", exclude_none=False),
            "provenance": provenance.model_dump(mode="json", exclude_none=False),
        }
        payload.update(record.extensions or {})
        if image is not None:
            identity = self._identity(image)
            payload["image"] = identity
        return SidecarMetadata.model_validate(payload)

    def _record(self, image: Path, *, for_update: bool = False) -> Meme:
        """按 storage_key 获取当前 scope Meme 并校验图片实际存在。"""
        key = self._relative(image)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key, for_update=for_update)
            if record is None:
                raise MetadataError("metadata_missing")
            return record

    def load(self, image: Path) -> SidecarMetadata:
        """读取数据库元数据并严格校验当前图片指纹。"""
        key = self._relative(image)
        try:
            identity = self._identity(image)
        except MetadataError:
            raise
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key)
            if record is None:
                raise MetadataError("metadata_missing")
            if record.extension != identity["extension"] or record.size_bytes != identity["size_bytes"] or record.sha256 != identity["sha256"]:
                record.context_status = "repair_required"
                record.updated_at = utcnow()
                environment.uow.session.flush()
                environment.uow.session.commit()
                raise MetadataError("metadata_image_mismatch")
            try:
                return self._to_sidecar(record)
            except Exception as exc:  # noqa: BLE001
                record.context_status = "repair_required"
                raise MetadataError("metadata_invalid") from exc

    def status(self, image: Path) -> dict[str, object]:
        """返回图片库安全状态摘要；失效指纹始终显示为 repair_required。"""
        try:
            metadata = self.load(image)
        except MetadataError as exc:
            return {"status": "repair_required", "error": exc.code}
        context = metadata.meme_context
        return {"status": metadata.context_status, "title": context.title, "summary": context.summary, "subjects": context.subjects, "meaning": context.meaning, "keywords": context.keywords}

    def meme_id_for_image(self, image: Path) -> UUID:
        """读取当前 scope 图片对应的稳定 meme_id。"""
        key = self._relative(image)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key)
            if record is None:
                raise MetadataError("metadata_missing")
            return record.id

    def image_for_meme(self, meme_id: UUID | str) -> tuple[Meme, Path]:
        """按当前 scope meme_id 返回数据库记录及经过 BlobStore 校验的图片路径。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.get(meme_id)
            if record is None:
                raise MetadataError("metadata_missing")
            try:
                image = self.blob_store.resolve(record.storage_key)
            except DatabaseError as exc:
                raise MetadataError(exc.code) from exc
            identity = self._identity(image)
            if record.sha256 != identity["sha256"] or record.size_bytes != identity["size_bytes"]:
                raise MetadataError("metadata_image_mismatch")
            return record, image

    def find_existing_upload(self, target_key: str, *, sha256: str, size_bytes: int) -> tuple[Meme, Path] | None:
        """按当前 scope 验证可幂等认领的 durable 上传事实。

        返回值为数据库 Meme 与受控图片路径；目标不存在时返回 ``None``。只要目标
        文件或数据库记录存在但任一指纹不一致，就返回 reconciliation 错误，避免
        把孤立文件或损坏记录误认成上传成功。
        """
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(target_key)
        try:
            image = self.blob_store.resolve(target_key, must_exist=True)
        except DatabaseError as exc:
            if record is None and exc.code == "file_not_found":
                return None
            raise MetadataError("upload_reconciliation_required") from exc
        if record is None:
            raise MetadataError("upload_reconciliation_required")
        try:
            identity = self._identity(image)
        except MetadataError as exc:
            raise MetadataError("upload_reconciliation_required") from exc
        # 先验证数据库与存储彼此一致；两者一致但与本次上传不同只是正常同名冲突，
        # 不应被误报为 reconciliation。只有 durable 事实自身不一致才进入修复路径。
        if record.size_bytes != identity["size_bytes"] or record.sha256.lower() != str(identity["sha256"]).lower():
            raise MetadataError("upload_reconciliation_required")
        if record.size_bytes != size_bytes or record.sha256.lower() != sha256.lower():
            raise MetadataError("file_exists")
        return record, image

    def create_pending(self, image: Path, *, status: str = "pending", meme_id: UUID | None = None) -> SidecarMetadata:
        """为合法图片幂等创建数据库 Meme 和 pending 语境。"""
        if status not in CONTEXT_STATUSES:
            raise MetadataError("invalid_context_status")
        identity = self._identity(image)
        key = str(identity["relative_path"])
        with self.resources.environment(self.scope.scope_id) as environment:
            existing = environment.memes.by_storage_key(key)
            if existing:
                return self._to_sidecar(existing)
            record = environment.memes.create(storage_key=key, extension=str(identity["extension"]), size_bytes=int(identity["size_bytes"]), sha256=str(identity["sha256"]), context=self._base_context(), provenance={"producer": "system", "model": None, "updated_at": datetime.now(timezone.utc).isoformat(), "field_sources": {}, "last_error": None}, status=status, meme_id=meme_id)
            return self._to_sidecar(record)

    def upload_bytes(self, content: bytes, *, target_key: str) -> tuple[UUID, Path]:
        """通过 StorageCoordinator 暂存并提交上传，返回稳定 ID 与受控文件路径。"""
        suffix = Path(target_key).suffix.lower()
        try:
            record = self.storage.upload(content, target_key=target_key, extension=suffix, context=self._base_context(), provenance={"producer": "system", "model": None, "updated_at": datetime.now(timezone.utc).isoformat(), "field_sources": {}, "last_error": None})
        except DatabaseError as exc:
            raise MetadataError(exc.code) from exc
        return record.id, self.blob_store.resolve(record.storage_key)

    def _merge_payload(self, current: SidecarMetadata, context_updates: dict[str, object], *, producer: str, model: str | None, status: str, error: str | None, agent_context: dict[str, object] | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """合并语境并应用人工字段保护，同时保留未知扩展字段。"""
        context = current.meme_context.model_dump(mode="json", exclude_none=False)
        sources = dict(current.provenance.field_sources or {})
        for field, value in context_updates.items():
            if field not in MemeContext.model_fields:
                continue
            if sources.get(field) == "human" and producer != "human":
                continue
            context[field] = value
            sources[field] = producer
        provenance = current.provenance.model_dump(mode="json", exclude_none=False)
        provenance.update({"producer": producer, "model": model, "updated_at": datetime.now(timezone.utc).isoformat(), "field_sources": sources, "last_error": error})
        if agent_context is not None and producer == "research":
            # 该摘要由后端根据 claim/SHA 生成，Agent 输出不能自行伪造候选资格。
            provenance["agent_context"] = dict(agent_context)
        known = {"schema_version", "image", "context_status", "meme_context", "provenance"}
        extensions = {key: value for key, value in current.model_dump(mode="json", exclude_none=False).items() if key not in known}
        return context, provenance, extensions

    def update_context(self, image: Path, context_updates: dict[str, object], *, producer: str, model: str | None = None, status: str = "partial", error: str | None = None, expected_revision: int | None = None, expected_sha256: str | None = None, claim: tuple[str, int, str] | None = None, agent_context: dict[str, object] | None = None) -> SidecarMetadata:
        """在一个事务中更新数据库语境，并用 revision/SHA 防止过期任务覆盖。"""
        key = self._relative(image)
        identity = self._identity(image)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key, for_update=True)
            if record is None:
                record = environment.memes.create(storage_key=key, extension=str(identity["extension"]), size_bytes=int(identity["size_bytes"]), sha256=str(identity["sha256"]), context=self._base_context(), provenance={"producer": "system", "updated_at": datetime.now(timezone.utc).isoformat(), "field_sources": {}}, status="pending")
            current = self._to_sidecar(record)
            context, provenance, extensions = self._merge_payload(current, context_updates, producer=producer, model=model, status=status, error=error, agent_context=agent_context)
            try:
                record = environment.memes.update_context(record.id, context=context, provenance=provenance, status=status if status in CONTEXT_STATUSES else "partial", expected_revision=expected_revision, expected_sha256=expected_sha256, claim=claim)
            except DatabaseError as exc:
                raise MetadataError(exc.code) from exc
            record.extensions = extensions
            return self._to_sidecar(record)

    def record_error(self, image: Path, *, producer: str, model: str | None, error: str) -> SidecarMetadata:
        """记录失败诊断但保留最近一次有效语境。"""
        key = self._relative(image)
        identity = self._identity(image)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key, for_update=True)
            if record is None:
                raise MetadataError("metadata_missing")
            # 失败诊断不是语境写回：只更新 provenance，保留同一事务开始时锁定的最新上下文和状态。
            current_provenance = dict(record.provenance or {})
            field_sources = dict(current_provenance.get("field_sources") or {})
            current_provenance.update(
                {
                    "producer": producer,
                    "model": model,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "field_sources": field_sources,
                    "last_error": error,
                }
            )
            record.provenance = current_provenance
            record.revision += 1
            record.updated_at = utcnow()
            environment.uow.session.flush()
            return self._to_sidecar(record)

    def update_reverse_image_provenance(self, meme_id: str | UUID, audit: dict[str, Any]) -> None:
        """把后端 usage event 汇总写入 Meme provenance，拒绝 Agent 自报覆盖。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.get(meme_id, for_update=True)
            if record is None:
                raise MetadataError("metadata_missing")
            provenance = dict(record.provenance or {})
            provenance["reverse_image"] = dict(audit)
            record.provenance = provenance
            record.updated_at = utcnow()
            environment.uow.session.flush()

    def rename(self, source: Path, target: Path) -> SidecarMetadata:
        """按稳定 Meme 身份更新数据库路径；文件移动由 API 的操作协调器负责。"""
        source_key = self._relative(source)
        target_key = self._relative(target)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(source_key, for_update=True)
            if record is None:
                raise MetadataError("metadata_missing")
            try:
                record = environment.memes.rename(record.id, target_key)
            except DatabaseError as exc:
                raise MetadataError(exc.code) from exc
            return self._to_sidecar(record)

    def rename_by_id(self, meme_id: UUID | str, target: Path) -> SidecarMetadata:
        """按稳定 meme_id 记录并完成文件移动，保持资源身份不变。"""
        target_key = self._relative(target)
        try:
            record = self.storage.rename(meme_id, target_key=target_key)
        except DatabaseError as exc:
            raise MetadataError(exc.code) from exc
        return self._to_sidecar(record)

    def remove(self, image: Path) -> None:
        """删除数据库 Meme；文件隔离由调用方先行完成并可恢复。"""
        key = self._relative(image)
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.memes.by_storage_key(key, for_update=True)
            if record is None:
                raise MetadataError("metadata_missing")
            environment.memes.delete(record.id)

    def remove_by_id(self, meme_id: UUID | str) -> None:
        """按稳定 meme_id 隔离文件、删除数据库记录并清理隔离区。"""
        try:
            self.storage.delete(meme_id)
        except DatabaseError as exc:
            raise MetadataError(exc.code) from exc

    def recover_storage(self, *, limit: int = 100) -> dict[str, int]:
        """恢复当前 scope 的未完成跨存储操作。"""
        try:
            return self.storage.recover(limit=limit)
        except DatabaseError as exc:
            raise MetadataError(exc.code) from exc

    def integrity_scan(self) -> dict[str, Any]:
        """报告孤立文件、缺失对象、指纹冲突和活动操作。"""
        return self.storage.integrity_scan()

    def embedding_record(self, image: Path) -> dict[str, object]:
        """返回数据库语境驱动的 embedding 资格和内容指纹。"""
        image_sha = self.image_sha256(image)
        try:
            metadata = self.load(image)
        except MetadataError as exc:
            return {"text": "", "indexable": False, "skip_reason": "metadata_invalid", "status": "repair_required", "metadata_schema_version": None, "metadata_hash": None, "image_sha256": image_sha, "error": exc.code}
        context = metadata.meme_context
        text = semantic_document(context) if metadata.context_status in {"partial", "ready"} else ""
        serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        reason = None if metadata.context_status in {"partial", "ready"} and text else ("metadata_pending" if metadata.context_status == "pending" else "semantic_text_empty" if metadata.context_status in {"partial", "ready"} else "metadata_unavailable")
        return {"text": text, "indexable": reason is None, "skip_reason": reason, "status": metadata.context_status, "metadata_schema_version": metadata.schema_version, "metadata_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(), "image_sha256": image_sha}

    def repair(self, progress: Any | None = None) -> dict[str, object]:
        """执行数据库与文件双向完整性扫描，不把孤立文件隐式导入业务列表。"""
        if progress:
            progress(0.25, "正在扫描数据库图片记录与文件指纹")
        report = self.integrity_scan()
        # 完整性扫描直接以数据库 Meme 为权威；孤立图片只报告，不自动导入。
        report.setdefault("orphan_files", [])
        report["processed"] = len(report.get("missing_files", [])) + len(report.get("mismatched", []))
        report["created"] = 0
        report["repair_required"] = len(report.get("missing_files", [])) + len(report.get("mismatched", []))
        if progress:
            progress(1.0, "图片完整性扫描完成")
        return report


class PostgresSearchService:
    """基于 search_generations/meme_embeddings 的 pgvector 检索服务。

    直接构造时的 local 默认值仅用于开源兼容夹具；生产请求通过 scope facade 获取。
    """

    def __init__(self, settings: Any, resources: DatabaseResources, metadata: PostgresMetadataService, *, scope_id: str | ScopeContext = "local"):
        self.settings, self.resources, self.metadata = settings, resources, metadata
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.model = settings.embedding_model
        self._generation_lock = __import__("threading").Lock()

    def _client(self) -> OpenAI:
        """创建嵌入客户端，缺少配置时返回稳定错误。"""
        if not self.settings.embedding_api_key or not self.settings.embedding_base_url:
            raise RuntimeError("embedding_not_configured")
        return OpenAI(api_key=self.settings.embedding_api_key, base_url=self.settings.embedding_base_url)

    def _embedding(self, text: str) -> list[float]:
        """调用模型并拒绝非 1024 维或零向量响应。"""
        response = self._client().embeddings.create(model=self.model, input=text, encoding_format="float")
        vector = np.asarray(response.data[0].embedding, dtype=float)
        if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.linalg.norm(vector):
            raise RuntimeError("embedding_dimensions_mismatch")
        return (vector / np.linalg.norm(vector)).tolist()

    def has_cache(self) -> bool:
        """检查当前 scope/model 的唯一检索来源是否已有有效数据。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            if environment.search.source_mode(self.model) == "incremental":
                return environment.search.has_incremental(self.model)
            # active generation 只说明控制面存在一代索引，不能证明其中仍有
            # 与当前 Meme SHA、语境 hash、维度和文件身份一致的可检索条目。
            return environment.search.has_legacy(self.model)

    def invalidate_cache(self) -> None:
        """数据库索引不在进程内缓存；此方法保留兼容调用但无副作用。"""

    def mark_cache_invalidated(self, batch_id: object = None) -> None:
        """兼容旧批次回调，generation 由独立任务显式刷新。"""

    def generate_cache(self, progress: Callable[[float | None, str | None], None], claim: tuple[str, int, str] | None = None) -> dict[str, object]:
        """短事务固化源快照，在事务外调用 embedding，完成后原子激活。"""
        with self._generation_lock:
            candidates: list[tuple[UUID, int, str, str, str]] = []
            skipped = 0
            with self.resources.environment(self.scope.scope_id) as environment:
                # 源集合必须在同一个短 REPEATABLE READ 事务中固化，避免刷新期间
                # 读取到跨事务的 Meme revision/语境组合。
                environment.uow.session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
                environment.search.assert_claim(claim)
                # 同一 scope/model 的 building generation 可能来自上次 Worker 崩溃；
                # 新 claim 先将其隔离，再创建本次不可变 generation。
                environment.search.abandon_building(self.model, claim=claim)
                for meme in environment.memes.list_all():
                    try:
                        image = self.metadata.blob_store.resolve(meme.storage_key)
                        identity = self.metadata._identity(image)
                        context = MemeContext.model_validate(meme.meme_context or {})
                    except (DatabaseError, MetadataError, ValueError):
                        skipped += 1
                        continue
                    if identity["sha256"] != meme.sha256 or identity["size_bytes"] != meme.size_bytes or meme.context_status not in {"partial", "ready"}:
                        skipped += 1
                        continue
                    text_value = semantic_document(context)
                    if not text_value:
                        skipped += 1
                        continue
                    serialized = json.dumps(self.metadata._to_sidecar(meme).model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    metadata_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
                    candidates.append((meme.id, meme.revision, meme.sha256, text_value, metadata_hash))
                if not candidates:
                    raise RuntimeError("no_indexable_images")
                candidates.sort(key=lambda item: str(item[0]))
                source = [(str(meme_id), revision, image_sha, metadata_hash, hashlib.sha256(text.encode()).hexdigest()) for meme_id, revision, image_sha, text, metadata_hash in candidates]
                source_hash = hashlib.sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                generation = environment.search.create_generation(self.model, source_hash)
                generation_id = generation.id
                for meme_id, revision, image_sha, text_value, metadata_hash in candidates:
                    environment.search.assert_claim(claim)
                    environment.search.add_snapshot_item(generation_id, meme_id=meme_id, meme_revision=revision, image_sha256=image_sha, semantic_document=text_value[:MAX_SEMANTIC_DOCUMENT_LENGTH], metadata_hash=metadata_hash)
            total = len(candidates)
            try:
                for index, (meme_id, _revision, _image_sha, text_value, _metadata_hash) in enumerate(candidates, start=1):
                    vector = self._embedding(text_value)
                    with self.resources.environment(self.scope.scope_id) as environment:
                        environment.search.set_item_embedding(generation_id, meme_id, vector, claim=claim)
                    if progress:
                        progress(index / total, f"正在生成 pgvector 索引 {index}/{total}")
                with self.resources.environment(self.scope.scope_id) as environment:
                    environment.search.activate(generation, expected_source_hash=source_hash, expected_items=source, claim=claim)
                return {"indexed_count": total, "skipped_count": skipped, "generation_id": str(generation_id), "model": self.model, "dimensions": EMBEDDING_DIMENSIONS}
            except Exception as exc:
                with self.resources.environment(self.scope.scope_id) as environment:
                    try:
                        environment.search.fail_generation(generation_id, error=str(exc)[:500], claim=claim)
                    except DatabaseError:
                        # 租约已被新 Worker 接管时，旧 Worker 不得修改 generation 状态。
                        pass
                raise

    def _enhance_query(self, query: str) -> str:
        """使用可选 LLM 改写查询；失败由 API 回退普通查询。"""
        if not self.settings.llm_enhance_model:
            raise RuntimeError("llm_enhance_not_configured")
        response = self._client().chat.completions.create(model=self.settings.llm_enhance_model, messages=[{"role": "user", "content": f"将下面内容改写为一句适合检索表情包的简短描述，只输出描述：{query}"}], temperature=0.2)
        value = response.choices[0].message.content
        if not value or not value.strip():
            raise RuntimeError("llm_enhance_invalid")
        return value.strip()

    def search(self, query: str, top_k: int = 5, api_key: str | None = None, use_llm: bool = False) -> list[str]:
        """按 meme_id 稳定排序查询并返回当前 scope 的稳定资源标识。"""
        if use_llm:
            try:
                query = self._enhance_query(query)
            except Exception:  # noqa: BLE001
                pass
        vector = self._embedding(query)
        with self.resources.environment(self.scope.scope_id) as environment:
            if environment.search.source_mode(self.model) == "incremental":
                ranked = environment.search.query_incremental(self.model, vector, top_k)
            else:
                ranked = environment.search.query(self.model, vector, top_k)
            result: list[str] = []
            for meme_id, _score in ranked:
                try:
                    _record, image = self.metadata.image_for_meme(meme_id)
                except MetadataError:
                    continue
                value = str(meme_id)
                if value not in result:
                    result.append(value)
                if len(result) >= top_k:
                    break
            return result


class PostgresTaskWorkerManager:
    """进程级任务协调器，统一管理线程池、处理器注册和任务恢复扫描。

    任务的数据库操作仍由 scope-bound ``PostgresTaskService`` 执行；协调器只按任务
    ID 调度工作，并在真正认领后根据持久 ``Task.scope_id`` 创建轻量服务视图。这样
    历史 scope 数量不会复制 Worker、线程池、handler registry 或全局 Agent lane。
    """

    def __init__(
        self,
        resources: DatabaseResources,
        *,
        agent_concurrency: int = 1,
        agent_backpressure: int = 32,
        settings_version: str | None = None,
        lease_seconds: int = 120,
        max_attempts: int = 3,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.resources = resources
        self.agent_concurrency = max(1, min(int(agent_concurrency), 8))
        self.agent_backpressure = max(1, min(int(agent_backpressure), 500))
        self.settings_version = settings_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._executor = executor or ThreadPoolExecutor(max_workers=max(2, self.agent_concurrency + 1), thread_name_prefix="mememeow-scope-worker")
        self._owns_executor = executor is None
        self._service_resolver: Callable[[str], Any] | None = None
        self._scope_service_resolver: Callable[[str | ScopeContext], Any] | None = None
        self._handlers: dict[str, Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]] = {}
        self._lock = Lock()
        self._stopped = Event()
        self._started = False
        self._scheduled: set[str] = set()
        self.owner = f"worker-{os.getpid()}-{id(self)}"

    @property
    def worker_count(self) -> int:
        """返回当前进程协调器数量；一个 manager 代表一个 Worker 控制面。"""
        return 0 if self._stopped.is_set() else 1

    @property
    def executor(self) -> ThreadPoolExecutor:
        """返回共享线程池，供 scope facade 复用而不自行创建调度资源。"""
        return self._executor

    def set_service_resolvers(self, task_resolver: Callable[[str], Any], scope_resolver: Callable[[str | ScopeContext], Any]) -> None:
        """安装按持久任务或显式 scope 创建轻量服务视图的回调。"""
        self._service_resolver = task_resolver
        self._scope_service_resolver = scope_resolver

    def register(self, task_type: str, handler: Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]) -> None:
        """在进程级 registry 注册一个任务处理器，所有 scope 共用该注册表。"""
        self._handlers[task_type] = handler

    def handler(self, task_type: str) -> Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any] | None:
        """读取当前任务类型的全局处理器。"""
        return self._handlers.get(task_type)

    def start(self) -> dict[str, list[str]]:
        """执行一次全局租约恢复、批次恢复和 queued 任务扫描。"""
        with self._lock:
            if self._started:
                return {"started": [], "invalid_tasks": []}
            self._started = True
        try:
            queued = self._recover_expired()
            invalid = self._fail_invalid_scope_tasks()
            queued.extend(self._recover_pending_batches())
            with self.resources.factory() as session:
                queued.extend(
                    session.scalars(
                        select(Task.id).where(
                            Task.status == "queued",
                            ~Task.task_type.in_(IMAGE_PROCESSING_TASK_TYPES),
                        )
                    )
                )
            for task_id in dict.fromkeys(queued):
                self.schedule(task_id)
            return {"started": [self.owner], "invalid_tasks": sorted(set(invalid))}
        except Exception:
            with self._lock:
                self._started = False
            raise

    def _recover_expired(self) -> list[str]:
        """跨所有 scope 恢复过期 claim，并释放旧 lane 槽位。"""
        now = utcnow()
        queued: list[str] = []
        with self.resources.factory() as session:
            rows = list(
                session.scalars(
                    select(Task)
                    .where(
                        Task.status == "running",
                        Task.lease_expires_at < now,
                        ~Task.task_type.in_(IMAGE_PROCESSING_TASK_TYPES),
                    )
                    .with_for_update(skip_locked=True)
                    .limit(5000)
                )
            )
            for task in rows:
                recovery_error = {"error": "lease_expired", "message": "Worker 租约已过期"}
                append_task_error_history(
                    task,
                    recovery_error,
                    attempt=task.attempt_count,
                    executor_attempt_id=task.executor_attempt_id,
                    session_id=task.resume_session_id,
                    occurred_at=now.isoformat(),
                )
                if task.attempt_count < task.max_attempts:
                    task.status = "queued"
                    task.available_at = now
                    task.message = "租约已过期，等待重新认领"
                    task.error = recovery_error
                    queued.append(task.id)
                else:
                    task.status = "failed"
                    task.completed_at = now
                    task.message = "任务达到最大尝试次数"
                    terminal_error = {"error": "max_attempts_exceeded", "message": "任务达到最大尝试次数"}
                    append_task_error_history(
                        task,
                        terminal_error,
                        attempt=task.attempt_count,
                        executor_attempt_id=task.executor_attempt_id,
                        session_id=task.resume_session_id,
                        occurred_at=now.isoformat(),
                    )
                    task.error = terminal_error
                task.lease_owner = None
                task.lease_expires_at = None
                task.updated_at = now
                self._release_slot(session, task.scope_id, task.id, owner=self.owner, claim_generation=task.claim_generation)
            session.commit()
        return queued

    @staticmethod
    def _release_slot(session: Any, scope_id: str, task_id: str, *, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """在全局恢复事务中按完整 claim 释放 lane 槽位。"""
        slot = session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == scope_id, TaskLaneSlot.task_id == task_id).with_for_update())
        if slot is not None:
            if owner is not None and slot.lease_owner not in {None, owner}:
                logger.info("task_lane_fencing_rejection task=%s scope=%s", task_id, scope_id)
                return False
            if claim_generation is not None and getattr(slot, "claim_generation", None) not in {None, claim_generation}:
                logger.info("task_lane_fencing_rejection task=%s scope=%s", task_id, scope_id)
                return False
            slot.task_scope_id = None
            slot.task_id = None
            slot.lease_owner = None
            if hasattr(slot, "claim_generation"):
                slot.claim_generation = None
            slot.lease_expires_at = None
            return True
        return False

    def _fail_invalid_scope_tasks(self) -> list[str]:
        """启动扫描发现非法持久 scope 时稳定失败，绝不猜测为 local。"""
        invalid: list[str] = []
        with self.resources.factory() as session:
            rows = list(
                session.scalars(
                    select(Task).where(
                        Task.status.in_(("queued", "running")),
                        ~Task.task_type.in_(IMAGE_PROCESSING_TASK_TYPES),
                    )
                )
            )
            for task in rows:
                try:
                    ScopeContext(task.scope_id)
                except (TypeError, ValueError):
                    invalid.append(task.id)
                    task.status = "failed"
                    task.completed_at = utcnow()
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.error = append_task_error_history(
                        task,
                        {"error": "task_scope_invalid", "message": "任务缺少有效 scope"},
                        attempt=task.attempt_count,
                        executor_attempt_id=task.executor_attempt_id,
                        session_id=task.resume_session_id,
                        occurred_at=utcnow().isoformat(),
                    )
                    task.message = "任务 scope 无效"
                    self._release_slot(session, task.scope_id, task.id)
            session.commit()
        return invalid

    def _recover_pending_batches(self) -> list[str]:
        """按批次所属 scope 恢复未收束的唯一 cache 任务，不扫描并实例化全部历史 scope。"""
        queued: list[str] = []
        if self._scope_service_resolver is None:
            return queued
        with self.resources.factory() as session:
            batches = list(session.execute(select(TaskBatch.scope_id, TaskBatch.batch_id).where(TaskBatch.sealed.is_(True), TaskBatch.finalizer_state.in_(('pending', 'submitted'))).limit(5000)).all())
        for scope_id, batch_id in batches:
            try:
                services = self._scope_service_resolver(scope_id)
                with self.resources.environment(services.scope) as environment:
                    task = environment.tasks.finalize_batch_with_task(
                        batch_id,
                        task_type="cache_generation",
                        payload={},
                        dedupe_key="cache_generation",
                        settings_version=self.settings_version,
                        max_attempts=self.max_attempts,
                    )
                    if task is not None:
                        queued.append(task.id)
            except (DatabaseError, RuntimeError, TypeError, ValueError):
                # 任务 scope 无法装配时由后续任务诊断和宿主日志收束，不回退到 local。
                continue
        return queued

    def schedule(self, task_id: str) -> None:
        """将任务加入进程级调度集合，避免不同 scope 重复提交同一 future。"""
        with self._lock:
            if self._stopped.is_set() or task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._executor.submit(self._run, task_id)

    def _run(self, task_id: str) -> None:
        """先按持久 scope 认领任务，再创建 scope facade 执行处理器。"""
        service = None
        claim = None
        try:
            claim = self._claim_for_task(task_id)
            if claim is None:
                self._task_finished(task_id, claimed=False)
                return
            if self._scope_service_resolver is None:
                raise RuntimeError("task_scope_unavailable")
            # Worker 认领后仍必须复用统一 factory 校验；宿主自定义 resolver 不能以
            # 返回错误 scope 的 facade 绕过 claim 的业务隔离边界。
            services = validate_scope_services(ScopeContext(claim.scope_id), self._scope_service_resolver(claim.scope_id))
            service = getattr(services, "tasks", None)
            if service is None:
                raise RuntimeError("task_scope_unavailable")
            service._run(task_id, preclaimed=claim)
        except Exception:
            if service is None:
                # 工厂异常发生在 claim 之后时只能用完整 claim fencing 收束；
                # 没有 claim 证据则不按裸 task_id 修改任意任务。
                self._fail_unresolvable(claim)
                self._task_finished(task_id, claimed=claim is not None)
            else:
                # 业务 facade 异常不能遗留 scheduled 标记，否则恢复扫描无法再次唤醒任务。
                self._task_finished(task_id, claimed=claim is not None)

    def _claim_for_task(self, task_id: str) -> Task | None:
        """从任务控制面读取 scope 并在创建业务 facade 前完成 claim。"""
        with self.resources.factory() as session:
            scope_id = session.scalar(select(Task.scope_id).where(Task.id == task_id))
        if not isinstance(scope_id, str) or not scope_id:
            return None
        try:
            scope = ScopeContext(scope_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("task_scope_invalid") from exc
        with self.resources.environment(scope) as environment:
            queued = environment.tasks.get(task_id)
            if queued is None:
                return None
            if queued.task_type in IMAGE_PROCESSING_TASK_TYPES:
                # 图片任务属于专用 Worker；通用 manager 不得认领或收束它们。
                return None
            return environment.tasks.claim(
                owner=self.owner,
                lease_seconds=self.lease_seconds,
                task_id=task_id,
                lane=queued.lane,
                lane_capacity=self.agent_concurrency if queued.lane == "agent" else None,
            )

    def _fail_unresolvable(self, claim: Task | None) -> None:
        """按完整 claim 收束 scope 装配失败，旧 claim 不得终止新 Worker。"""
        if claim is None:
            return
        with self.resources.factory() as session:
            now = utcnow()
            statement = select(Task).where(
                Task.scope_id == claim.scope_id,
                Task.id == claim.id,
                Task.status == "running",
                Task.claim_generation == claim.claim_generation,
                Task.lease_owner == self.owner,
                Task.lease_expires_at > now,
            ).with_for_update()
            task = session.scalar(statement)
            if task is None:
                logger.info("task_scope_assembly_fencing_rejection task=%s scope=%s generation=%s", claim.id, claim.scope_id, claim.claim_generation)
                session.commit()
                return
            task.status = "failed"
            task.completed_at = utcnow()
            task.lease_owner = None
            task.lease_expires_at = None
            task.message = "任务 scope 无法装配"
            task.error = append_task_error_history(
                task,
                {"error": "task_scope_unavailable", "message": "任务 scope 当前不可用"},
                attempt=getattr(task, "attempt_count", 0),
                executor_attempt_id=getattr(task, "executor_attempt_id", None),
                session_id=getattr(task, "resume_session_id", None),
                occurred_at=utcnow().isoformat(),
            )
            self._release_slot(session, task.scope_id, task.id, owner=self.owner, claim_generation=claim.claim_generation)
            session.commit()

    def _task_finished(self, task_id: str, *, claimed: bool) -> None:
        """释放全局调度标记，并在 lane 槽位释放后扫描下一批任务。"""
        with self._lock:
            self._scheduled.discard(task_id)
            stopped = self._stopped.is_set()
        if claimed and not stopped:
            self._schedule_queued()

    def _schedule_queued(self) -> None:
        """跨 scope 唤醒 queued 任务，保持全局 lane 背压后的前进性。"""
        if self._stopped.is_set():
            return
        with self.resources.factory() as session:
            task_ids = list(
                session.scalars(
                    select(Task.id)
                    .where(
                        Task.status == "queued",
                        ~Task.task_type.in_(IMAGE_PROCESSING_TASK_TYPES),
                    )
                    .limit(500)
                )
            )
        for task_id in task_ids:
            self.schedule(task_id)

    def shutdown(self) -> None:
        """停止调度并等待本进程持有的任务退出后再释放线程池。"""
        self._stopped.set()
        now = utcnow()
        with self.resources.factory() as session:
            rows = list(session.scalars(select(Task).where(Task.status == "running", Task.lease_owner == self.owner).with_for_update(skip_locked=True)))
            for task in rows:
                interrupted_error = {"error": "task_interrupted", "message": "任务执行 Worker 已停止"}
                append_task_error_history(
                    task,
                    interrupted_error,
                    attempt=task.attempt_count,
                    executor_attempt_id=task.executor_attempt_id,
                    session_id=task.resume_session_id,
                    occurred_at=now.isoformat(),
                )
                task.status = "failed"
                task.completed_at = now
                task.message = "Worker 已停止"
                task.error = interrupted_error
                task.lease_owner = None
                task.lease_expires_at = None
                task.updated_at = now
                self._release_slot(session, task.scope_id, task.id, owner=self.owner, claim_generation=task.claim_generation)
            session.commit()
        if self._owns_executor:
            # 任务线程可能仍在使用数据库连接；先等待其退出，避免应用释放连接池
            # 后留下后台事务与下一次启动/测试清理互相死锁。
            self._executor.shutdown(wait=True, cancel_futures=True)


class PostgresTaskService:
    """使用指定 scope 记录、去重、租约和 claim fencing 的任务执行器。

    直接构造时的 local 默认值仅用于开源兼容夹具；生产 Worker 由 scope factory 装配。
    ``scope`` 只用于选择任务表中的候选行；真正执行时仍从刚认领的 Task 行
    恢复并校验 scope，避免普通 payload 或 Worker 的历史默认值成为归属事实。
    """

    def __init__(self, resources: DatabaseResources, *, scope_id: str | ScopeContext = "local", agent_concurrency: int = 1, agent_backpressure: int = 32, settings_version: str | None = None, lease_seconds: int = 120, max_attempts: int = 3, executor: ThreadPoolExecutor | None = None, worker_manager: PostgresTaskWorkerManager | None = None, finalize_image_tasks: bool = True, operation_policy: OperationPolicyGateway | None = None, grant_store: GrantAssociationStore | None = None, resume_enabled: bool = False, resume_max_attempts: int = 2, resume_backoff_seconds: int = 2, resume_max_backoff_seconds: int = 60, resume_timeout_seconds: int = 900):
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.agent_concurrency = max(1, min(int(agent_concurrency), 8))
        self.agent_backpressure = max(1, min(int(agent_backpressure), 500))
        self.settings_version = settings_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.resume_enabled = bool(resume_enabled)
        self.resume_max_attempts = max(0, min(int(resume_max_attempts), 10))
        self.resume_backoff_seconds = max(0, min(int(resume_backoff_seconds), 300))
        self.resume_max_backoff_seconds = max(0, min(int(resume_max_backoff_seconds), 3600))
        self.resume_timeout_seconds = max(1, min(int(resume_timeout_seconds), 86400))
        # 图片 Worker 执行叶子任务时关闭旧批次 finalizer，避免再次隐式创建
        # cache_generation；普通兼容 facade 仍保留既有显式批次能力。
        self._finalize_image_tasks = bool(finalize_image_tasks)
        self._handlers: dict[str, Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]] = {}
        self._batch_finalizer: Callable[[str], Any] | None = None
        self._worker_manager = worker_manager
        # 只有图片专用 facade 注入该二元组时才启用 Agent grant 复核；普通
        # 兼容任务服务仍可执行历史任务，但不会替客户端伪造计量事实。
        self._operation_policy = operation_policy
        self._grant_store = grant_store
        self._executor = worker_manager.executor if worker_manager is not None else executor or ThreadPoolExecutor(max_workers=max(2, self.agent_concurrency + 1), thread_name_prefix="mememeow-pg-task")
        self._owns_executor = worker_manager is None and executor is None
        self._lock = Lock()
        self._stopped = Event()
        self._scheduled: set[str] = set()
        self.owner = worker_manager.owner if worker_manager is not None else f"worker-{os.getpid()}-{id(self)}"

    def register(self, task_type: str, handler: Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]) -> None:
        """注册由数据库 payload 重建的同步处理器。"""
        if self._worker_manager is not None:
            self._worker_manager.register(task_type, handler)
        else:
            self._handlers[task_type] = handler

    def set_batch_finalizer(self, callback: Callable[[str], Any] | None) -> None:
        """注册批次全部终态后的单次收束回调。"""
        self._batch_finalizer = callback

    def seal_batch(self, batch_id: str) -> None:
        """封口批次并在同一数据库事务中持久化唯一缓存任务。"""
        created_task_id: str | None = None
        with self.resources.environment(self.scope.scope_id) as environment:
            environment.tasks.seal_batch(batch_id)
            task = environment.tasks.finalize_batch_with_task(
                batch_id,
                task_type="cache_generation",
                payload={},
                dedupe_key="cache_generation",
                settings_version=self.settings_version,
                max_attempts=self.max_attempts,
            )
            if task is not None:
                created_task_id = task.id
        if created_task_id:
            self._schedule(created_task_id)

    @staticmethod
    def _dedupe(task_type: str, payload: dict[str, Any]) -> str:
        """为普通任务和图片阶段任务生成包含来源模式的稳定活动去重键。"""
        mode = str(payload.get("submission_mode") or ("pipeline" if payload.get("job_id") else "legacy"))
        stage = str(payload.get("stage") or {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
            "text_embedding_generation": "text_embedding",
        }.get(task_type) or "legacy")
        if task_type == "visual_embedding_generation":
            return "visual:{mode}:{stage}:{meme}:{sha}:{model}:{preprocess}:{config}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                model=payload.get("visual_model"),
                preprocess=payload.get("preprocess_version"),
                config=payload.get("processing_config_hash") or "legacy",
            )
        if task_type == "meme_context_generation":
            return "context:{mode}:{stage}:{meme}:{sha}:{config}:{policy}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                config=payload.get("processing_config_hash") or payload.get("skill_hash") or payload.get("model"),
                policy=payload.get("reverse_image_policy") or "forbid",
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "image_auto_rename":
            return "rename:{mode}:{stage}:{meme}:{sha}:{storage}:{title}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                storage=payload.get("expected_storage_key"),
                title=payload.get("title_fingerprint"),
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "text_embedding_generation":
            return "text:{mode}:{stage}:{meme}:{sha}:{metadata}:{model}:{config}:r{revision}".format(
                mode=mode,
                stage=stage,
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                metadata=payload.get("metadata_hash") or "unknown",
                model=payload.get("embedding_model") or payload.get("model"),
                config=payload.get("processing_config_hash") or "legacy",
                revision=payload.get("job_revision") or "legacy",
            )
        if task_type == "cache_generation":
            return "cache_generation"
        return f"{task_type}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    def _context_policy_conflict(self, payload: dict[str, Any], dedupe: str) -> None:
        """拒绝同一图片活动任务的策略不一致提交，避免静默复用错误权限。"""
        if payload.get("reverse_image_policy") not in {"forbid", "auto"}:
            return
        requested_mode = payload.get("submission_mode") if payload.get("submission_mode") in {"pipeline", "standalone"} else None
        with self.resources.environment(self.scope.scope_id) as environment:
            existing = environment.uow.session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.task_type == "meme_context_generation",
                    Task.submission_mode == requested_mode,
                    Task.dedupe_key.like(f"context:%:{payload.get('meme_id')}:{payload.get('image_sha256')}:%"),
                    Task.status.in_(('queued', 'running')),
                )
            )
            if existing is not None:
                current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
                current_config = str((existing.payload or {}).get("processing_config_hash") or (existing.payload or {}).get("skill_hash") or (existing.payload or {}).get("model") or "")
                requested_config = str(payload.get("processing_config_hash") or payload.get("skill_hash") or payload.get("model") or "")
                if current != str(payload.get("reverse_image_policy")) or current_config != requested_config:
                    raise RuntimeError("generation_policy_conflict")

    @staticmethod
    def _assert_context_policy(existing: Task, requested: dict[str, Any]) -> None:
        """在任务 repository 复用活动任务后再次核对策略，覆盖预检与插入之间的竞态。"""
        current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
        wanted = str(requested.get("reverse_image_policy") or "forbid")
        current_config = str((existing.payload or {}).get("processing_config_hash") or (existing.payload or {}).get("skill_hash") or (existing.payload or {}).get("model") or "")
        wanted_config = str(requested.get("processing_config_hash") or requested.get("skill_hash") or requested.get("model") or "")
        if current != wanted or current_config != wanted_config:
            raise RuntimeError("generation_policy_conflict")

    def start(self) -> None:
        """启动数据库任务恢复调度，包括队列和过期租约。"""
        if self._worker_manager is not None:
            self._worker_manager.start()
            return
        owned_types = None if self._finalize_image_tasks else IMAGE_PROCESSING_TASK_TYPES
        with self.resources.environment(self.scope.scope_id) as environment:
            queued = environment.tasks.recover_expired(
                owner=self.owner,
                limit=5000,
                exclude_task_types=IMAGE_PROCESSING_TASK_TYPES if owned_types is None else None,
                include_task_types=owned_types,
            )
            # 普通 facade 需要恢复旧批次 finalizer；图片专用 facade 禁止重新
            # 创建 scope 级 cache_generation。
            pending_batches = environment.tasks.pending_finalizer_batches(limit=5000) if self._finalize_image_tasks else []
            cursor = None
            while True:
                records, cursor = environment.tasks.list(statuses={"queued"}, cursor=cursor, limit=100)
                queued.extend(
                    record.id
                    for record in records
                    if owned_types is None or record.task_type in owned_types
                )
                if cursor is None:
                    break
            for batch_id in pending_batches:
                task = environment.tasks.finalize_batch_with_task(
                    batch_id,
                    task_type="cache_generation",
                    payload={},
                    dedupe_key="cache_generation",
                    settings_version=self.settings_version,
                    max_attempts=self.max_attempts,
                )
                if task is not None:
                    queued.append(task.id)
        for task_id in dict.fromkeys(queued):
            self._schedule(task_id)

    def _record_to_dataclass(self, record: Any, *, slot_id: int | None = None) -> TaskRecord:
        """将 ORM 任务转换为 API/旧领域共用的安全快照。"""
        session_id = normalize_identifier(getattr(record, "resume_session_id", None), kind="session")
        executor_attempt_id = normalize_identifier(getattr(record, "executor_attempt_id", None), kind="attempt")
        stored_resume_available = bool(getattr(record, "resume_available", False))
        return TaskRecord(
            task_id=record.id,
            task_type=record.task_type,
            submission_mode=getattr(record, "submission_mode", None),
            image_stage=getattr(record, "image_stage", None),
            processing_job_id=str(getattr(record, "processing_job_id", "")) if getattr(record, "processing_job_id", None) else None,
            payload=dict(record.payload or {}),
            status=record.status,
            progress=record.progress,
            message=record.message,
            created_at=_iso(record.created_at),
            updated_at=_iso(record.updated_at),
            completed_at=_iso(record.completed_at) if record.completed_at else None,
            attempts=record.attempt_count,
            error=sanitize_error(record.error) if isinstance(getattr(record, "error", None), dict) else None,
            resume_available=bool(stored_resume_available and session_id and executor_attempt_id),
            resume_reason=("session_not_resumable" if stored_resume_available and not (session_id and executor_attempt_id) else getattr(record, "resume_reason", None)),
            session_id=session_id,
            executor_attempt_id=executor_attempt_id,
            workspace_selector=getattr(record, "workspace_selector", None) if isinstance(getattr(record, "workspace_selector", None), str) else None,
            resume_attempts=int(getattr(record, "resume_attempt_count", 0) or 0),
            resume_started_at=_iso(getattr(record, "resume_started_at", None)) if getattr(record, "resume_started_at", None) else None,
            first_error=sanitize_error(getattr(record, "first_error", None)) if isinstance(getattr(record, "first_error", None), dict) else None,
            error_history=sanitize_error_history(getattr(record, "error_history", None)),
            result=record.result,
            settings_version=record.settings_version,
            agent_concurrency=self.agent_concurrency if record.lane == "agent" else None,
            slot_id=slot_id,
            scope_id=record.scope_id,
        )

    def _image_attempt_state(self, claim: Task, payload: dict[str, Any], state: str) -> None:
        """保存图片叶子当前 claim 的 attempt 状态，供重启恢复辨认未知执行。"""
        if claim.task_type not in IMAGE_PROCESSING_TASK_TYPES:
            return
        mode = payload.get("submission_mode")
        if mode == "pipeline" and not isinstance(payload.get("job_id"), str):
            return
        if mode not in {None, "pipeline", "standalone"}:
            return
        target_sha = payload.get("image_sha256")
        if not isinstance(target_sha, str) or len(target_sha) != 64:
            return
        # claim、resume 和 attempt 绑定字段都是运行时事实，不属于同一输入的
        # 业务摘要；排除全部内部字段才能让续跑 attempt 与原 attempt 对齐。
        stable_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        input_digest = hashlib.sha256(json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        now = utcnow()
        raw_selector = payload.get("_workspace_selector")
        if raw_selector is not None and (
            not isinstance(raw_selector, str)
            or not SELECTOR_RE.fullmatch(raw_selector)
            or (self.scope.scope_id != "local" and raw_selector == "local")
        ):
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            current_task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == claim.id,
                    Task.status == "running",
                    Task.claim_generation == claim.claim_generation,
                    Task.lease_owner == self.owner,
                    Task.lease_expires_at > now,
                )
            )
            if current_task is None:
                return
            row = session.scalar(
                select(ImageProcessingAttempt).where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt == claim.attempt_count,
                ).with_for_update()
            )
            if row is None:
                row = ImageProcessingAttempt(
                    scope_id=self.scope.scope_id,
                    task_id=claim.id,
                    attempt=claim.attempt_count,
                    attempt_id=uuid4().hex,
                    stage=str(payload.get("stage") or claim.task_type),
                    state=state,
                    request_id=str(payload.get("request_id")) if payload.get("request_id") else None,
                    session_id=str(payload.get("_resume_session_id") or payload.get("session_id")) if (payload.get("_resume_session_id") or payload.get("session_id")) else None,
                    executor_attempt_id=normalize_identifier(payload.get("_executor_attempt_id"), kind="attempt"),
                    resume_of_attempt_id=normalize_identifier(payload.get("_resume_of_attempt_id"), kind="attempt"),
                    workspace_selector=str(payload.get("_workspace_selector")) if isinstance(payload.get("_workspace_selector"), str) and SELECTOR_RE.fullmatch(str(payload.get("_workspace_selector"))) else None,
                    processing_config_hash=normalize_config_hash(payload.get("processing_config_hash")),
                    input_digest=input_digest,
                    target_sha256=target_sha.lower(),
                    claim_generation=claim.claim_generation,
                )
                session.add(row)
            else:
                # 旧 Worker 不能把新 claim 的 attempt 状态覆盖回去。
                if row.claim_generation != claim.claim_generation:
                    return
                if isinstance(raw_selector, str) and row.workspace_selector is not None and row.workspace_selector != raw_selector:
                    return
                row.state = state
                row.updated_at = now
                if normalize_identifier(payload.get("_resume_session_id"), kind="session"):
                    row.session_id = str(payload["_resume_session_id"])
                if normalize_identifier(payload.get("_executor_attempt_id"), kind="attempt"):
                    row.executor_attempt_id = str(payload["_executor_attempt_id"])
                if isinstance(payload.get("_workspace_selector"), str) and SELECTOR_RE.fullmatch(str(payload["_workspace_selector"])):
                    row.workspace_selector = str(payload["_workspace_selector"])
            session.commit()

    def record_agent_attempt(
        self,
        payload: dict[str, Any],
        *,
        error: dict[str, Any] | None = None,
        session_id: str | None = None,
        executor_attempt_id: str | None = None,
        workspace_selector: str | None = None,
        resume_available: bool = False,
        resume_reason: str | None = None,
    ) -> bool:
        """在当前 claim fencing 下持久化 Agent session、executor attempt 和失败历史。"""
        task_id = payload.get("_claim_task_id")
        generation = payload.get("_claim_generation")
        owner = payload.get("_claim_owner")
        attempt = payload.get("_claim_attempt")
        if not isinstance(task_id, str) or not isinstance(generation, int) or not isinstance(owner, str) or not isinstance(attempt, int):
            return False
        safe_session = normalize_identifier(session_id, kind="session")
        safe_executor_attempt = normalize_identifier(executor_attempt_id, kind="attempt")
        supplied_selector = workspace_selector if workspace_selector is not None else payload.get("_workspace_selector")
        if supplied_selector is not None and (
            not isinstance(supplied_selector, str)
            or not SELECTOR_RE.fullmatch(supplied_selector)
            or (self.scope.scope_id != "local" and supplied_selector == "local")
        ):
            return False
        safe_workspace_selector = supplied_selector if isinstance(supplied_selector, str) else None
        if self.scope.scope_id != "local" and safe_workspace_selector is None:
            # non-local attempt 缺少绑定时不能把失败或成功写成可继续执行的事实。
            return False
        payload_config_hash = normalize_config_hash(payload.get("processing_config_hash"))
        if payload.get("processing_config_hash") is not None and payload_config_hash is None:
            return False
        now = utcnow()
        safe_error = sanitize_error(error) if error else None
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == task_id,
                    Task.status == "running",
                    Task.claim_generation == generation,
                    Task.lease_owner == owner,
                    Task.lease_expires_at > now,
                )
                .with_for_update()
            )
            if task is None:
                session.commit()
                return False
            row = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == task_id,
                    ImageProcessingAttempt.attempt == attempt,
                )
                .with_for_update()
            )
            if row is None:
                session.commit()
                return False
            if row.claim_generation != generation:
                # attempt 行也必须与当前 claim generation 一致；只校验 Task
                # 行会允许旧 attempt 借用同一 attempt 序号写入新 claim。
                session.commit()
                return False
            if normalize_config_hash(row.processing_config_hash) != payload_config_hash:
                session.commit()
                return False
            if safe_workspace_selector is not None and row.workspace_selector is not None and row.workspace_selector != safe_workspace_selector:
                session.commit()
                return False
            if safe_workspace_selector is not None and task.workspace_selector is not None and task.workspace_selector != safe_workspace_selector:
                session.commit()
                return False
            if safe_session:
                row.session_id = safe_session
                task.resume_session_id = safe_session
            if safe_executor_attempt:
                row.executor_attempt_id = safe_executor_attempt
                task.executor_attempt_id = safe_executor_attempt
            if safe_workspace_selector:
                row.workspace_selector = safe_workspace_selector
                task.workspace_selector = safe_workspace_selector
            if safe_error:
                row.error = safe_error
                row.resume_reason = resume_reason or safe_error.get("error")
            row.resume_available = bool(resume_available and safe_session and safe_executor_attempt)
            row.state = "failed" if safe_error else "completed"
            row.updated_at = now
            if safe_error:
                task.first_error = sanitize_error(task.first_error) if isinstance(task.first_error, dict) else safe_error
                task.error_history = append_error_history(
                    task.error_history,
                    safe_error,
                    attempt=attempt,
                    executor_attempt_id=safe_executor_attempt,
                    session_id=safe_session,
                    occurred_at=now.isoformat(),
                )
                task.resume_available = bool(resume_available and safe_session and safe_executor_attempt)
                task.resume_reason = resume_reason or safe_error.get("error")
                if task.resume_available and task.resume_started_at is None:
                    task.resume_started_at = now
            else:
                task.resume_available = False
                task.resume_reason = None
            session.commit()
            return True

    def _resume_candidate(self, claim: Task, payload: dict[str, Any]) -> dict[str, str] | None:
        """读取并校验同一任务最近的可续跑 attempt，拒绝猜测 session。"""
        if not self.resume_enabled or claim.task_type != "meme_context_generation":
            return None
        if claim.resume_attempt_count >= self.resume_max_attempts:
            return None
        if not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
            return None
        target_sha = payload.get("image_sha256")
        if not isinstance(target_sha, str) or len(target_sha) != 64:
            return None
        config_hash = normalize_config_hash(payload.get("processing_config_hash"))
        if payload.get("processing_config_hash") is not None and config_hash is None:
            return None
        stable_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        input_digest = hashlib.sha256(json.dumps(stable_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.resources.factory() as session:
            previous = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt < claim.attempt_count,
                    ImageProcessingAttempt.state == "failed",
                    ImageProcessingAttempt.resume_available.is_(True),
                    ImageProcessingAttempt.target_sha256 == target_sha.lower(),
                    ImageProcessingAttempt.input_digest == input_digest,
                )
                .order_by(ImageProcessingAttempt.attempt.desc())
            )
        session_id = normalize_identifier(getattr(previous, "session_id", None), kind="session") if previous else None
        executor_attempt_id = normalize_identifier(getattr(previous, "executor_attempt_id", None), kind="attempt") if previous else None
        if not session_id or not executor_attempt_id:
            return None
        selector = getattr(previous, "workspace_selector", None) if previous is not None else None
        if not isinstance(selector, str) or not SELECTOR_RE.fullmatch(selector):
            if self.scope.scope_id == "local":
                selector = "local"
            else:
                raise RuntimeError("opencode_workspace_mismatch")
        if self.scope.scope_id != "local" and selector == "local":
            raise RuntimeError("opencode_workspace_mismatch")
        if normalize_config_hash(getattr(previous, "processing_config_hash", None)) != config_hash:
            return None
        previous_reason = getattr(previous, "resume_reason", None) if previous is not None else None
        resume_reason = previous_reason if isinstance(previous_reason, str) and previous_reason else "session_resumable"
        return {
            "session_id": session_id,
            "executor_attempt_id": executor_attempt_id,
            "resume_of_attempt_id": executor_attempt_id,
            "resume_reason": resume_reason,
            "workspace_selector": selector,
        }

    def _begin_resume(self, claim: Task, candidate: dict[str, str]) -> bool:
        """在 claim fencing 下原子递增续跑次数，防止并发恢复器重复使用 session。"""
        now = utcnow()
        with self.resources.environment(self.scope.scope_id) as environment:
            session = environment.uow.session
            task = session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.id == claim.id,
                    Task.status == "running",
                    Task.claim_generation == claim.claim_generation,
                    Task.lease_owner == self.owner,
                    Task.lease_expires_at > now,
                    Task.resume_attempt_count < self.resume_max_attempts,
                )
                .with_for_update()
            )
            if task is None:
                session.commit()
                return False
            task.resume_attempt_count += 1
            task.resume_started_at = task.resume_started_at or now
            task.resume_available = False
            task.resume_reason = "resume_started"
            task.resume_session_id = candidate["session_id"]
            session.commit()
            return True

    def _image_attempt_requires_unknown(self, claim: Task) -> bool:
        """判断新 claim 前是否存在无法证明已完成的图片外部 attempt。"""
        if claim.task_type not in IMAGE_PROCESSING_TASK_TYPES or claim.attempt_count <= 1:
            return False
        # 续跑配置已启用时，额度或累计时间耗尽即使历史 attempt 行缺失也必须
        # 直接 fencing；否则数据库部分损坏会把恢复请求降级成一次新外部调用。
        if claim.task_type == "meme_context_generation" and self.resume_enabled:
            if claim.resume_attempt_count >= self.resume_max_attempts:
                return True
            if claim.resume_started_at is not None and not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
                return True
        with self.resources.factory() as session:
            previous = session.scalar(
                select(ImageProcessingAttempt)
                .where(
                    ImageProcessingAttempt.scope_id == self.scope.scope_id,
                    ImageProcessingAttempt.task_id == claim.id,
                    ImageProcessingAttempt.attempt < claim.attempt_count,
                )
                .order_by(ImageProcessingAttempt.attempt.desc())
            )
            if previous is None:
                return False
            if previous.state in {"grant_committed", "external_started", "completed"}:
                return True
            # 续跑额度或累计时间耗尽后，不得退化成新的无 session 外部调用；
            # 只要历史上存在可续跑失败，就把当前 claim 收束为 unknown_execution。
            if self.resume_enabled and previous.resume_available:
                if not normalize_identifier(previous.session_id, kind="session") or not normalize_identifier(previous.executor_attempt_id, kind="attempt"):
                    return True
                if self.scope.scope_id != "local" and (
                    not isinstance(previous.workspace_selector, str)
                    or not SELECTOR_RE.fullmatch(previous.workspace_selector)
                    or previous.workspace_selector == "local"
                ):
                    # 旧 attempt 已声明可恢复但没有 workspace 绑定，不能退化为
                    # 新 session 执行；由恢复链收束为不可安全重放。
                    return True
                if claim.resume_attempt_count >= self.resume_max_attempts or not within_total_timeout(claim.resume_started_at, timeout_seconds=self.resume_timeout_seconds):
                    return True
            return False

    def _commit_agent_grant(self, claim: Task, payload: dict[str, Any]) -> None:
        """在 Agent 外部执行前幂等提交服务端 grant。"""
        if claim.task_type != "meme_context_generation" or self._operation_policy is None or self._grant_store is None:
            return
        meme_id = payload.get("meme_id")
        image_sha256 = payload.get("image_sha256")
        config_hash = payload.get("processing_config_hash")
        revision = payload.get("job_revision")
        policy = payload.get("reverse_image_policy") or "forbid"
        if not all(isinstance(value, str) and value for value in (meme_id, image_sha256, config_hash)):
            raise OperationPolicyError("operation_grant_invalid")
        mode = payload.get("submission_mode")
        if mode == "standalone":
            logical_key = payload.get("agent_grant_key")
            if not isinstance(logical_key, str) or not logical_key.startswith("standalone-agent:"):
                raise OperationPolicyError("operation_grant_invalid")
            source = "image-processing-standalone"
        else:
            logical_key = f"agent:{meme_id}:{image_sha256}:{config_hash}:{policy}:r{revision}"
            source = "image-processing"
        request = self._operation_policy.request(self.scope, Operations.ANALYSIS_AGENT, logical_key, resource_id=meme_id, task_id=claim.id, source=source, input_digest=image_sha256)
        association = self._grant_store.get(request)
        if association is None or association.grant.scope != self.scope or association.grant.operation != Operations.ANALYSIS_AGENT:
            raise OperationPolicyError("operation_grant_invalid")
        if association.state == "committed":
            return
        if association.state != "acquired":
            raise OperationPolicyError("operation_grant_invalid")
        try:
            result = self._operation_policy.commit(association.grant)
        except OperationPolicyError:
            # policy 返回异常时无法证明计量是否已经生效；保留 unknown，后续
            # claim 只能收束，不能通过重试再次触发不确定的计量边界。
            self._grant_store.transition(association.grant, "unknown")
            raise
        if not result.ok or result.state not in {"committed", "already_committed"}:
            self._grant_store.transition(association.grant, "unknown")
            raise OperationPolicyError("operation_policy_unavailable", retry_at=result.retry_at)
        if not self._grant_store.transition(association.grant, "committed"):
            self._grant_store.transition(association.grant, "unknown")
            raise OperationPolicyError("operation_grant_invalid")

    def submit(self, task_type: str, payload: dict[str, Any] | None = None, *, schedule: bool = True) -> TaskRecord:
        """以事务插入或复用活动任务，并立即安排本进程执行。

        图片阶段来源由当前受信控制面 payload 规范化后落入专用列；客户端不能
        通过额外 scope、Job、grant 或 claim 字段改变这些事实。
        """
        payload = dict(payload or {})
        for field in UNTRUSTED_SCOPE_FIELDS:
            payload.pop(field, None)
        # scope/user 只能由 resolver 或 Task.scope_id 提供；即使调用方伪造字段，
        # 也不得让它们进入后续 handler 作为授权事实。
        payload.pop("scope_id", None)
        payload.pop("user_id", None)
        # session/attempt 只能由当前 Worker 从持久 attempt 恢复，客户端 payload
        # 即使携带同名字段也不得改变续跑绑定事实。
        for internal_field in ("session_id", "executor_attempt_id", "attempt_id", "resume_available", "resume_reason"):
            payload.pop(internal_field, None)
        lane = "agent" if task_type == "meme_context_generation" else "default"
        image_stage = None
        submission_mode = None
        processing_job_id = None
        if task_type in IMAGE_PROCESSING_TASK_TYPES:
            stage_by_type = {
                "visual_embedding_generation": "visual",
                "meme_context_generation": "agent",
                "image_auto_rename": "auto_rename",
                "text_embedding_generation": "text_embedding",
            }
            expected_stage = stage_by_type[task_type]
            requested_stage = payload.get("stage")
            if requested_stage is not None and requested_stage != expected_stage:
                raise RuntimeError("image_stage_mismatch")
            requested_mode = payload.get("submission_mode")
            if requested_mode not in {"pipeline", "standalone"}:
                # 没有新来源字段的旧记录仍可被读取/执行，查询时会显示为未归类；
                # 新控制面入口始终显式传入 mode。
                requested_mode = None
            submission_mode = requested_mode
            if submission_mode == "pipeline":
                raw_job_id = payload.get("job_id")
                if not isinstance(raw_job_id, str) or not raw_job_id:
                    raise RuntimeError("image_processing_job_required")
                processing_job_id = raw_job_id
            elif submission_mode == "standalone":
                if payload.get("job_id") is not None:
                    raise RuntimeError("image_task_job_conflict")
            elif payload.get("job_id") is not None:
                # 旧 job 叶子在来源迁移前仍按 pipeline 处理，避免丢失父 Job
                # 关联；该分支只接受服务端已有的 Job UUID。
                submission_mode = "pipeline"
                processing_job_id = payload.get("job_id")
            # 没有阶段、Job 或来源字段的旧 facade 调用属于迁移前任务。保留
            # NULL image_stage 使它可以继续完成既有业务；迁移脚本对能够
            # 可靠识别阶段的历史任务会补写 image_stage，专用 Worker 随后
            # 将那类未归类任务收束为只读诊断。
            explicit_source = requested_stage is not None or requested_mode is not None or processing_job_id is not None
            if explicit_source:
                image_stage = expected_stage
                payload["stage"] = expected_stage
                if submission_mode is not None:
                    payload["submission_mode"] = submission_mode
        dedupe = self._dedupe(task_type, payload)
        if task_type == "meme_context_generation":
            self._context_policy_conflict(payload, dedupe)
        with self.resources.environment(self.scope.scope_id) as environment:
            try:
                record = environment.tasks.submit(task_type=task_type, payload=payload, lane=lane, dedupe_key=dedupe, settings_version=self.settings_version, max_attempts=self.max_attempts, lane_backpressure=self.agent_backpressure if lane == "agent" else None, submission_mode=submission_mode, image_stage=image_stage, processing_job_id=processing_job_id)
            except DatabaseError as exc:
                if exc.code == "agent_backpressure":
                    existing = environment.uow.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe, Task.status.in_(("queued", "running"))))
                    if existing:
                        record = existing
                    else:
                        raise RuntimeError("agent_backpressure") from exc
                else:
                    raise
            if task_type == "meme_context_generation":
                self._assert_context_policy(record, payload)
            if task_type in {"meme_context_generation", "visual_embedding_generation"} and isinstance(payload.get("batch_id"), str):
                environment.tasks.add_batch_item(payload["batch_id"], record.id)
            snapshot = self._record_to_dataclass(record)
        if schedule:
            self._schedule(snapshot.task_id)
        return snapshot

    def retry(self, task_id: str) -> TaskRecord:
        """重试一个普通失败任务；图片阶段必须通过受限图片入口重试。"""
        record = self.get(task_id)
        if record is None:
            raise RuntimeError("task_not_found")
        if record.task_type in IMAGE_PROCESSING_TASK_TYPES:
            raise RuntimeError("image_stage_retry_forbidden")
        if record.status != "failed":
            raise RuntimeError("task_not_failed")
        payload = {key: value for key, value in record.payload.items() if not key.startswith("_claim_")}
        # 失败任务的同一 dedupe key 已不属于活动集合，submit 会创建新的可轮询尝试。
        return self.submit(record.task_type, payload)

    def schedule(self, task_id: str) -> None:
        """显式唤醒一个已提交任务；批量提交时用于关闭入批竞态窗口。"""
        self._schedule(task_id)

    def _schedule(self, task_id: str) -> None:
        """避免同一进程重复调度同一个数据库任务。"""
        if self._worker_manager is not None:
            self._worker_manager.schedule(task_id)
            return
        with self._lock:
            if self._stopped.is_set() or task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._executor.submit(self._run, task_id)

    def _run(self, task_id: str, *, preclaimed: Task | None = None) -> None:
        """执行已认领任务并以 claim generation fencing 写回终态。"""
        claim = preclaimed
        try:
            if claim is None:
                with self.resources.environment(self.scope.scope_id) as environment:
                    queued_record = environment.tasks.get(task_id)
                    if queued_record is None:
                        return
                    lane = queued_record.lane
                    claim = environment.tasks.claim(
                        owner=self.owner,
                        lease_seconds=self.lease_seconds,
                        task_id=task_id,
                        lane=lane,
                        lane_capacity=self.agent_concurrency if lane == "agent" else None,
                        # 专用 facade 必须能恢复自己的过期图片叶子；通用 manager
                        # 在更早的 ``_claim_for_task`` 边界排除这些类型。
                        exclude_task_types=None,
                    )
                    if claim is None or claim.id != task_id:
                        return
                    task_payload = dict(claim.payload or {})
                    generation = claim.claim_generation
                    task_payload["_claim_task_id"] = claim.id
                    task_payload["_claim_generation"] = generation
                    task_payload["_claim_owner"] = self.owner
                    task_payload["_claim_attempt"] = claim.attempt_count
                    task_payload["_resume_attempt_count"] = claim.resume_attempt_count
                    task_payload["_resume_started_at"] = claim.resume_started_at
                    try:
                        claim_scope = ScopeContext(claim.scope_id)
                    except (TypeError, ValueError) as exc:
                        # 无效持久 scope 不能猜测为 local；以稳定错误进入 fencing 收束。
                        environment.tasks.fail_fenced(task_id, generation, self.owner, message="任务 scope 无效", error={"error": "task_scope_invalid", "message": "任务缺少有效 scope"}, retry=False)
                        raise RuntimeError("task_scope_invalid") from exc
                    if claim_scope.scope_id != self.scope.scope_id:
                        environment.tasks.fail_fenced(task_id, generation, self.owner, message="任务 scope 与 Worker 不一致", error={"error": "task_scope_mismatch", "message": "任务 scope 与当前执行环境不一致"}, retry=False)
                        raise RuntimeError("task_scope_mismatch")
                    task_payload["_claim_scope_id"] = claim_scope.scope_id
            else:
                if claim.id != task_id or claim.scope_id != self.scope.scope_id:
                    return
                task_payload = dict(claim.payload or {})
                generation = claim.claim_generation
                task_payload["_claim_task_id"] = claim.id
                task_payload["_claim_generation"] = generation
                task_payload["_claim_owner"] = self.owner
                task_payload["_claim_attempt"] = claim.attempt_count
                task_payload["_resume_attempt_count"] = claim.resume_attempt_count
                task_payload["_resume_started_at"] = claim.resume_started_at
                try:
                    claim_scope = ScopeContext(claim.scope_id)
                except (TypeError, ValueError):
                    return
                if claim_scope.scope_id != self.scope.scope_id:
                    return
                task_payload["_claim_scope_id"] = claim_scope.scope_id
            handler = self._worker_manager.handler(claim.task_type) if self._worker_manager is not None else self._handlers.get(claim.task_type)
            if handler is None:
                self._fenced_failure(task_id, generation, message="任务处理器不可用", error={"error": "task_handler_missing", "message": "当前服务未注册此任务类型"}, retry=False)
                return

            if claim.task_type in IMAGE_PROCESSING_TASK_TYPES and claim.submission_mode not in {"pipeline", "standalone"} and claim.image_stage is not None:
                # 无法可靠归类的历史图片 Task 只允许查询诊断，不能在启动恢复时
                # 被旧 Worker 重新执行或通过异常路径产生下游阶段。没有显式
                # 阶段列的迁移前兼容任务仍由原任务 facade 完成。
                self._fenced_failure(
                    task_id,
                    generation,
                    message="历史图片任务未归类，只读展示",
                    error={"error": "image_task_unclassified", "message": "历史图片阶段缺少可信提交来源"},
                    retry=False,
                )
                return

            if self._image_attempt_requires_unknown(claim):
                self._image_attempt_state(claim, task_payload, "unknown_execution")
                self._fenced_failure(
                    task_id,
                    generation,
                    message="外部执行结果无法确认",
                    error={"error": "unknown_execution", "message": "上一次图片阶段已进入外部执行窗口，无法安全重放"},
                    retry=False,
                    resume_available=False,
                    resume_reason="unknown_execution",
                )
                return

            try:
                resume_candidate = self._resume_candidate(claim, task_payload)
            except RuntimeError as exc:
                code = str(exc).partition(":")[0]
                if code != "opencode_workspace_mismatch":
                    raise
                self._image_attempt_state(claim, task_payload, "failed")
                self._fenced_failure(
                    task_id,
                    generation,
                    message="workspace 绑定无法恢复",
                    error={"error": code, "message": "workspace selector 与持久恢复事实不一致"},
                    retry=False,
                    resume_available=False,
                    resume_reason=code,
                )
                return
            if resume_candidate is not None:
                if not self._begin_resume(claim, resume_candidate):
                    # 恢复计数的原子 fencing 失败时不能降级为一次全新外部调用；
                    # 让当前 claim 自然收束，由仍有效的 Worker 重新决定。
                    return
                # session 只来自上一条同 scope/Task/输入摘要的 attempt，不能从
                # 普通 payload 或客户端请求直接注入。
                task_payload["_resume_session_id"] = resume_candidate["session_id"]
                task_payload["_resume_of_attempt_id"] = resume_candidate["resume_of_attempt_id"]
                task_payload["_previous_executor_attempt_id"] = resume_candidate["executor_attempt_id"]
                # 候选已通过全部恢复绑定校验；即使下一次 executor 错误没有回传
                # session，也必须保留这份服务端确认的可续跑事实交给 handler。
                task_payload["_resume_available"] = True
                task_payload["_resume_reason"] = resume_candidate["resume_reason"]
                task_payload["_workspace_selector"] = resume_candidate["workspace_selector"]

            self._image_attempt_state(claim, task_payload, "prepared")

            def progress(value: float | None, message: str | None = None) -> None:
                self._fenced_update(task_id, generation, progress=value, message=message)

            heartbeat_stop = Event()
            def heartbeat() -> None:
                """定期续租当前 claim，停止后退出后台线程。"""
                while not heartbeat_stop.wait(max(1, self.lease_seconds // 3)):
                    with self.resources.environment(self.scope.scope_id) as heartbeat_env:
                        if not heartbeat_env.tasks.heartbeat(task_id, generation, self.owner, self.lease_seconds):
                            return
            heartbeat_thread = threading.Thread(target=heartbeat, name=f"mememeow-heartbeat-{task_id}", daemon=True)
            heartbeat_thread.start()
            try:
                if claim.task_type in IMAGE_PROCESSING_TASK_TYPES:
                    # 图片阶段均可能触发外部模型或持久副作用；Agent 先完成
                    # grant commit，再进入外部执行窗口，恢复者才能区分计量边界。
                    self._commit_agent_grant(claim, task_payload)
                    if claim.task_type == "meme_context_generation":
                        self._image_attempt_state(claim, task_payload, "grant_committed")
                    # 恢复者无法证明结果时必须收束 unknown_execution。
                    self._image_attempt_state(claim, task_payload, "external_started")
                result = handler(task_payload, progress)
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, OperationPolicyError):
                    code = exc.code
                    diagnostic = code
                else:
                    diagnostic = str(exc)[:500]
                    code = diagnostic.partition(":")[0] if diagnostic.partition(":")[0] in STABLE_TASK_ERRORS else "task_failed"
                resume_available = bool(task_payload.get("_resume_available"))
                resume_reason = task_payload.get("_resume_reason") if isinstance(task_payload.get("_resume_reason"), str) else None
                session_id = task_payload.get("_resume_session_id") if isinstance(task_payload.get("_resume_session_id"), str) else None
                executor_attempt_id = task_payload.get("_executor_attempt_id") if isinstance(task_payload.get("_executor_attempt_id"), str) else None
                if claim.task_type == "meme_context_generation" and agent_failure_requires_unknown(
                    code,
                    session_id=session_id,
                    resume_available=resume_available,
                    resuming=isinstance(task_payload.get("_resume_session_id"), str),
                    resume_enabled=self.resume_enabled,
                ):
                    # handler 已尽力记录原始 executor/provider 错误；任务终态必须
                    # 另行收束为 unknown_execution，阻止同一业务任务从头重放。
                    original_code = code
                    code = "unknown_execution"
                    diagnostic = f"外部执行状态无法确认（{original_code}）"
                    resume_available = False
                    if resume_reason != "resume_budget_exhausted":
                        resume_reason = "unknown_execution"
                self._image_attempt_state(claim, task_payload, "unknown_execution" if code in {"unknown_execution", "reverse_image_unknown_execution"} else "failed")
                retry_delay = bounded_backoff(
                    claim.resume_attempt_count,
                    base_seconds=self.resume_backoff_seconds if self.resume_enabled and resume_available else 0,
                    max_seconds=self.resume_max_backoff_seconds,
                )
                retry = code not in {
                    "target_changed",
                    "agent_output_schema_invalid",
                    "agent_output_invalid_json",
                    "agent_result_file_missing",
                    "agent_result_file_unreadable",
                    "agent_result_file_too_large",
                    "agent_result_file_invalid_json",
                    "agent_result_file_schema_invalid",
                    "agent_image_path_forbidden",
                    "agent_input_provider_unavailable",
                    "agent_result_path_invalid",
                    "task_handler_missing",
                    "opencode_not_configured",
                    "agent_runtime_unavailable",
                    "agent_image_root_mismatch",
                    "reverse_image_forbidden",
                    "invalid_reverse_image_policy",
                    "usage_request_conflict",
                    "visual_model_not_configured",
                    "visual_model_migration_required",
                    "visual_model_identity_invalid",
                    "visual_weights_checksum_mismatch",
                    "visual_embedding_dimensions_mismatch",
                    "visual_embedding_non_finite",
                    "visual_embedding_zero_norm",
                    "visual_image_decode_failed",
                    "visual_model_identity_mismatch",
                    "visual_service_invalid_response",
                    "visual_embedding_invalid",
                    "visual_embedding_sha256_invalid",
                    "visual_embedding_sha256_mismatch",
                    "embedding_not_configured",
                    "embedding_dimensions_mismatch",
                    "embedding_non_finite",
                    "embedding_zero_norm",
                    "query_embedding_not_ready",
                    "invalid_task",
                    "task_not_running",
                    "auto_rename_title_missing",
                    "auto_rename_invalid_filename",
                    "auto_rename_target_exists",
                    "auto_rename_target_changed",
                    "auto_rename_claim_expired",
                    "auto_rename_unknown_execution",
                    "task_scope_invalid",
                    "task_scope_mismatch",
                    "unknown_execution",
                    "reverse_image_unknown_execution",
                    "operation_forbidden",
                    "operation_limit_exceeded",
                    "operation_policy_unavailable",
                    "operation_grant_invalid",
                    "blocked",
                }
                audit_result = self._with_reverse_image_audit(task_id, None, write_provenance=False)
                self._fenced_failure(
                    task_id,
                    generation,
                    message="任务执行失败",
                    error={"error": code, "message": diagnostic},
                    retry=retry,
                    result=audit_result,
                    retry_delay_seconds=retry_delay,
                    resume_available=resume_available,
                    resume_reason=resume_reason,
                    session_id=session_id,
                    executor_attempt_id=executor_attempt_id,
                )
            else:
                # 只有当前 claim 仍有效时才写入任务终态和 Meme provenance。
                self._image_attempt_state(claim, task_payload, "completed")
                audit_result = self._with_reverse_image_audit(task_id, result, write_provenance=False)
                self._fenced_success(task_id, generation, audit_result)
            finally:
                heartbeat_stop.set()
            self._maybe_finalize(task_id)
        finally:
            if self._worker_manager is not None:
                self._worker_manager._task_finished(task_id, claimed=claim is not None)
            else:
                with self._lock:
                    self._scheduled.discard(task_id)
                if claim is not None and not self._stopped.is_set():
                    self._schedule_queued()

    def _schedule_queued(self) -> None:
        """在槽位释放后唤醒数据库中的排队任务，避免 lane 满载时忙循环。"""
        if self._worker_manager is not None:
            self._worker_manager._schedule_queued()
            return
        if self._stopped.is_set():
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            records, _ = environment.tasks.list(statuses={"queued"}, limit=100)
        for record in records:
            if self._finalize_image_tasks or record.task_type in IMAGE_PROCESSING_TASK_TYPES:
                self._schedule(record.id)

    def _fenced_update(self, task_id: str, generation: int, **changes: Any) -> bool:
        """在一个短事务中验证 owner/generation/租约后更新任务。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.tasks.update_fenced(task_id, generation, self.owner, **changes)

    def _fenced_success(self, task_id: str, generation: int, result: Any) -> bool:
        """以 claim fencing 原子提交成功结果和图片 Agent provenance。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            complete = getattr(environment.tasks, "complete_fenced_with_provenance", None)
            if callable(complete):
                return bool(complete(task_id, generation, self.owner, result=result))
            # 兼容尚未提供原子扩展的宿主 repository；标准 PostgreSQL
            # repository 始终走上面的单事务路径。
            changed = environment.tasks.update_fenced(
                task_id,
                generation,
                self.owner,
                status="succeeded",
                progress=1.0,
                message="任务完成",
                result=result,
            )
        if changed:
            self._write_reverse_image_provenance(task_id, generation)
        return changed

    def _fenced_failure(self, task_id: str, generation: int, *, message: str, error: dict[str, Any], retry: bool, result: Any | None = None, retry_delay_seconds: int = 0, resume_available: bool | None = None, resume_reason: str | None = None, session_id: str | None = None, executor_attempt_id: str | None = None) -> bool:
        """按最大尝试次数将当前 claim 重新排队或置为失败。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            changed, should_retry = environment.tasks.fail_fenced(
                task_id,
                generation,
                self.owner,
                error=error,
                message=message,
                retry=retry,
                result=result,
                retry_delay_seconds=retry_delay_seconds,
                resume_available=resume_available,
                resume_reason=resume_reason,
                session_id=session_id,
                executor_attempt_id=executor_attempt_id,
            )
        if changed and should_retry:
            self._schedule(task_id)
        return changed

    def _with_reverse_image_audit(self, task_id: str, result: Any, *, write_provenance: bool = False) -> dict[str, Any]:
        """按任务事件生成终态审计摘要，默认不修改 Meme provenance。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            task = environment.tasks.get(task_id)
            policy = str((task.payload or {}).get("reverse_image_policy") or "forbid") if task else "forbid"
            audit = environment.reverse_image_usage.aggregate_task(task_id)
            payload = dict(result) if isinstance(result, dict) else {}
            payload["reverse_image"] = {"policy": policy, **audit}
            if write_provenance:
                meme_id = (task.payload or {}).get("meme_id") if task else None
                if meme_id:
                    self._write_reverse_image_provenance(task_id, None)
            return payload

    def _write_reverse_image_provenance(self, task_id: str, claim_generation: int | None) -> None:
        """仅在成功 claim 的收束阶段写回 Meme 反向图片审计，避免旧 Worker 覆盖新结果。"""
        try:
            with self.resources.environment(self.scope.scope_id) as environment:
                task = environment.tasks.get(task_id)
                if task is None or task.status != "succeeded":
                    return
                if claim_generation is not None and task.claim_generation != claim_generation:
                    return
                audit = environment.reverse_image_usage.aggregate_task(task_id)
                meme_id = (task.payload or {}).get("meme_id")
                if not meme_id:
                    return
                meme = environment.memes.get(meme_id, for_update=True)
                if meme is None:
                    return
                provenance = dict(meme.provenance or {})
                provenance["reverse_image"] = {"policy": str((task.payload or {}).get("reverse_image_policy") or "forbid"), **audit}
                meme.provenance = provenance
                environment.uow.session.flush()
        except Exception:
            # 审计 provenance 是可重建的附属写回，不能让已成功的任务线程崩溃。
            return

    def get(self, task_id: str) -> TaskRecord | None:
        """读取当前 scope 任务快照。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.tasks.get(task_id)
            if record is None:
                return None
            slot = environment.tasks.slot_for_task(record.id)
            return self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None)

    def find_active(self, task_type: str, dedupe_key: str) -> TaskRecord | None:
        """按当前 scope、类型和活动去重键读取叶子 Task。

        图片 Worker 在取得 Agent grant 前调用此方法，避免把已有活动任务误判为
        新的计量请求；查询结果只是提示，真正提交仍由 TaskRepository 的唯一键兜底。
        """
        if not isinstance(task_type, str) or not task_type or not isinstance(dedupe_key, str) or not dedupe_key:
            return None
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.uow.session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.task_type == task_type,
                    Task.dedupe_key == dedupe_key,
                    Task.status.in_(("queued", "running")),
                )
                .order_by(Task.created_at.asc(), Task.id.asc())
            )
            if record is None:
                return None
            slot = environment.tasks.slot_for_task(record.id)
            return self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None)

    def cancel(self, task_id: str) -> bool:
        """取消单个任务并仅终止其 Agent session，不停止共享容器。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            record = environment.tasks.get(task_id, for_update=True)
            if record is None:
                return False
            changed = environment.tasks.cancel(task_id, error={"error": "task_cancelled", "message": "任务已取消"}, message="任务已取消")
        if changed:
            handler = self._handlers.get(record.task_type)
            _ = handler  # 仅保留任务类型快照，实际 session 清理由运行器按 task_id 完成。
        return changed

    def list(self, *, statuses: set[str] | None = None, task_types: set[str] | None = None, cursor: str | None = None, limit: int = 50) -> tuple[list[TaskRecord], str | None]:
        """分页列出当前 scope 任务，返回兼容 TaskRecord 的安全快照。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            records, next_cursor = environment.tasks.list(statuses=statuses, task_types=task_types, cursor=cursor, limit=limit)
            result = []
            for record in records:
                slot = environment.tasks.slot_for_task(record.id)
                result.append(self._record_to_dataclass(record, slot_id=slot.slot_number if slot else None))
            return result, next_cursor

    def _maybe_finalize(self, task_id: str) -> None:
        """批次成员全部终态后在数据库中只提交一次 finalizer 标记。"""
        record = self.get(task_id)
        if not record or record.task_type not in {"meme_context_generation", "visual_embedding_generation"}:
            return
        if not self._finalize_image_tasks:
            return
        # 批量接口可能复用上传时已存在的活动去重任务；此时 payload 没有
        # batch_id，必须以数据库关联表为准，避免 finalizer 永久遗漏。
        with self.resources.environment(self.scope.scope_id) as environment:
            values = environment.tasks.batch_ids_for_task(task_id)
        batch_id = record.payload.get("batch_id")
        batch_ids = record.payload.get("batch_ids")
        if isinstance(batch_id, str) and batch_id:
            values.append(batch_id)
        if isinstance(batch_ids, list):
            values.extend(item for item in batch_ids if isinstance(item, str) and item)
        values = list(dict.fromkeys(values))
        if not values:
            return
        for current_batch_id in values:
            with self.resources.environment(self.scope.scope_id) as environment:
                task = environment.tasks.finalize_batch_with_task(
                    current_batch_id,
                    task_type="cache_generation",
                    payload={},
                    dedupe_key="cache_generation",
                    settings_version=self.settings_version,
                    max_attempts=self.max_attempts,
                )
                created_task_id = task.id if task is not None else None
            if created_task_id:
                self._schedule(created_task_id)
            if task is None or not self._batch_finalizer:
                continue
            try:
                self._batch_finalizer(current_batch_id)
            except Exception:
                pass

    def shutdown(self) -> None:
        """停止新认领并将本 Worker 仍持有的任务标记为可诊断中断。"""
        if self._worker_manager is not None:
            return
        self._stopped.set()
        with self.resources.environment(self.scope.scope_id) as environment:
            environment.tasks.interrupt_owner(self.owner)
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
