"""PostgreSQL 版元数据、文件生命周期和检索服务适配器。

领域 schema 与旧服务共用 ``MemeContext``/``SidecarMetadata`` 类型，但运行时不读写
sidecar 文件；数据库记录是唯一结构化事实，BlobStore 只保存图片字节。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import UUID

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
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    ScopeContext,
    SearchGeneration,
    StorageCoordinator,
    StorageOperation,
    Task,
    UnitOfWork,
    utcnow,
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
from backend.paths import SUPPORTED_EXTENSIONS
from backend.tasks import TaskRecord, TERMINAL, STABLE_TASK_ERRORS
from backend.visual import VisualEmbeddingError, VisualInferenceClient, identity_from_settings


def _iso(value: datetime | str | None) -> str:
    """将数据库时间转换为旧领域模型接受的 UTC ISO 字符串。"""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat() if isinstance(value, datetime) else str(value)


class PostgresMetadataService:
    """以 scope-bound repository 管理 Meme 元数据和图片指纹。"""

    def __init__(self, resources: DatabaseResources, *, scope_id: str = "local"):
        self.resources = resources
        self.scope = ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(scope_id)
        self.root = self.blob_store.root
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage = StorageCoordinator(resources, scope_id=scope_id)

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
        """将图片解析为 local scope 内的 POSIX storage_key。"""
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
    """基于 search_generations/meme_embeddings 的 pgvector 检索服务。"""

    def __init__(self, settings: Any, resources: DatabaseResources, metadata: PostgresMetadataService, *, scope_id: str = "local"):
        self.settings, self.resources, self.metadata = settings, resources, metadata
        self.scope = ScopeContext(scope_id)
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
        """检查当前 scope/model 是否存在 active generation。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.search.active_generation(self.model) is not None

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


