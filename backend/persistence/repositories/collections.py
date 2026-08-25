"""Meme 合集及成员关系的 scope 绑定持久化访问。

该模块位于持久化 Repository 边界，只负责合集关系的数据库 CRUD、成员批量和导出
快照；Meme 文件的移动与删除仍由 StorageCoordinator 负责。
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.persistence.engine import DatabaseError
from backend.persistence.models import Meme, MemeCollection, MemeCollectionItem, ScopeContext, StorageOperation, utcnow


class CollectionRepository:
    """绑定 Session 与 scope 的合集 CRUD、成员批量和分页 repository。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    @staticmethod
    def normalize_name(name: str) -> str:
        """去除合集名称首尾空白并限制有效长度。"""
        normalized = name.strip() if isinstance(name, str) else ""
        if not 1 <= len(normalized) <= 100:
            raise DatabaseError("invalid_collection_name")
        return normalized

    def get(self, collection_id: UUID | str, *, for_update: bool = False) -> MemeCollection | None:
        """读取当前 scope 合集，跨 scope 或非法 UUID 均视为不存在。"""
        try:
            identifier = UUID(str(collection_id))
        except (ValueError, TypeError):
            return None
        statement = select(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id, MemeCollection.id == identifier)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def by_name(self, name: str) -> MemeCollection | None:
        """按规范名称查询当前 scope 合集，供导入预检避免业务副作用。"""
        normalized = self.normalize_name(name)
        return self.session.scalar(select(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id, MemeCollection.name == normalized))

    def count(self) -> int:
        """统计当前 scope 合集数量。"""
        return int(self.session.scalar(select(func.count()).select_from(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id)) or 0)

    def list(self, *, page: int = 1, page_size: int = 50) -> list[MemeCollection]:
        """按更新时间降序和 UUID 稳定分页列出合集。"""
        return list(self.session.scalars(select(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id).order_by(MemeCollection.updated_at.desc(), MemeCollection.id.desc()).offset(max(0, page - 1) * page_size).limit(max(1, min(page_size, 100)))))

    def create(self, name: str) -> MemeCollection:
        """创建当前 scope 的规范化合集，重名映射为稳定冲突错误。"""
        row = MemeCollection(scope_id=self.scope.scope_id, name=self.normalize_name(name))
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DatabaseError("collection_exists") from exc
        return row

    def rename(self, collection_id: UUID | str, name: str) -> MemeCollection:
        """重命名当前 scope 合集并保留成员关系。"""
        row = self.get(collection_id, for_update=True)
        if row is None:
            raise DatabaseError("collection_not_found")
        row.name = self.normalize_name(name)
        row.updated_at = utcnow()
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DatabaseError("collection_exists") from exc
        return row

    def delete(self, collection_id: UUID | str) -> None:
        """删除合集及成员关系，不触碰 Meme 或图片文件。"""
        row = self.get(collection_id, for_update=True)
        if row is None:
            raise DatabaseError("collection_not_found")
        self.session.delete(row)
        self.session.flush()

    def member_count(self, collection_id: UUID | str) -> int:
        """统计合集成员数量。"""
        return int(self.session.scalar(select(func.count()).select_from(MemeCollectionItem).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == UUID(str(collection_id)))) or 0)

    def members(self, collection_id: UUID | str, *, page: int = 1, page_size: int = 50) -> list[tuple[MemeCollectionItem, Meme]]:
        """按加入时间和 Meme UUID 稳定分页返回成员及当前 Meme。"""
        statement = select(MemeCollectionItem, Meme).join(Meme, (Meme.scope_id == MemeCollectionItem.scope_id) & (Meme.id == MemeCollectionItem.meme_id)).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == UUID(str(collection_id))).order_by(MemeCollectionItem.added_at.asc(), MemeCollectionItem.meme_id.asc()).offset(max(0, page - 1) * page_size).limit(max(1, min(page_size, 100)))
        return list(self.session.execute(statement).all())

    def members_for_export(self, collection_id: UUID | str) -> list[Meme]:
        """按加入顺序读取合集全部可导出 Meme，并排除活动存储操作。

        导出需要一次稳定的非分页成员快照；``prepared``/``file_applied`` 期间的 Meme
        可能正处于跨数据库与文件系统的中间态，必须等待恢复后再进入归档。
        """
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        statement = (
            select(Meme)
            .join(MemeCollectionItem, (Meme.scope_id == MemeCollectionItem.scope_id) & (Meme.id == MemeCollectionItem.meme_id))
            .where(
                MemeCollectionItem.scope_id == self.scope.scope_id,
                MemeCollectionItem.collection_id == UUID(str(collection_id)),
                ~active_operation,
            )
            .order_by(MemeCollectionItem.added_at.asc(), MemeCollectionItem.meme_id.asc())
        )
        return list(self.session.scalars(statement))

    def add_members(self, collection_id: UUID | str, meme_ids: Sequence[UUID | str]) -> tuple[int, int, int]:
        """原子批量加入 Meme，重复请求幂等；任一 Meme 越界则整批失败。"""
        collection = self.get(collection_id, for_update=True)
        if collection is None:
            raise DatabaseError("collection_not_found")
        unique_ids: list[UUID] = []
        for value in meme_ids:
            try:
                identifier = UUID(str(value))
            except (ValueError, TypeError) as exc:
                raise DatabaseError("meme_not_found") from exc
            if identifier not in unique_ids:
                unique_ids.append(identifier)
        if not unique_ids:
            raise DatabaseError("empty_members")
        found = set(self.session.scalars(select(Meme.id).where(Meme.scope_id == self.scope.scope_id, Meme.id.in_(unique_ids))))
        if found != set(unique_ids):
            raise DatabaseError("meme_not_found")
        existing = set(self.session.scalars(select(MemeCollectionItem.meme_id).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == collection.id, MemeCollectionItem.meme_id.in_(unique_ids))))
        for meme_id in unique_ids:
            if meme_id not in existing:
                self.session.add(MemeCollectionItem(scope_id=self.scope.scope_id, collection_id=collection.id, meme_id=meme_id))
        self.session.flush()
        total = self.member_count(collection.id)
        return len(set(unique_ids) - existing), len(existing), total

    def remove_member(self, collection_id: UUID | str, meme_id: UUID | str) -> int:
        """幂等移除单个成员并返回最终成员数。"""
        collection = self.get(collection_id)
        if collection is None:
            raise DatabaseError("collection_not_found")
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError) as exc:
            raise DatabaseError("meme_not_found") from exc
        self.session.execute(delete(MemeCollectionItem).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == collection.id, MemeCollectionItem.meme_id == identifier))
        self.session.flush()
        return self.member_count(collection.id)

    def cover(self, collection_id: UUID | str) -> Meme | None:
        """返回按加入顺序最早且仍存在的 Meme。"""
        row = self.session.scalar(select(Meme).join(MemeCollectionItem, (Meme.scope_id == MemeCollectionItem.scope_id) & (Meme.id == MemeCollectionItem.meme_id)).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == UUID(str(collection_id))).order_by(MemeCollectionItem.added_at.asc(), MemeCollectionItem.meme_id.asc()))
        return row
