from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.persistence.models import (
    EMBEDDING_DIMENSIONS,
    ImageProcessingAttempt,
    Meme,
    MemeEmbedding,
    MemeVisualEmbedding,
    ScopeContext,
    SearchGeneration,
    StorageOperation,
    Task,
    TaskBatch,
    TaskLaneSlot,
)
from backend.persistence.engine import DatabaseError
from backend.persistence.resources import DatabaseResources
from backend.persistence.storage import StorageCoordinator
from backend.persistence.models import utcnow
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
from backend.visual import VisualEmbeddingError, VisualInferenceClient, identity_from_settings

# 统一使用旧 facade 的 logger 名称，保持现有安全审计筛选和运营日志聚合。
logger = logging.getLogger("backend.pg_services")

class PostgresMetadataService:
    """以 scope-bound repository 管理 Meme 元数据和图片指纹。

    ``scope_id`` 的 local 默认值只为开源旧夹具保留；应用运行时由
    ``ScopeServiceFactory`` 显式传入 scope。
    """

    def __init__(self, resources: DatabaseResources, *, scope_id: str | ScopeContext = "local"):
        """绑定数据库资源和 scope，并初始化受控图片存储协调器。"""
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
