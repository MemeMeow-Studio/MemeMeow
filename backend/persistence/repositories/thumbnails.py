"""缩略图派生事实的 scope-bound Repository。

该模块只管理数据库中的源版本、输出身份、状态和诊断；文件写入与校验由派生
服务及其独立 BlobStore 负责，避免 Repository 绕过受控存储边界。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.persistence.engine import DatabaseError
from backend.persistence.models import DerivedImageThumbnail, Meme, ScopeContext, utcnow


THUMBNAIL_STATUSES = frozenset({"available", "pending", "failed", "stale"})


def _uuid(value: UUID | str) -> UUID:
    """将 Meme 标识转换为 UUID，非法值统一映射为资源不存在。"""
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise DatabaseError("meme_not_found") from exc


def _validate_source(source_sha256: str, source_size_bytes: int) -> None:
    """校验派生绑定使用的源内容版本字段。"""
    if not isinstance(source_sha256, str) or len(source_sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in source_sha256):
        raise DatabaseError("thumbnail_source_invalid")
    if not isinstance(source_size_bytes, int) or isinstance(source_size_bytes, bool) or source_size_bytes < 0:
        raise DatabaseError("thumbnail_source_invalid")


class DerivedThumbnailRepository:
    """按 scope 管理原图版本对应的缩略图事实。"""

    def __init__(self, session: Session, scope: ScopeContext):
        """绑定共享事务 Session 和不可变 scope。"""
        self.session = session
        self.scope = scope

    def get(
        self,
        meme_id: UUID | str,
        source_sha256: str,
        source_size_bytes: int,
        profile: str,
        *,
        for_update: bool = False,
    ) -> DerivedImageThumbnail | None:
        """按完整派生身份读取当前 scope 的一条事实。"""
        _validate_source(source_sha256, source_size_bytes)
        statement = select(DerivedImageThumbnail).where(
            DerivedImageThumbnail.scope_id == self.scope.scope_id,
            DerivedImageThumbnail.meme_id == _uuid(meme_id),
            DerivedImageThumbnail.source_sha256 == source_sha256.lower(),
            DerivedImageThumbnail.source_size_bytes == source_size_bytes,
            DerivedImageThumbnail.profile == profile,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def current(self, meme: Meme, profile: str, *, for_update: bool = False) -> DerivedImageThumbnail | None:
        """按数据库 Meme 当前 SHA/大小读取同 profile 派生事实。"""
        return self.get(meme.id, meme.sha256, meme.size_bytes, profile, for_update=for_update)

    def ensure_pending(
        self,
        meme: Meme,
        profile: str,
        *,
        reset_failed: bool = False,
    ) -> DerivedImageThumbnail:
        """幂等创建当前源版本的 pending 事实，并使旧版本进入 stale。"""
        # 先锁住父 Meme，再检查派生行。这样删除事务不能在 FK 检查和首次插入之间
        # 删除父行；SAVEPOINT 仍保留为数据库约束或历史数据竞态的最后一道防线。
        authoritative = self.session.scalar(
            select(Meme)
            .where(Meme.scope_id == self.scope.scope_id, Meme.id == _uuid(meme.id))
            .with_for_update()
        )
        if authoritative is None:
            raise DatabaseError("meme_not_found")
        source_sha256 = str(authoritative.sha256).lower()
        source_size_bytes = authoritative.size_bytes
        current = self.get(authoritative.id, source_sha256, source_size_bytes, profile, for_update=True)
        old_rows = list(
            self.session.scalars(
                select(DerivedImageThumbnail)
                .where(
                    DerivedImageThumbnail.scope_id == self.scope.scope_id,
                    DerivedImageThumbnail.meme_id == authoritative.id,
                    DerivedImageThumbnail.profile == profile,
                    DerivedImageThumbnail.source_sha256 != source_sha256,
                )
                .with_for_update()
            )
        )
        for row in old_rows:
            if row.status != "stale":
                row.status = "stale"
                row.diagnostic = {"error": "source_version_changed"}
                row.updated_at = utcnow()
        if current is None:
            current = DerivedImageThumbnail(
                scope_id=self.scope.scope_id,
                meme_id=authoritative.id,
                source_sha256=source_sha256,
                source_size_bytes=source_size_bytes,
                profile=profile,
                status="pending",
            )
            try:
                with self.session.begin_nested():
                    self.session.add(current)
                    self.session.flush()
            except IntegrityError as exc:
                # 并发首次投影/任务/生成可能仍在历史事务路径中竞争同一
                # 复合主键；唯一冲突回滚到 SAVEPOINT 后重读权威行即可复用。
                current = self.get(authoritative.id, source_sha256, source_size_bytes, profile, for_update=True)
                if current is None:
                    parent = self.session.scalar(
                        select(Meme.id).where(Meme.scope_id == self.scope.scope_id, Meme.id == authoritative.id)
                    )
                    if parent is None:
                        raise DatabaseError("meme_not_found") from exc
                    raise DatabaseError("thumbnail_pending_conflict") from exc
            return current
        if reset_failed and current.status in {"failed", "stale"}:
            current.status = "pending"
            current.output_key = None
            current.output_sha256 = None
            current.output_size_bytes = None
            current.width = None
            current.height = None
            current.media_type = None
            current.diagnostic = None
            current.generated_at = None
            current.updated_at = utcnow()
            self.session.flush()
        return current

    def list_current(self, memes: Iterable[Meme], profile: str) -> dict[UUID, DerivedImageThumbnail]:
        """批量读取一页 Meme 的当前 profile 派生事实。"""
        values = list(memes)
        if not values:
            return {}
        ids = [item.id for item in values]
        rows = self.session.scalars(
            select(DerivedImageThumbnail).where(
                DerivedImageThumbnail.scope_id == self.scope.scope_id,
                DerivedImageThumbnail.meme_id.in_(ids),
                DerivedImageThumbnail.profile == profile,
            )
        )
        expected = {(item.id, str(item.sha256).lower(), item.size_bytes) for item in values}
        return {
            row.meme_id: row
            for row in rows
            if (row.meme_id, str(row.source_sha256).lower(), row.source_size_bytes) in expected
        }

    def mark_pending(self, meme: Meme, profile: str) -> DerivedImageThumbnail:
        """把同源的失败或过期事实恢复为可重试的 pending。"""
        return self.ensure_pending(meme, profile, reset_failed=True)

    def mark_available(
        self,
        meme: Meme,
        profile: str,
        *,
        output_key: str,
        output_sha256: str,
        output_size_bytes: int,
        width: int,
        height: int,
        media_type: str,
        generated_at: datetime | None = None,
    ) -> DerivedImageThumbnail:
        """在文件已原子落位后提交可访问的输出身份。"""
        if not isinstance(output_key, str) or not output_key or not isinstance(output_sha256, str) or len(output_sha256) != 64:
            raise DatabaseError("thumbnail_output_invalid")
        if output_size_bytes < 1 or width < 1 or height < 1:
            raise DatabaseError("thumbnail_output_invalid")
        row = self.ensure_pending(meme, profile)
        if (
            row.status == "available"
            and row.output_key == output_key
            and str(row.output_sha256).lower() == output_sha256.lower()
            and row.output_size_bytes == output_size_bytes
            and row.width == width
            and row.height == height
            and row.media_type == media_type
        ):
            # 同一确定输出已被另一 Worker 提交时，保留先完成的成功事实，避免
            # 后到者重复刷新时间或在异常路径中把 available 降级。
            return row
        row.output_key = output_key
        row.output_sha256 = output_sha256.lower()
        row.output_size_bytes = output_size_bytes
        row.width = width
        row.height = height
        row.media_type = media_type
        row.status = "available"
        row.diagnostic = None
        row.generated_at = generated_at or utcnow()
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def mark_failed(self, meme: Meme, profile: str, diagnostic: dict[str, Any]) -> DerivedImageThumbnail:
        """保存可诊断失败状态并清除不可访问的部分输出。"""
        row = self.ensure_pending(meme, profile)
        row.status = "failed"
        row.output_key = None
        row.output_sha256 = None
        row.output_size_bytes = None
        row.width = None
        row.height = None
        row.media_type = None
        row.diagnostic = dict(diagnostic)
        row.generated_at = None
        row.updated_at = utcnow()
        self.session.flush()
        return row

    def mark_stale(self, meme_id: UUID | str, profile: str, *, diagnostic: dict[str, Any] | None = None) -> int:
        """阻断同一 Meme profile 的全部旧派生访问。"""
        rows = list(
            self.session.scalars(
                select(DerivedImageThumbnail)
                .where(
                    DerivedImageThumbnail.scope_id == self.scope.scope_id,
                    DerivedImageThumbnail.meme_id == _uuid(meme_id),
                    DerivedImageThumbnail.profile == profile,
                )
                .with_for_update()
            )
        )
        for row in rows:
            row.status = "stale"
            row.diagnostic = dict(diagnostic or {"error": "source_unavailable"})
            row.updated_at = utcnow()
        self.session.flush()
        return len(rows)

    def delete_for_meme(self, meme_id: UUID | str) -> int:
        """删除已完成 Meme 的派生数据库事实，文件清理由服务负责。"""
        rows = list(
            self.session.scalars(
                select(DerivedImageThumbnail)
                .where(
                    DerivedImageThumbnail.scope_id == self.scope.scope_id,
                    DerivedImageThumbnail.meme_id == _uuid(meme_id),
                )
                .with_for_update()
            )
        )
        for row in rows:
            self.session.delete(row)
        self.session.flush()
        return len(rows)


__all__ = ["DerivedThumbnailRepository", "THUMBNAIL_STATUSES"]
