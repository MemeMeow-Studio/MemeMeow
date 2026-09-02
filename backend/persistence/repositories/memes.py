"""Meme 记录的 scope 绑定持久化访问。

该模块位于持久化 Repository 边界，只负责 Meme 记录及其语境状态的数据库读写；
文件一致性仍由 StorageCoordinator 负责，旧 backend.database 导入路径由 facade 保留。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key
from backend.persistence.engine import DatabaseError
from backend.persistence.models import Meme, ScopeContext, StorageOperation, Task, utcnow


class MemeRepository:
    """按构造绑定 scope 的 Meme 读写 repository。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, meme_id: UUID | str, *, for_update: bool = False) -> Meme | None:
        """只读取当前 scope 的 Meme，不接受客户端 scope 覆盖。"""
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError):
            return None
        statement = select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == identifier)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def by_storage_key(self, storage_key: str, *, for_update: bool = False) -> Meme | None:
        """按当前 scope 的相对 storage_key 查询 Meme。"""
        statement = select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.storage_key == storage_key)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list(self, *, search: str | None = None, page: int = 1, page_size: int = 200) -> list[Meme]:
        """在数据库内按文件名筛选、分页并稳定排序当前 scope 的 Meme。"""
        statement = select(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        statement = statement.order_by(Meme.storage_key.asc(), Meme.id.asc()).offset(max(0, page - 1) * page_size).limit(max(1, min(page_size, 200)))
        return list(self.session.scalars(statement))

    def count(self, *, search: str | None = None) -> int:
        """返回当前 scope 的可见 Meme 数量，筛选在数据库执行。"""
        statement = select(func.count()).select_from(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        return int(self.session.scalar(statement) or 0)

    def list_all(self, *, search: str | None = None) -> list[Meme]:
        """供缓存生成等内部批处理读取当前 scope 全量 Meme；公共列表仍使用分页。"""
        statement = select(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        return list(self.session.scalars(statement.order_by(Meme.storage_key.asc(), Meme.id.asc())))

    def _visible_predicate(self) -> tuple[Any, ...]:
        """返回列表与 count 共用的结构可见性条件。

        结构性脏记录在数据库层隐藏；原图是否存在由媒体消费路径单独严格校验。
        """
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        return (
            Meme.scope_id == self.scope.scope_id,
            ~active_operation,
            Meme.storage_key != "",
            ~Meme.storage_key.in_((".", "..", ".staging", ".quarantine")),
            ~Meme.storage_key.contains("/"),
            ~Meme.storage_key.contains("\\"),
            Meme.extension.in_(tuple(SUPPORTED_EXTENSIONS)),
        )

    def create(self, *, storage_key: str, extension: str, size_bytes: int, sha256: str, context: dict[str, Any], provenance: dict[str, Any], status: str = "pending", meme_id: UUID | None = None, extensions: dict[str, Any] | None = None) -> Meme:
        """创建稳定 UUID Meme 和初始语境记录。"""
        try:
            validate_business_storage_key(storage_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        from backend.metadata import MemeContext, semantic_document_hash

        parsed_context = MemeContext.model_validate(context)
        record = Meme(id=meme_id or uuid.uuid4(), scope_id=self.scope.scope_id, storage_key=storage_key, extension=extension.lower(), size_bytes=size_bytes, sha256=sha256, context_status=status, search_metadata_hash=semantic_document_hash(parsed_context), meme_context=parsed_context.model_dump(mode="json", exclude_none=False), provenance=provenance, extensions=extensions or {}, revision=1)
        self.session.add(record)
        self.session.flush()
        return record

    def update_context(self, meme_id: UUID | str, *, context: dict[str, Any], provenance: dict[str, Any], status: str, expected_revision: int | None = None, expected_sha256: str | None = None, claim: tuple[str, int, str] | None = None) -> Meme:
        """以 revision、SHA 和可选任务 claim 在单事务中更新完整语境。"""
        record = self.get(meme_id, for_update=True)
        if record is None:
            raise DatabaseError("meme_not_found")
        if claim is not None:
            task_id, claim_generation, owner = claim
            now = utcnow()
            task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
            if task is None or task.status != "running" or task.claim_generation != claim_generation or task.lease_owner != owner or task.lease_expires_at is None or task.lease_expires_at <= now:
                raise DatabaseError("claim_expired")
        if expected_revision is not None and record.revision != expected_revision:
            raise DatabaseError("target_changed")
        if expected_sha256 is not None and record.sha256 != expected_sha256:
            raise DatabaseError("target_changed")
        from backend.metadata import MemeContext, semantic_document_hash

        parsed_context = MemeContext.model_validate(context)
        record.meme_context = parsed_context.model_dump(mode="json", exclude_none=False)
        record.search_metadata_hash = semantic_document_hash(parsed_context)
        record.provenance = provenance
        record.context_status = status
        record.revision += 1
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def rename(self, meme_id: UUID | str, storage_key: str) -> Meme:
        """按稳定 Meme ID 更新扁平 storage_key，确保身份和 revision 保持。"""
        try:
            validate_business_storage_key(storage_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        record = self.get(meme_id, for_update=True)
        if record is None:
            raise DatabaseError("meme_not_found")
        if self.by_storage_key(storage_key) and record.storage_key != storage_key:
            raise DatabaseError("target_exists")
        record.storage_key = storage_key
        record.revision += 1
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def delete(self, meme_id: UUID | str) -> Meme:
        """删除当前 scope 的 Meme 记录；调用方负责先完成文件隔离。"""
        record = self.get(meme_id, for_update=True)
        if record is None:
            raise DatabaseError("meme_not_found")
        self.session.delete(record)
        self.session.flush()
        return record
