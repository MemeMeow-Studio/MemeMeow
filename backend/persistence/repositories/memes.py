"""Meme 记录的 scope 绑定持久化访问。

该模块位于持久化 Repository 边界，只负责 Meme 记录及其语境状态的数据库读写；
文件一致性仍由 StorageCoordinator 负责，旧 backend.database 导入路径由 facade 保留。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from backend.image_naming import normalize_display_name, normalize_extension, saved_filename
from backend.embedding_input import build_text_input
from backend.paths import validate_business_storage_key
from backend.persistence.engine import DatabaseError
from backend.persistence.models import Meme, ScopeContext, StorageOperation, Task, utcnow
from backend.task_deadline import ensure_task_deadline


ImageSort = Literal["name_asc", "updated_desc"]


def _durable_meme_clause():
    """构造列表和 count 共用的最终 durable Meme 过滤条件。"""
    return and_(
        Meme.sha256.op("~")(r"^[0-9a-f]{64}$"),
        Meme.extension.in_((".png", ".jpg", ".jpeg", ".gif")),
        Meme.extension == func.lower(Meme.extension),
        Meme.storage_key != "",
        Meme.storage_key == Meme.sha256 + Meme.extension,
        Meme.storage_key.not_like("%/%"),
        Meme.storage_key.not_like("%\\%", escape="!"),
        Meme.storage_key.not_in((".", "..", ".staging", ".quarantine")),
        Meme.display_name.is_not(None),
        Meme.display_name != "",
        Meme.display_name == func.btrim(Meme.display_name, " ."),
        Meme.display_name.not_like("%/%"),
        Meme.display_name.not_like("%\\%", escape="!"),
        Meme.display_name.not_in((".", "..", ".staging", ".quarantine")),
        Meme.display_name.not_like("%.png"),
        Meme.display_name.not_like("%.jpg"),
        Meme.display_name.not_like("%.jpeg"),
        Meme.display_name.not_like("%.gif"),
        Meme.display_name.op("~")(r"^[^[:cntrl:]]+$"),
    )


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

    def by_content(self, sha256: str, extension: str, *, for_update: bool = False) -> Meme | None:
        """按当前 scope 的最终图片 SHA 和扩展名查询唯一 Meme。"""
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise DatabaseError("sha256_invalid")
        try:
            normalized_extension = normalize_extension(extension)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        statement = select(Meme).where(
            Meme.scope_id == self.scope.scope_id,
            Meme.sha256 == sha256.lower(),
            Meme.extension == normalized_extension,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def find_by_content(self, sha256: str, extension: str, *, for_update: bool = False) -> Meme | None:
        """返回 `by_content` 的语义别名，供上传和合集导入统一调用。"""
        return self.by_content(sha256, extension, for_update=for_update)

    def list(self, *, search: str | None = None, page: int = 1, page_size: int = 200, sort: ImageSort = "name_asc", condition: ColumnElement[bool] | None = None) -> list[Meme]:
        """按名称和可选状态条件筛选当前 scope 图片，排序后返回指定页。"""
        if sort == "name_asc":
            ordering = (func.lower(Meme.display_name).asc(), Meme.id.asc())
        elif sort == "updated_desc":
            ordering = (Meme.updated_at.desc(), Meme.id.desc())
        else:
            raise DatabaseError("invalid_image_sort", "图片排序方式必须为 name_asc 或 updated_desc")
        statement = select(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.display_name.ilike(f"%{search}%"))
        if condition is not None:
            statement = statement.where(condition)
        statement = statement.order_by(*ordering).offset(max(0, page - 1) * page_size).limit(max(1, min(page_size, 200)))
        return list(self.session.scalars(statement))

    def count(self, *, search: str | None = None, condition: ColumnElement[bool] | None = None) -> int:
        """返回名称和状态条件筛选后的可见图片数量，供图片库分页使用。"""
        statement = select(func.count()).select_from(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.display_name.ilike(f"%{search}%"))
        if condition is not None:
            statement = statement.where(condition)
        return int(self.session.scalar(statement) or 0)

    def list_all(self, *, search: str | None = None) -> list[Meme]:
        """供缓存生成等内部批处理读取当前 scope 全量 Meme；公共列表仍使用分页。"""
        statement = select(Meme).where(*self._visible_predicate())
        if search:
            statement = statement.where(Meme.display_name.ilike(f"%{search}%"))
        return list(self.session.scalars(statement.order_by(Meme.display_name.asc(), Meme.id.asc())))

    def list_rebuild_page(self, *, after_id: UUID | None = None, limit: int = 200) -> list[Meme]:
        """按稳定 UUID keyset 读取检索重建的一页 Meme。

        输入是上一次返回的最后一个 UUID 和有界页大小，输出只包含当前 scope 的
        结构合法、状态允许索引的数据库行。调用场景是离线检索向量重建；它不
        读取文件，也不把整个 scope 装入单次查询结果。
        """
        try:
            normalized_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError) as exc:
            raise DatabaseError("migration_count_invalid") from exc
        statement = select(Meme).where(
            Meme.scope_id == self.scope.scope_id,
            Meme.context_status.in_(("pending", "partial", "ready")),
            _durable_meme_clause(),
        )
        if after_id is not None:
            statement = statement.where(Meme.id > after_id)
        return list(self.session.scalars(statement.order_by(Meme.id.asc()).limit(normalized_limit)))

    def _visible_predicate(self) -> tuple[Any, ...]:
        """返回列表与 count 共用的结构可见性条件。

        结构性脏记录在数据库层隐藏；原图是否存在由媒体消费路径单独严格校验。
        """
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        return (Meme.scope_id == self.scope.scope_id, ~active_operation, _durable_meme_clause())

    def create(self, *, storage_key: str, extension: str, size_bytes: int, sha256: str, context: dict[str, Any], provenance: dict[str, Any], display_name: str | None = None, status: str = "pending", meme_id: UUID | None = None, extensions: dict[str, Any] | None = None, name_source: str = "unknown") -> Meme:
        """创建稳定 UUID Meme、独立展示名称和初始语境记录。"""
        try:
            validate_business_storage_key(storage_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        try:
            normalized_extension = normalize_extension(extension)
            if Path(storage_key).suffix.lower() != normalized_extension:
                raise ValueError("extension_storage_key_mismatch")
            normalized_name = normalize_display_name(display_name if display_name is not None else Path(storage_key).stem)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            raise DatabaseError("size_bytes_invalid")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
            raise DatabaseError("sha256_invalid")
        normalized_sha256 = sha256.lower()
        if storage_key != f"{normalized_sha256}{normalized_extension}":
            raise DatabaseError("content_addressed_key_required")
        from backend.metadata import MemeContext

        parsed_context = MemeContext.model_validate(context)
        if name_source not in {"uploaded", "manual", "automatic", "unknown"}:
            raise DatabaseError("name_source_invalid")
        text_input = build_text_input(normalized_name, parsed_context, status)
        record = Meme(id=meme_id or uuid.uuid4(), scope_id=self.scope.scope_id, storage_key=storage_key, display_name=normalized_name, name_source=name_source, text_input_status=text_input.status, extension=normalized_extension, size_bytes=size_bytes, sha256=normalized_sha256, context_status=status, search_metadata_hash=text_input.fingerprint, meme_context=parsed_context.model_dump(mode="json", exclude_none=False), provenance=provenance, extensions=extensions or {}, revision=1)
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
            ensure_task_deadline(task.payload or {}, now)
        if expected_revision is not None and record.revision != expected_revision:
            raise DatabaseError("target_changed")
        if expected_sha256 is not None and record.sha256 != expected_sha256:
            raise DatabaseError("target_changed")
        from backend.metadata import MemeContext

        parsed_context = MemeContext.model_validate(context)
        normalized_context = parsed_context.model_dump(mode="json", exclude_none=False)
        context_changed = record.meme_context != normalized_context or record.context_status != status
        record.meme_context = normalized_context
        text_input = build_text_input(record.display_name, parsed_context, status)
        record.search_metadata_hash = text_input.fingerprint
        record.text_input_status = text_input.status
        record.provenance = provenance
        record.context_status = status
        if context_changed:
            record.revision += 1
            from backend.text_updates import enqueue_text_update

            enqueue_text_update(self.session, record, source_task=task if claim is not None else None)
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def update_display_name(
        self,
        meme_id: UUID | str,
        display_name: str,
        *,
        expected_display_name: str | None = None,
        expected_revision: int | None = None,
        expected_sha256: str | None = None,
        claim: tuple[str, int, str] | None = None,
        name_source: str = "manual",
        expected_name_source: str | None = None,
    ) -> Meme:
        """以可选 CAS/Task claim 只更新展示名称，不触碰物理身份或 revision。"""
        try:
            normalized_name = normalize_display_name(display_name)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        record = self.get(meme_id, for_update=True)
        if record is None:
            raise DatabaseError("meme_not_found")
        if claim is not None:
            task_id, claim_generation, owner = claim
            now = utcnow()
            task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
            if task is None or task.status != "running" or task.claim_generation != claim_generation or task.lease_owner != owner or task.lease_expires_at is None or task.lease_expires_at <= now:
                raise DatabaseError("claim_expired")
        if expected_display_name is not None and record.display_name != expected_display_name:
            raise DatabaseError("target_changed")
        if expected_revision is not None and record.revision != expected_revision:
            raise DatabaseError("target_changed")
        if expected_sha256 is not None and record.sha256.lower() != expected_sha256.lower():
            raise DatabaseError("target_changed")
        if name_source not in {"manual", "automatic"}:
            raise DatabaseError("name_source_invalid")
        if expected_name_source is not None and record.name_source != expected_name_source:
            raise DatabaseError("target_changed")
        if name_source == "automatic" and record.name_source in {"manual", "unknown"}:
            raise DatabaseError("name_protected")
        previous_hash = record.search_metadata_hash
        record.display_name = normalized_name
        record.name_source = name_source
        text_input = build_text_input(normalized_name, record.meme_context, record.context_status)
        record.search_metadata_hash = text_input.fingerprint
        record.text_input_status = text_input.status
        if previous_hash != text_input.fingerprint:
            from backend.text_updates import enqueue_text_update

            enqueue_text_update(self.session, record, source_task=task if claim is not None else None)
        record.updated_at = utcnow()
        self.session.flush()
        return record

    @staticmethod
    def saved_filename(record: Meme) -> str:
        """返回 Meme 的完整用户文件名，不使用物理 storage key。"""
        return saved_filename(record.display_name, record.extension)

    def delete(self, meme_id: UUID | str) -> Meme:
        """删除当前 scope 的 Meme 记录；调用方负责先完成文件隔离。"""
        record = self.get(meme_id, for_update=True)
        if record is None:
            raise DatabaseError("meme_not_found")
        self.session.delete(record)
        self.session.flush()
        return record