class PostgresTaskService:
    """使用 PostgreSQL 记录、去重、租约和 claim fencing 的任务执行器。"""

    def __init__(self, resources: DatabaseResources, *, scope_id: str = "local", agent_concurrency: int = 1, agent_backpressure: int = 32, settings_version: str | None = None, lease_seconds: int = 120, max_attempts: int = 3):
        self.resources = resources
        self.scope = ScopeContext(scope_id)
        self.agent_concurrency = max(1, min(int(agent_concurrency), 8))
        self.agent_backpressure = max(1, min(int(agent_backpressure), 500))
        self.settings_version = settings_version
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._handlers: dict[str, Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]] = {}
        self._batch_finalizer: Callable[[str], Any] | None = None
        self._executor = ThreadPoolExecutor(max_workers=max(2, self.agent_concurrency + 1), thread_name_prefix="mememeow-pg-task")
        self._lock = Lock()
        self._stopped = Event()
        self._scheduled: set[str] = set()
        self.owner = f"worker-{os.getpid()}-{id(self)}"

    def register(self, task_type: str, handler: Callable[[dict[str, Any], Callable[[float | None, str | None], None]], Any]) -> None:
        """注册由数据库 payload 重建的同步处理器。"""
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
        """为普通任务和图片语境任务生成稳定活动去重键。"""
        if task_type == "visual_embedding_generation":
            return "visual:{meme}:{sha}:{model}:{preprocess}".format(
                meme=payload.get("meme_id"),
                sha=payload.get("image_sha256"),
                model=payload.get("visual_model"),
                preprocess=payload.get("preprocess_version"),
            )
        if task_type == "meme_context_generation":
            return f"context:{payload.get('meme_id')}:{payload.get('image_sha256')}"
        if task_type == "cache_generation":
            return "cache_generation"
        return f"{task_type}:{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"

    def _context_policy_conflict(self, payload: dict[str, Any], dedupe: str) -> None:
        """拒绝同一图片活动任务的策略不一致提交，避免静默复用错误权限。"""
        if payload.get("reverse_image_policy") not in {"forbid", "auto"}:
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            existing = environment.uow.session.scalar(
                select(Task)
                .where(
                    Task.scope_id == self.scope.scope_id,
                    Task.task_type == "meme_context_generation",
                    Task.dedupe_key == dedupe,
                    Task.status.in_(('queued', 'running')),
                )
            )
            if existing is not None:
                current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
                if current != str(payload.get("reverse_image_policy")):
                    raise RuntimeError("generation_policy_conflict")

    @staticmethod
    def _assert_context_policy(existing: Task, requested: dict[str, Any]) -> None:
        """在任务 repository 复用活动任务后再次核对策略，覆盖预检与插入之间的竞态。"""
        current = str((existing.payload or {}).get("reverse_image_policy") or "forbid")
        wanted = str(requested.get("reverse_image_policy") or "forbid")
        if current != wanted:
            raise RuntimeError("generation_policy_conflict")

    def start(self) -> None:
        """启动数据库任务恢复调度，包括队列和过期租约。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            queued = environment.tasks.recover_expired(owner=self.owner, limit=5000)
            # 批次状态和索引任务在同一事务提交；进程可能在提交后尚未唤醒任务时退出，
            # 因此启动时重新扫描 pending/submitted 批次，依靠 dedupe_key 恢复唯一任务。
            pending_batches = environment.tasks.pending_finalizer_batches(limit=5000)
            cursor = None
            while True:
                records, cursor = environment.tasks.list(statuses={"queued"}, cursor=cursor, limit=100)
                queued.extend(record.id for record in records)
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
        return TaskRecord(task_id=record.id, task_type=record.task_type, payload=dict(record.payload or {}), status=record.status, progress=record.progress, message=record.message, created_at=_iso(record.created_at), updated_at=_iso(record.updated_at), completed_at=_iso(record.completed_at) if record.completed_at else None, attempts=record.attempt_count, error=record.error, result=record.result, settings_version=record.settings_version, agent_concurrency=self.agent_concurrency if record.lane == "agent" else None, slot_id=slot_id)

    def submit(self, task_type: str, payload: dict[str, Any] | None = None, *, schedule: bool = True) -> TaskRecord:
        """以事务插入或复用活动任务，并立即安排本进程执行。"""
        payload = dict(payload or {})
        lane = "agent" if task_type == "meme_context_generation" else "default"
        dedupe = self._dedupe(task_type, payload)
        if task_type == "meme_context_generation":
            self._context_policy_conflict(payload, dedupe)
        with self.resources.environment(self.scope.scope_id) as environment:
            try:
                record = environment.tasks.submit(task_type=task_type, payload=payload, lane=lane, dedupe_key=dedupe, settings_version=self.settings_version, max_attempts=self.max_attempts, lane_backpressure=self.agent_backpressure if lane == "agent" else None)
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
        """显式重试一个失败阶段，复用其 payload 而不级联重跑下游阶段。"""
        record = self.get(task_id)
        if record is None:
            raise RuntimeError("task_not_found")
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
        with self._lock:
            if self._stopped.is_set() or task_id in self._scheduled:
                return
            self._scheduled.add(task_id)
        self._executor.submit(self._run, task_id)

    def _run(self, task_id: str) -> None:
        """认领任务、执行处理器并以 claim generation fencing 写回终态。"""
        claim = None
        try:
            with self.resources.environment(self.scope.scope_id) as environment:
                queued_record = environment.tasks.get(task_id)
                if queued_record is None:
                    return
                lane = queued_record.lane
                claim = environment.tasks.claim(owner=self.owner, lease_seconds=self.lease_seconds, task_id=task_id, lane=lane, lane_capacity=self.agent_concurrency if lane == "agent" else None)
                if claim is None or claim.id != task_id:
                    return
                task_payload = dict(claim.payload or {})
                generation = claim.claim_generation
                task_payload["_claim_task_id"] = claim.id
                task_payload["_claim_generation"] = generation
                task_payload["_claim_owner"] = self.owner
            handler = self._handlers.get(claim.task_type)
            if handler is None:
                self._fenced_failure(task_id, generation, message="任务处理器不可用", error={"error": "task_handler_missing", "message": "当前服务未注册此任务类型"}, retry=False)
                return

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
                result = handler(task_payload, progress)
            except Exception as exc:  # noqa: BLE001
                diagnostic = str(exc)[:500]
                code = diagnostic.partition(":")[0] if diagnostic.partition(":")[0] in STABLE_TASK_ERRORS else "task_failed"
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
                    "visual_embedding_non_finite",
                    "visual_embedding_zero_norm",
                    "visual_image_decode_failed",
                    "visual_model_identity_mismatch",
                    "visual_service_invalid_response",
                    "visual_embedding_invalid",
                    "visual_embedding_sha256_invalid",
                    "visual_embedding_sha256_mismatch",
                    "invalid_task",
                    "task_not_running",
                }
                audit_result = self._with_reverse_image_audit(task_id, None, write_provenance=False)
                self._fenced_failure(task_id, generation, message="任务执行失败", error={"error": code, "message": diagnostic}, retry=retry, result=audit_result)
            else:
                # 只有当前 claim 仍有效时才写入任务终态和 Meme provenance。
                audit_result = self._with_reverse_image_audit(task_id, result, write_provenance=False)
                if self._fenced_update(task_id, generation, status="succeeded", progress=1.0, message="任务完成", result=audit_result):
                    self._write_reverse_image_provenance(task_id, generation)
            finally:
                heartbeat_stop.set()
            self._maybe_finalize(task_id)
        finally:
            with self._lock:
                self._scheduled.discard(task_id)
            if claim is not None and not self._stopped.is_set():
                self._schedule_queued()

    def _schedule_queued(self) -> None:
        """在槽位释放后唤醒数据库中的排队任务，避免 lane 满载时忙循环。"""
        if self._stopped.is_set():
            return
        with self.resources.environment(self.scope.scope_id) as environment:
            records, _ = environment.tasks.list(statuses={"queued"}, limit=100)
        for record in records:
            self._schedule(record.id)

    def _fenced_update(self, task_id: str, generation: int, **changes: Any) -> bool:
        """在一个短事务中验证 owner/generation/租约后更新任务。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            return environment.tasks.update_fenced(task_id, generation, self.owner, **changes)

    def _fenced_failure(self, task_id: str, generation: int, *, message: str, error: dict[str, Any], retry: bool, result: Any | None = None) -> bool:
        """按最大尝试次数将当前 claim 重新排队或置为失败。"""
        with self.resources.environment(self.scope.scope_id) as environment:
            changed, should_retry = environment.tasks.fail_fenced(task_id, generation, self.owner, error=error, message=message, retry=retry, result=result)
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
        self._stopped.set()
        with self.resources.environment(self.scope.scope_id) as environment:
            environment.tasks.interrupt_owner(self.owner)
        self._executor.shutdown(wait=False, cancel_futures=True)
