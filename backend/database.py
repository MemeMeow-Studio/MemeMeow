"""PostgreSQL 权威存储、scope 边界和同步数据环境。

该模块位于 API、任务 Worker 与领域服务之间。所有结构化业务数据通过这里创建的
SQLAlchemy Session 和已绑定 scope 的 repository 访问；图片字节仍由 BlobStore 保存。
"""

from __future__ import annotations

import hashlib
import json
import os
import errno
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Sequence
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    create_engine,
    delete,
    event,
    func,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

from backend.paths import SUPPORTED_EXTENSIONS, validate_business_storage_key


EMBEDDING_DIMENSIONS = 1024
SCOPE_LOCAL = "local"
UTC = timezone.utc
# 当前代码要求的 Alembic head；数据库初始化脚本会显式传入同一 revision。
CURRENT_SCHEMA_REVISION = "0004_meme_collections"


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供数据库字段和租约计算共用。"""
    return datetime.now(UTC)


class DatabaseError(RuntimeError):
    """数据库边界错误，携带不会泄露连接凭据的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


class ScopeContext:
    """不可为空的数据范围上下文；公共开源适配层固定使用 ``local``。"""

    def __init__(self, scope_id: str):
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("scope_required")
        self.scope_id = scope_id.strip()

    def __repr__(self) -> str:
        return f"ScopeContext({self.scope_id!r})"


class Base(DeclarativeBase):
    """应用全部 PostgreSQL 表的声明式基类。"""


class Scope(Base):
    """数据范围和不可变文件命名空间。"""

    __tablename__ = "scopes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    storage_namespace: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InstallationState(Base):
    """单实例安装门禁；不表示用户或账户。"""

    __tablename__ = "installation_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Meme(Base):
    """稳定 UUID Meme 身份及版本化结构化语境。"""

    __tablename__ = "memes"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(128), ForeignKey("scopes.id", ondelete="CASCADE"), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    context_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    meme_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope_id", "id", name="uq_memes_scope_id"),
        UniqueConstraint("scope_id", "storage_key", name="uq_memes_scope_storage"),
        CheckConstraint("size_bytes >= 0", name="ck_memes_size_nonnegative"),
        CheckConstraint("context_status IN ('pending','partial','ready','repair_required')", name="ck_memes_context_status"),
        CheckConstraint("storage_key <> '' AND storage_key NOT IN ('.', '..', '.staging', '.quarantine') AND position('/' in storage_key) = 0 AND position(chr(92) in storage_key) = 0 AND storage_key !~ '[[:cntrl:]]'", name="ck_memes_storage_key_flat"),
    )


class MemeCollection(Base):
    """当前 scope 内的逻辑 Meme 合集，不拥有图片文件。"""

    __tablename__ = "meme_collections"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        UniqueConstraint("scope_id", "id", name="uq_meme_collections_scope_id"),
        UniqueConstraint("scope_id", "name", name="uq_meme_collections_scope_name"),
    )


class MemeCollectionItem(Base):
    """合集与 Meme 的 scope-safe 多对多成员关系。"""

    __tablename__ = "meme_collection_items"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    collection_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "collection_id"], ["meme_collections.scope_id", "meme_collections.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        Index("ix_meme_collection_items_page", "scope_id", "collection_id", "added_at", "meme_id"),
    )


class StorageOperation(Base):
    """数据库与文件系统之间可恢复的上传、重命名和删除意图。"""

    __tablename__ = "storage_operations"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    meme_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_token: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    source_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    target_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    staging_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    before_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        UniqueConstraint("scope_id", "meme_id", "operation_token", name="uq_storage_operation_token"),
        CheckConstraint("operation_type IN ('upload','rename','delete')", name="ck_storage_operation_type"),
        CheckConstraint("status IN ('prepared','file_applied','completed','compensated','blocked')", name="ck_storage_operation_status"),
    )


class SearchGeneration(Base):
    """按 scope 与模型分代构建的 pgvector 索引。"""

    __tablename__ = "search_generations"
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=EMBEDDING_DIMENSIONS)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="building")
    source_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        UniqueConstraint("scope_id", "id", name="uq_generations_scope_id"),
        UniqueConstraint("scope_id", "model", "id", name="uq_generations_scope_model_id"),
        CheckConstraint(f"dimensions = {EMBEDDING_DIMENSIONS}", name="ck_generation_dimensions"),
        CheckConstraint("status IN ('building','ready','active','failed','retired')", name="ck_generation_status"),
    )


class SearchHead(Base):
    """当前 scope/model 对外可见的 active generation 指针。"""

    __tablename__ = "search_heads"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    active_generation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    active_generation_model: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "active_generation_model", "active_generation_id"], ["search_generations.scope_id", "search_generations.model", "search_generations.id"]),
    )


class MemeEmbedding(Base):
    """generation 中单张 Meme 的固定维度向量及源快照。"""

    __tablename__ = "meme_embeddings"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    semantic_document: Mapped[str] = mapped_column(String(6000), nullable=False)
    semantic_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    meme_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    item_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "generation_id"], ["search_generations.scope_id", "search_generations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        CheckConstraint("item_status IN ('pending','ready','failed')", name="ck_embedding_item_status"),
    )


class Task(Base):
    """字符串任务 ID、状态、去重键和 Worker 租约。"""

    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    lane: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[float | None] = mapped_column(nullable=True, default=0.0)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    settings_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('queued','running','succeeded','failed')", name="ck_task_status"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_task_attempts"),
        UniqueConstraint("scope_id", "id", name="uq_task_scope_id"),
    )




class TaskBatch(Base):
    """批次成员与索引刷新收束状态。"""

    __tablename__ = "task_batches"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    finalizer_state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        CheckConstraint("finalizer_state IN ('pending','submitted','complete')", name="ck_batch_finalizer_state"),
    )


class TaskBatchItem(Base):
    """批次到任务的 scope-safe 关联。"""

    __tablename__ = "task_batch_items"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "batch_id"], ["task_batches.scope_id", "task_batches.batch_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
    )


class TaskLaneSlot(Base):
    """数据库全局 Agent lane 槽位和租约。"""

    __tablename__ = "task_lane_slots"
    lane: Mapped[str] = mapped_column(String(64), primary_key=True)
    slot_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_scope_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["task_scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["task_scope_id", "task_id"], ["tasks.scope_id", "tasks.id"]),
        UniqueConstraint("task_scope_id", "task_id", name="uq_task_lane_slot_task"),
        CheckConstraint("slot_number >= 0", name="ck_slot_number"),
    )


Index("ix_tasks_active_dedupe", Task.scope_id, Task.task_type, Task.dedupe_key, unique=True, postgresql_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"))
Index("ix_tasks_claimable", Task.scope_id, Task.status, Task.available_at, Task.lease_expires_at)
Index("ix_embeddings_generation_status", MemeEmbedding.scope_id, MemeEmbedding.generation_id, MemeEmbedding.item_status)
Index("uq_search_generation_building", SearchGeneration.scope_id, SearchGeneration.model, unique=True, postgresql_where=text("status = 'building'"))
Index("ix_storage_operations_recovery", StorageOperation.scope_id, StorageOperation.status, StorageOperation.updated_at)
Index("uq_storage_operation_active", StorageOperation.scope_id, StorageOperation.meme_id, unique=True, postgresql_where=text("status IN ('prepared','file_applied') AND meme_id IS NOT NULL"))


def database_url_from_env() -> str:
    """读取 PostgreSQL URL；明确拒绝 SQLite 等非目标数据库。"""
    value = os.getenv("MEMEMEOW_DATABASE_URL", "postgresql+psycopg://mememeow:mememeow@127.0.0.1:5434/mememeow")
    if not value.startswith("postgresql"):
        raise DatabaseError("postgresql_required")
    return value


def create_engine_for_url(url: str | None = None, **kwargs: Any) -> Engine:
    """创建进程级 SQLAlchemy Engine 与连接池。"""
    try:
        engine = create_engine(url or database_url_from_env(), pool_pre_ping=True, future=True, **kwargs)
    except SQLAlchemyError as exc:
        raise DatabaseError("database_engine_failed") from exc
    return engine


def create_engine_for_settings(settings: Any) -> Engine:
    """按 Settings 的池参数创建共享 Engine。"""
    return create_engine_for_url(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout,
        pool_recycle=settings.database_pool_recycle,
    )


def check_database(engine: Engine, *, expected_revision: str | None = None) -> dict[str, Any]:
    """执行连接、pgvector、Alembic revision 和安装标记健康检查。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            vector_enabled = bool(connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar())
            revision = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
            installed = connection.execute(text("SELECT schema_revision FROM installation_state WHERE key='local'")).scalar()
    except SQLAlchemyError as exc:
        raise DatabaseError("database_unavailable") from exc
    if not vector_enabled:
        raise DatabaseError("pgvector_missing")
    if revision is None:
        raise DatabaseError("schema_revision_missing")
    if expected_revision and revision != expected_revision:
        raise DatabaseError("schema_revision_mismatch")
    if installed is None:
        raise DatabaseError("installation_required")
    if installed != revision or (expected_revision and installed != expected_revision):
        raise DatabaseError("installation_revision_mismatch")
    return {"revision": revision, "installed": installed, "pgvector": True}


def initialize_local(engine: Engine, *, revision: str = CURRENT_SCHEMA_REVISION) -> None:
    """幂等创建 ``local`` scope 与安装标记，不扫描图片、不创建账户。"""
    try:
        with Session(engine) as session, session.begin():
            scope = session.scalar(select(Scope).where(Scope.id == SCOPE_LOCAL))
            if scope is None:
                session.add(Scope(id=SCOPE_LOCAL))
                session.flush()
            marker = session.get(InstallationState, "local")
            if marker is None:
                session.add(InstallationState(key="local", schema_revision=revision))
            elif marker.schema_revision != revision:
                raise DatabaseError("installation_conflict")
    except IntegrityError as exc:
        raise DatabaseError("installation_conflict") from exc


class UnitOfWork:
    """同步事务边界；成功退出提交，异常退出回滚并关闭 Session。"""

    def __init__(self, factory: sessionmaker[Session], scope: ScopeContext):
        if not isinstance(scope, ScopeContext):
            raise ValueError("scope_required")
        self.scope = scope
        self.session = factory()

    def __enter__(self) -> "UnitOfWork":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type:
                self.session.rollback()
            else:
                self.session.commit()
        finally:
            self.session.close()

    def rollback(self) -> None:
        """显式回滚当前事务，供跨存储补偿路径使用。"""
        self.session.rollback()


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
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        statement = select(Meme).where(Meme.scope_id == self.scope.scope_id, ~active_operation)
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        statement = statement.order_by(Meme.storage_key.asc(), Meme.id.asc()).offset(max(0, page - 1) * page_size).limit(max(1, min(page_size, 200)))
        return list(self.session.scalars(statement))

    def count(self, *, search: str | None = None) -> int:
        """返回当前 scope 的可见 Meme 数量，筛选在数据库执行。"""
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        statement = select(func.count()).select_from(Meme).where(Meme.scope_id == self.scope.scope_id, ~active_operation)
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        return int(self.session.scalar(statement) or 0)

    def list_all(self, *, search: str | None = None) -> list[Meme]:
        """供缓存生成等内部批处理读取当前 scope 全量 Meme；公共列表仍使用分页。"""
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        statement = select(Meme).where(Meme.scope_id == self.scope.scope_id, ~active_operation)
        if search:
            statement = statement.where(Meme.storage_key.ilike(f"%{search}%"))
        return list(self.session.scalars(statement.order_by(Meme.storage_key.asc(), Meme.id.asc())))

    def create(self, *, storage_key: str, extension: str, size_bytes: int, sha256: str, context: dict[str, Any], provenance: dict[str, Any], status: str = "pending", meme_id: UUID | None = None, extensions: dict[str, Any] | None = None) -> Meme:
        """创建稳定 UUID Meme 和初始语境记录。"""
        try:
            validate_business_storage_key(storage_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        record = Meme(id=meme_id or uuid.uuid4(), scope_id=self.scope.scope_id, storage_key=storage_key, extension=extension.lower(), size_bytes=size_bytes, sha256=sha256, context_status=status, meme_context=context, provenance=provenance, extensions=extensions or {}, revision=1)
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
        record.meme_context = context
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

    @staticmethod
    def _uuid(value: UUID | str, code: str = "collection_not_found") -> UUID:
        """解析边界 UUID；非法值按资源不存在处理，避免泄露数据库异常。"""
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise DatabaseError(code) from exc

    def get(self, collection_id: UUID | str, *, for_update: bool = False) -> MemeCollection | None:
        """读取当前 scope 合集，跨 scope 或非法 UUID 均视为不存在。"""
        try:
            identifier = self._uuid(collection_id)
        except DatabaseError:
            return None
        statement = select(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id, MemeCollection.id == identifier)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def count(self) -> int:
        """统计当前 scope 合集数量。"""
        return int(self.session.scalar(select(func.count()).select_from(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id)) or 0)

    def list(self, *, page: int = 1, page_size: int = 50) -> list[MemeCollection]:
        """按更新时间降序和 UUID 稳定分页列出合集。"""
        size = max(1, min(page_size, 100))
        return list(self.session.scalars(select(MemeCollection).where(MemeCollection.scope_id == self.scope.scope_id).order_by(MemeCollection.updated_at.desc(), MemeCollection.id.desc()).offset(max(0, page - 1) * size).limit(size)))

    def create(self, name: str) -> MemeCollection:
        """创建当前 scope 的规范化合集，重名映射为稳定冲突错误。"""
        row = MemeCollection(scope_id=self.scope.scope_id, name=self.normalize_name(name))
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
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
            with self.session.begin_nested():
                self.session.flush()
        except IntegrityError as exc:
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
        identifier = self._uuid(collection_id)
        return int(self.session.scalar(select(func.count()).select_from(MemeCollectionItem).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == identifier)) or 0)

    def members(self, collection_id: UUID | str, *, page: int = 1, page_size: int = 50) -> list[tuple[MemeCollectionItem, Meme]]:
        """按加入时间和 Meme UUID 稳定分页返回成员及当前 Meme。"""
        identifier = self._uuid(collection_id)
        size = max(1, min(page_size, 100))
        statement = (
            select(MemeCollectionItem, Meme)
            .join(Meme, (Meme.scope_id == MemeCollectionItem.scope_id) & (Meme.id == MemeCollectionItem.meme_id))
            .where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == identifier)
            .order_by(MemeCollectionItem.added_at.asc(), MemeCollectionItem.meme_id.asc())
            .offset(max(0, page - 1) * size)
            .limit(size)
        )
        return list(self.session.execute(statement).all())

    def add_members(self, collection_id: UUID | str, meme_ids: Sequence[UUID | str]) -> tuple[int, int, int]:
        """原子批量加入 Meme，重复请求幂等；任一 Meme 越界则整批失败。"""
        collection = self.get(collection_id, for_update=True)
        if collection is None:
            raise DatabaseError("collection_not_found")
        unique_ids: list[UUID] = []
        for value in meme_ids:
            identifier = self._uuid(value, "meme_not_found")
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
        if len(existing) != len(unique_ids):
            collection.updated_at = utcnow()
        self.session.flush()
        return len(set(unique_ids) - existing), len(existing), self.member_count(collection.id)

    def remove_member(self, collection_id: UUID | str, meme_id: UUID | str) -> int:
        """幂等移除单个成员并返回最终成员数。"""
        collection = self.get(collection_id, for_update=True)
        if collection is None:
            raise DatabaseError("collection_not_found")
        identifier = self._uuid(meme_id, "meme_not_found")
        deleted = self.session.execute(delete(MemeCollectionItem).where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == collection.id, MemeCollectionItem.meme_id == identifier))
        if deleted.rowcount:
            collection.updated_at = utcnow()
        self.session.flush()
        return self.member_count(collection.id)

    def cover(self, collection_id: UUID | str) -> Meme | None:
        """返回按加入顺序最早且仍存在的 Meme。"""
        identifier = self._uuid(collection_id)
        return self.session.scalar(
            select(Meme)
            .join(MemeCollectionItem, (Meme.scope_id == MemeCollectionItem.scope_id) & (Meme.id == MemeCollectionItem.meme_id))
            .where(MemeCollectionItem.scope_id == self.scope.scope_id, MemeCollectionItem.collection_id == identifier)
            .order_by(MemeCollectionItem.added_at.asc(), MemeCollectionItem.meme_id.asc())
        )


class SearchRepository:
    """按 scope 管理 generation、head 和 pgvector 查询。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def _assert_claim(self, claim: tuple[str, int, str] | None) -> None:
        """在当前事务中锁定并验证 Worker claim，阻止过期任务写入索引。"""
        if claim is None:
            return
        task_id, claim_generation, owner = claim
        now = utcnow()
        task = self.session.scalar(
            select(Task)
            .where(
                Task.scope_id == self.scope.scope_id,
                Task.id == task_id,
                Task.claim_generation == claim_generation,
                Task.lease_owner == owner,
                Task.status == "running",
                Task.lease_expires_at > now,
            )
            .with_for_update()
        )
        if task is None:
            raise DatabaseError("claim_expired")

    def assert_claim(self, claim: tuple[str, int, str] | None) -> None:
        """验证可选 Worker claim，供跨模块 generation 工作在写入前调用。"""
        self._assert_claim(claim)

    def active_generation(self, model: str) -> SearchGeneration | None:
        """返回当前 scope/model 的 active generation。"""
        head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model))
        if not head or not head.active_generation_id:
            return None
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == head.active_generation_id, SearchGeneration.model == model, SearchGeneration.status == "active"))
        return generation

    def create_generation(self, model: str, source_snapshot_hash: str) -> SearchGeneration:
        """创建 building generation，维度固定为 1024。"""
        generation = SearchGeneration(scope_id=self.scope.scope_id, model=model, dimensions=EMBEDDING_DIMENSIONS, source_snapshot_hash=source_snapshot_hash, status="building")
        self.session.add(generation)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DatabaseError("generation_in_progress") from exc
        return generation

    def abandon_building(self, model: str, *, claim: tuple[str, int, str] | None = None) -> int:
        """在持有任务 claim 时隔离上次崩溃遗留的 building generation。"""
        self._assert_claim(claim)
        rows = list(
            self.session.scalars(
                select(SearchGeneration)
                .where(
                    SearchGeneration.scope_id == self.scope.scope_id,
                    SearchGeneration.model == model,
                    SearchGeneration.status == "building",
                )
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = "failed"
        if rows:
            self.session.flush()
        return len(rows)

    def add_item(self, generation: SearchGeneration, meme: Meme, *, semantic_document: str, metadata_hash: str, embedding: Sequence[float] | None, item_status: str = "pending") -> MemeEmbedding:
        """向 generation 写入单条固定维度向量及来源指纹。"""
        if embedding is not None and len(embedding) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        item = MemeEmbedding(scope_id=self.scope.scope_id, generation_id=generation.id, meme_id=meme.id, embedding=list(embedding) if embedding is not None else None, semantic_document=semantic_document, semantic_document_hash=hashlib.sha256(semantic_document.encode()).hexdigest(), metadata_hash=metadata_hash, image_sha256=meme.sha256, meme_revision=meme.revision, item_status=item_status)
        self.session.add(item)
        self.session.flush()
        return item

    def add_snapshot_item(self, generation_id: UUID, *, meme_id: UUID, meme_revision: int, image_sha256: str, semantic_document: str, metadata_hash: str) -> MemeEmbedding:
        """将短事务快照写入 generation，外部 embedding 完成前保持 pending。"""
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id).with_for_update())
        if generation is None or generation.status != "building":
            raise DatabaseError("generation_not_building")
        item = MemeEmbedding(scope_id=self.scope.scope_id, generation_id=generation_id, meme_id=meme_id, embedding=None, semantic_document=semantic_document, semantic_document_hash=hashlib.sha256(semantic_document.encode()).hexdigest(), metadata_hash=metadata_hash, image_sha256=image_sha256, meme_revision=meme_revision, item_status="pending")
        self.session.add(item)
        self.session.flush()
        return item

    def set_item_embedding(self, generation_id: UUID, meme_id: UUID, embedding: Sequence[float], *, item_status: str = "ready", claim: tuple[str, int, str] | None = None) -> None:
        """在独立短事务中写回单条 embedding，并验证可选 Worker claim。"""
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        self._assert_claim(claim)
        result = self.session.execute(
            update(MemeEmbedding)
            .where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation_id, MemeEmbedding.meme_id == meme_id, MemeEmbedding.item_status == "pending")
            .values(embedding=list(embedding), item_status=item_status)
        )
        if result.rowcount != 1:
            raise DatabaseError("generation_item_missing")

    def fail_generation(self, generation_id: UUID, *, error: str, claim: tuple[str, int, str] | None = None) -> None:
        """将仍在构建的 generation 隔离为 failed，不触碰旧 active head。"""
        self._assert_claim(claim)
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id).with_for_update())
        if generation is not None and generation.status == "building":
            generation.status = "failed"
            generation.activated_at = None
            self.session.flush()

    def _generation_source(self, generation_id: UUID) -> list[tuple[str, int, str, str, str]]:
        """读取 generation 中按 meme_id 排序的固定源集合。"""
        rows = self.session.execute(select(MemeEmbedding).where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation_id).order_by(MemeEmbedding.meme_id)).scalars()
        return [(str(row.meme_id), int(row.meme_revision), row.image_sha256, row.metadata_hash, row.semantic_document_hash) for row in rows]

    def _current_indexable_sources(self, memes: Sequence[Meme]) -> list[tuple[str, int, str, str, str]]:
        """从锁定的 Meme 集合重算完整源快照，捕获语境和扩展字段变化。"""
        from backend.metadata import MemeContext, Provenance, SidecarMetadata, semantic_document

        result: list[tuple[str, int, str, str, str]] = []
        for meme in memes:
            if meme.context_status not in {"partial", "ready"}:
                continue
            try:
                context = MemeContext.model_validate(meme.meme_context or {})
                text_value = semantic_document(context)
                if not text_value:
                    continue
                payload: dict[str, Any] = {
                    "schema_version": meme.metadata_schema_version,
                    "image": {
                        "relative_path": meme.storage_key,
                        "extension": meme.extension,
                        "size_bytes": meme.size_bytes,
                        "sha256": meme.sha256,
                    },
                    "context_status": meme.context_status,
                    "meme_context": context.model_dump(mode="json", exclude_none=False),
                    "provenance": Provenance.model_validate(meme.provenance or {}).model_dump(mode="json", exclude_none=False),
                }
                payload.update(meme.extensions or {})
                metadata = SidecarMetadata.model_validate(payload)
                serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                metadata_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            except (TypeError, ValueError):
                continue
            result.append((str(meme.id), int(meme.revision), meme.sha256, metadata_hash, hashlib.sha256(text_value.encode()).hexdigest()))
        return sorted(result)

    def activate(self, generation: SearchGeneration, *, expected_source_hash: str | None = None, expected_items: list[tuple[str, int, str, str, str]] | None = None, claim: tuple[str, int, str] | None = None) -> None:
        """在单事务中校验状态、向量完整性、源快照和 Worker claim 后原子切换 head。"""
        self._assert_claim(claim)
        managed_generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation.id).with_for_update())
        if managed_generation is None:
            raise DatabaseError("generation_not_found")
        generation = managed_generation
        if generation.scope_id != self.scope.scope_id or generation.dimensions != EMBEDDING_DIMENSIONS or generation.status != "building":
            raise DatabaseError("generation_invalid")
        missing = self.session.scalar(select(func.count()).select_from(MemeEmbedding).where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation.id, MemeEmbedding.item_status != "ready"))
        if missing:
            raise DatabaseError("generation_incomplete")
        current_items = self._generation_source(generation.id)
        if expected_items is not None and current_items != expected_items:
            raise DatabaseError("source_changed")
        if expected_source_hash is not None and generation.source_snapshot_hash != expected_source_hash:
            raise DatabaseError("source_changed")
        current_memes = list(self.session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id).with_for_update()))
        current_by_id = {str(item.id): item for item in current_memes}
        if self._current_indexable_sources(current_memes) != current_items:
            raise DatabaseError("source_changed")
        head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == generation.model).with_for_update())
        if head is None:
            head = SearchHead(scope_id=self.scope.scope_id, model=generation.model)
            self.session.add(head)
        if head.active_generation_id and head.active_generation_id != generation.id:
            old = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == head.active_generation_id))
            if old:
                old.status = "retired"
        head.active_generation_id = generation.id
        head.active_generation_model = generation.model
        generation.status = "active"
        generation.activated_at = utcnow()
        self.session.flush()

    def query(self, model: str, vector: Sequence[float], limit: int = 5) -> list[tuple[UUID, float]]:
        """执行 scope/head 限定的精确余弦距离查询并按 meme_id 稳定排序。"""
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        generation = self.active_generation(model)
        if generation is None:
            raise DatabaseError("cache_not_ready")
        distance = MemeEmbedding.embedding.cosine_distance(list(vector))
        statement = select(MemeEmbedding.meme_id, (1 - distance).label("score")).join(Meme, (Meme.scope_id == MemeEmbedding.scope_id) & (Meme.id == MemeEmbedding.meme_id)).where(MemeEmbedding.scope_id == self.scope.scope_id, MemeEmbedding.generation_id == generation.id, MemeEmbedding.item_status == "ready", Meme.context_status.in_(("partial", "ready")), MemeEmbedding.meme_revision == Meme.revision, MemeEmbedding.image_sha256 == Meme.sha256).order_by(distance, MemeEmbedding.meme_id).limit(max(1, min(max(limit, 1) * 4, 120)))
        return [(identifier, float(score)) for identifier, score in self.session.execute(statement).all()]


class TaskRepository:
    """数据库任务队列 repository，所有方法自动带 scope 条件。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, task_id: str, *, for_update: bool = False) -> Task | None:
        """按当前 scope 读取任务，不泄露其他 scope 记录。"""
        statement = select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def submit(self, *, task_type: str, payload: dict[str, Any], lane: str = "default", dedupe_key: str | None = None, settings_version: str | None = None, max_attempts: int = 3, task_id: str | None = None, lane_backpressure: int | None = None) -> Task:
        """在事务中插入或复用活动 dedupe_key 任务。"""
        # Agent 任务先锁定 lane，再检查活动去重，避免策略请求并发穿过预检窗口。
        if lane == "agent" or lane_backpressure is not None:
            self._lock_lane(lane)
        if dedupe_key:
            existing = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))).with_for_update())
            if existing:
                return existing
        if lane_backpressure is not None:
            active = self.session.scalar(select(func.count()).select_from(Task).where(Task.lane == lane, Task.status.in_(("queued", "running")))) or 0
            if int(active) >= int(lane_backpressure):
                raise DatabaseError("agent_backpressure")
        task = Task(id=task_id or uuid.uuid4().hex, scope_id=self.scope.scope_id, task_type=task_type, lane=lane, payload=payload, dedupe_key=dedupe_key, settings_version=settings_version, max_attempts=max_attempts)
        try:
            with self.session.begin_nested():
                self.session.add(task)
                self.session.flush()
        except IntegrityError:
            if dedupe_key:
                # SAVEPOINT 已回滚唯一冲突，外层事务仍可继续写入批次关系。
                existing = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))))
                if existing:
                    return existing
            raise DatabaseError("task_submit_conflict")
        return task

    def _lock_lane(self, lane: str) -> None:
        """在当前事务中锁定整个 lane，保证跨进程槽位和背压判断原子化。"""
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"mememeow:lane:{lane}"))))

    def _ensure_lane_slots(self, lane: str, capacity: int) -> None:
        """幂等创建 lane 槽位；调用者必须先持有 lane advisory lock。"""
        for number in range(max(1, int(capacity))):
            if self.session.get(TaskLaneSlot, (lane, number)) is None:
                self.session.add(TaskLaneSlot(lane=lane, slot_number=number))
        self.session.flush()

    def _release_lane_slot(self, task_scope_id: str, task_id: str, *, owner: str | None = None, claim_generation: int | None = None) -> None:
        """释放任务占用的数据库槽位；owner/generation 用于旧 Worker fencing。"""
        statement = select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == task_scope_id, TaskLaneSlot.task_id == task_id).with_for_update()
        slot = self.session.scalar(statement)
        if slot is None:
            return
        if owner is not None and slot.lease_owner not in {None, owner}:
            return
        slot.task_scope_id = None
        slot.task_id = None
        slot.lease_owner = None
        slot.lease_expires_at = None

    def slot_for_task(self, task_id: str) -> TaskLaneSlot | None:
        """读取当前 scope 任务占用的槽位，用于安全摘要和诊断。"""
        return self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id))

    def recover_expired(self, *, owner: str, limit: int = 1000) -> list[str]:
        """恢复失效租约；未达到上限的任务重新排队，达到上限的任务进入失败终态。"""
        now = utcnow()
        rows = list(self.session.scalars(
            select(Task)
            .where(Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now)
            .order_by(Task.lease_expires_at, Task.id)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(int(limit), 5000)))
        ))
        queued: list[str] = []
        for task in rows:
            if task.attempt_count < task.max_attempts:
                task.status = "queued"
                task.available_at = now
                task.message = "租约已过期，等待重新认领"
                task.error = {"error": "lease_expired", "message": "Worker 租约已过期"}
                queued.append(task.id)
            else:
                task.status = "failed"
                task.completed_at = now
                task.message = "任务达到最大尝试次数"
                task.error = {"error": "max_attempts_exceeded", "message": "任务达到最大尝试次数"}
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=None)
        self.session.flush()
        return queued

    def interrupt_owner(self, owner: str) -> int:
        """将当前 Worker 无法继续管理的 running 任务标记为可诊断失败。"""
        now = utcnow()
        rows = list(self.session.scalars(select(Task).where(Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_owner == owner).with_for_update(skip_locked=True)))
        for task in rows:
            task.status = "failed"
            task.completed_at = now
            task.message = "Worker 已停止"
            task.error = {"error": "task_interrupted", "message": "任务执行 Worker 已停止"}
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner)
        self.session.flush()
        return len(rows)

    def cancel(self, task_id: str, *, error: dict[str, Any], message: str) -> bool:
        """取消当前 scope 的 queued/running 任务并释放其 lane 槽位。"""
        task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
        if task is None or task.status in ("succeeded", "failed"):
            return False
        task.status = "failed"
        task.completed_at = utcnow()
        task.message = message
        task.error = error
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = utcnow()
        self._release_lane_slot(task.scope_id, task.id, owner=None)
        self.session.flush()
        return True

    def _claim_lane_slot(self, task: Task, *, owner: str, lease_expires_at: datetime, capacity: int) -> bool:
        """为任务原子分配可用槽位，过期槽位可被新 claim 回收。"""
        self._lock_lane(task.lane)
        self._ensure_lane_slots(task.lane, capacity)
        slot = self.session.scalar(
            select(TaskLaneSlot)
            .where(
                TaskLaneSlot.lane == task.lane,
                TaskLaneSlot.slot_number < max(1, int(capacity)),
                ((TaskLaneSlot.task_id.is_(None)) | (TaskLaneSlot.lease_expires_at <= utcnow())),
            )
            .order_by(TaskLaneSlot.slot_number)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if slot is None:
            return False
        slot.task_scope_id = task.scope_id
        slot.task_id = task.id
        slot.lease_owner = owner
        slot.lease_expires_at = lease_expires_at
        self.session.flush()
        return True

    def ensure_batch(self, batch_id: str) -> TaskBatch:
        """幂等创建当前 scope 的批次记录。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None:
            batch = TaskBatch(scope_id=self.scope.scope_id, batch_id=batch_id)
            self.session.add(batch)
            self.session.flush()
        return batch

    def add_batch_item(self, batch_id: str, task_id: str) -> None:
        """幂等写入 scope-safe 批次成员关系。"""
        batch = self.ensure_batch(batch_id)
        if batch.sealed:
            raise DatabaseError("batch_sealed")
        if self.session.scalar(select(TaskBatchItem).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id, TaskBatchItem.task_id == task_id)) is None:
            self.session.add(TaskBatchItem(scope_id=self.scope.scope_id, batch_id=batch_id, task_id=task_id))
            self.session.flush()

    def batch_ids_for_task(self, task_id: str) -> list[str]:
        """读取当前 scope 中与任务关联的所有批次，兼容任务去重后的复用路径。"""
        return list(self.session.scalars(select(TaskBatchItem.batch_id).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.task_id == task_id)))

    def finalize_batch(self, batch_id: str) -> bool:
        """在锁定已封口批次后检查成员终态，并原子写入索引刷新任务。"""
        return self._finalize_batch_task(batch_id) is not None

    def seal_batch(self, batch_id: str) -> None:
        """标记批次不再接收成员，防止 finalizer 观察到不完整成员集合。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None:
            raise DatabaseError("batch_not_found")
        batch.sealed = True
        self.session.flush()

    def finalize_batch_with_task(self, batch_id: str, *, task_type: str, payload: dict[str, Any], dedupe_key: str, settings_version: str | None = None, max_attempts: int = 3) -> Task | None:
        """锁定已封口批次，并在同一事务插入或复用唯一索引任务。"""
        return self._finalize_batch_task(batch_id, task_type=task_type, payload=payload, dedupe_key=dedupe_key, settings_version=settings_version, max_attempts=max_attempts)

    def pending_finalizer_batches(self, *, limit: int = 1000) -> list[str]:
        """列出当前 scope 中需要重试收束的已封口批次，供 Worker 启动恢复。"""
        statement = (
            select(TaskBatch.batch_id)
            .where(
                TaskBatch.scope_id == self.scope.scope_id,
                TaskBatch.sealed.is_(True),
                TaskBatch.finalizer_state.in_(('pending', 'submitted')),
            )
            .order_by(TaskBatch.created_at, TaskBatch.batch_id)
            .limit(max(1, min(int(limit), 5000)))
        )
        return list(self.session.scalars(statement))

    def _finalize_batch_task(self, batch_id: str, *, task_type: str | None = None, payload: dict[str, Any] | None = None, dedupe_key: str | None = None, settings_version: str | None = None, max_attempts: int = 3) -> Task | None:
        """在同一事务锁定批次并可选插入唯一索引任务。"""
        batch = self.session.scalar(select(TaskBatch).where(TaskBatch.scope_id == self.scope.scope_id, TaskBatch.batch_id == batch_id).with_for_update())
        if batch is None or not batch.sealed or batch.finalizer_state not in {"pending", "submitted"}:
            return None
        item_count = self.session.scalar(select(func.count()).select_from(TaskBatchItem).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id)) or 0
        if not item_count:
            return None
        active = self.session.scalar(select(func.count()).select_from(TaskBatchItem).join(Task, (Task.scope_id == TaskBatchItem.scope_id) & (Task.id == TaskBatchItem.task_id)).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id, ~Task.status.in_(("succeeded", "failed")))) or 0
        if active:
            return None
        task: Task | None = None
        if task_type is not None:
            if dedupe_key:
                task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))).with_for_update())
            if task is None:
                task = Task(id=uuid.uuid4().hex, scope_id=self.scope.scope_id, task_type=task_type, lane="default", payload=dict(payload or {}), dedupe_key=dedupe_key, settings_version=settings_version, max_attempts=max_attempts)
                self.session.add(task)
                try:
                    with self.session.begin_nested():
                        self.session.flush()
                except IntegrityError:
                    task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.task_type == task_type, Task.dedupe_key == dedupe_key, Task.status.in_(("queued", "running"))))
                    if task is None:
                        raise DatabaseError("task_submit_conflict")
            batch.finalizer_state = "complete"
        else:
            batch.finalizer_state = "submitted"
        batch.finalized_at = utcnow()
        self.session.flush()
        return task

    def claim(self, *, owner: str, lease_seconds: int = 120, lane: str | None = None, task_id: str | None = None, lane_capacity: int | None = None) -> Task | None:
        """使用 FOR UPDATE SKIP LOCKED 原子认领一个到期任务并递增 claim generation。"""
        now = utcnow()
        self.recover_expired(owner=owner)
        if lane and lane_capacity:
            self._lock_lane(lane)
            self._ensure_lane_slots(lane, lane_capacity)
        filters = [Task.scope_id == self.scope.scope_id, Task.status == "queued", Task.available_at <= now]
        if lane:
            filters.append(Task.lane == lane)
        if task_id:
            filters.append(Task.id == task_id)
        statement = select(Task).where(*filters).order_by(Task.available_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
        task = self.session.scalar(statement)
        if task is None:
            filters = [Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now, Task.attempt_count < Task.max_attempts]
            if lane:
                filters.append(Task.lane == lane)
            if task_id:
                filters.append(Task.id == task_id)
            statement = select(Task).where(*filters).order_by(Task.lease_expires_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
            task = self.session.scalar(statement)
        if task is None:
            return None
        if lane and lane_capacity:
            # task_id 过滤只缩小候选范围，不能绕过全局 lane 槽位上限。
            expires_at = now + timedelta(seconds=lease_seconds)
            if not self._claim_lane_slot(task, owner=owner, lease_expires_at=expires_at, capacity=lane_capacity):
                return None
        else:
            expires_at = now + timedelta(seconds=lease_seconds)
        task.status = "running"
        task.claim_generation += 1
        task.attempt_count += 1
        task.lease_owner = owner
        task.lease_expires_at = expires_at
        task.started_at = task.started_at or now
        task.updated_at = now
        self.session.flush()
        return task

    def heartbeat(self, task_id: str, claim_generation: int, owner: str, lease_seconds: int = 120) -> bool:
        """在当前 claim 仍有效时续租，防止长时间外部调用被误判为崩溃。"""
        now = utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        result = self.session.execute(update(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id, Task.claim_generation == claim_generation, Task.lease_owner == owner, Task.status == "running", Task.lease_expires_at > now).values(lease_expires_at=expires_at, updated_at=now))
        if result.rowcount != 1:
            return False
        slot = self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id).with_for_update())
        if slot is not None and slot.lease_owner == owner:
            slot.lease_expires_at = expires_at
            self.session.flush()
        return True

    def fail_fenced(self, task_id: str, claim_generation: int, owner: str, *, error: dict[str, Any], message: str, retry: bool = True, result: Any | None = None) -> tuple[bool, bool]:
        """在 fencing 条件下失败或重新排队任务，返回 (已更新, 是否可重试)。"""
        now = utcnow()
        task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
        if task is None or task.status != "running" or task.claim_generation != claim_generation or task.lease_owner != owner or not task.lease_expires_at or task.lease_expires_at <= now:
            return False, False
        should_retry = bool(retry and task.attempt_count < task.max_attempts)
        if should_retry:
            task.status = "queued"
            task.available_at = now
            task.message = message
            task.error = error
            if result is not None:
                task.result = result
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner, claim_generation=claim_generation)
        else:
            task.status = "failed"
            task.completed_at = now
            task.message = message
            task.error = error
            if result is not None:
                task.result = result
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = now
            self._release_lane_slot(task.scope_id, task.id, owner=owner, claim_generation=claim_generation)
        self.session.flush()
        return True, should_retry

    def update_fenced(self, task_id: str, claim_generation: int, owner: str, **changes: Any) -> bool:
        """仅在当前 claim 和租约有效时提交进度、终态或结果。"""
        now = utcnow()
        requested_status = changes.get("status")
        if requested_status not in {None, "succeeded", "failed"}:
            raise DatabaseError("invalid_task_transition")
        values = {key: value for key, value in changes.items() if hasattr(Task, key) and key != "status"}
        if requested_status in {"succeeded", "failed"}:
            values["status"] = requested_status
            values["completed_at"] = now
            values["lease_expires_at"] = None
        values["updated_at"] = now
        result = self.session.execute(update(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id, Task.claim_generation == claim_generation, Task.lease_owner == owner, Task.status == "running", Task.lease_expires_at > now).values(**values))
        if result.rowcount != 1:
            return False
        if requested_status in {"succeeded", "failed"}:
            self._release_lane_slot(self.scope.scope_id, task_id, owner=owner, claim_generation=claim_generation)
            self.session.flush()
        return True

    def list(self, *, statuses: set[str] | None = None, task_types: set[str] | None = None, cursor: str | None = None, limit: int = 50) -> tuple[list[Task], str | None]:
        """按更新时间和 ID 稳定分页列出当前 scope 任务。"""
        statement = select(Task).where(Task.scope_id == self.scope.scope_id)
        if statuses:
            statement = statement.where(Task.status.in_(statuses))
        if task_types:
            statement = statement.where(Task.task_type.in_(task_types))
        if cursor:
            cursor_record = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == cursor))
            if cursor_record:
                statement = statement.where((Task.updated_at < cursor_record.updated_at) | ((Task.updated_at == cursor_record.updated_at) & (Task.id < cursor_record.id)))
        statement = statement.order_by(Task.updated_at.desc(), Task.id.desc()).limit(max(1, min(limit, 100)) + 1)
        records = list(self.session.scalars(statement))
        next_cursor = records[-1].id if len(records) > limit else None
        return records[:limit], next_cursor



class DataEnvironment:
    """请求或任务级 scope-bound Session、repository 和 BlobStore 组合。"""

    def __init__(self, factory: sessionmaker[Session], scope: ScopeContext):
        self.uow = UnitOfWork(factory, scope)
        self.scope = scope
        self.memes = MemeRepository(self.uow.session, scope)
        self.collections = CollectionRepository(self.uow.session, scope)
        self.search = SearchRepository(self.uow.session, scope)
        self.tasks = TaskRepository(self.uow.session, scope)

    def __enter__(self) -> "DataEnvironment":
        self.uow.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.uow.__exit__(exc_type, exc, tb)


class BlobStore:
    """绑定 scope 的文件存储；local 使用现有图片根目录，其他 scope 独立命名空间。"""

    def __init__(self, *, root: Path, scope: ScopeContext, storage_namespace: UUID | None = None, local: bool = False):
        self.scope = scope
        self.root = root.expanduser().resolve() if local else (root / "scopes" / str(storage_namespace or uuid.uuid4()) / "images").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root = self.root / ".staging"
        self.quarantine_root = self.root / ".quarantine"
        self.staging_root.mkdir(exist_ok=True)
        self.quarantine_root.mkdir(exist_ok=True)

    def resolve(self, storage_key: str, *, must_exist: bool = True) -> Path:
        """安全解析 scope 内相对 key，拒绝绝对路径、穿越和符号链接逃逸。"""
        if not isinstance(storage_key, str) or not storage_key or storage_key.startswith(("/", "\\")) or "\x00" in storage_key:
            raise DatabaseError("invalid_storage_key")
        # 暂存和隔离对象只允许由内部恢复流程访问，不能成为公开 Meme 路径。
        if storage_key == ".staging" or storage_key == ".quarantine" or storage_key.startswith((".staging/", ".quarantine/")):
            raise DatabaseError("internal_storage_key")
        lexical = self.root / storage_key
        current = self.root
        for part in Path(storage_key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        candidate = lexical.resolve()
        # 解析后的不存在目标仍需允许后续上传检查；只有现存路径才要求最终路径在根目录内。
        if candidate == self.root or self.root not in candidate.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not candidate.is_file() or candidate.is_symlink()):
            raise DatabaseError("file_not_found")
        return candidate

    def relative(self, path: Path) -> str:
        """返回绑定根目录下的 POSIX storage_key。"""
        candidate = Path(path)
        if candidate.is_symlink():
            raise DatabaseError("symlink_forbidden")
        resolved = candidate.resolve()
        if resolved == self.root or self.root not in resolved.parents:
            raise DatabaseError("path_forbidden")
        return resolved.relative_to(self.root).as_posix()

    def _safe_child(self, base: Path, key: str, *, must_exist: bool = False) -> Path:
        """解析 staging/quarantine 子路径并拒绝符号链接与越界。"""
        if not isinstance(key, str) or not key or key.startswith(("/", "\\")) or "\x00" in key:
            raise DatabaseError("invalid_storage_key")
        lexical = base / key
        current = base
        for part in Path(key).parts:
            current = current / part
            if current.is_symlink():
                raise DatabaseError("symlink_forbidden")
        resolved = lexical.resolve()
        if resolved == base or base not in resolved.parents:
            raise DatabaseError("path_forbidden")
        if must_exist and (not resolved.is_file() or resolved.is_symlink()):
            raise DatabaseError("file_not_found")
        return resolved

    def stage_bytes(self, content: bytes, *, token: UUID) -> str:
        """以独占创建和 fsync 将已验证字节写入受控暂存区。"""
        key = f"{token.hex}.part"
        target = self._safe_child(self.staging_root, key)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(target, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise DatabaseError("staging_conflict") from exc
        except OSError as exc:
            raise DatabaseError("staging_write_failed") from exc
        self._fsync_directory(self.staging_root)
        return f".staging/{key}"

    def _fsync_directory(self, directory: Path) -> None:
        """尽力持久化目录项，平台不支持时保留原子文件语义。"""
        try:
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _key_path(self, key: str, *, must_exist: bool = False) -> Path:
        """解析普通 storage key 或内部暂存/隔离 key。"""
        if key.startswith(".staging/"):
            return self._safe_child(self.staging_root, key[len(".staging/"):], must_exist=must_exist)
        if key.startswith(".quarantine/"):
            return self._safe_child(self.quarantine_root, key[len(".quarantine/"):], must_exist=must_exist)
        return self.resolve(key, must_exist=must_exist)

    def link_move(self, source_key: str, target_key: str) -> None:
        """以 link+unlink 实现同文件系统的原子不覆盖移动。"""
        source = self._key_path(source_key, must_exist=True)
        target = self._key_path(target_key, must_exist=False)
        if target.exists() or target.is_symlink():
            raise DatabaseError("target_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target, follow_symlinks=False)
            os.unlink(source)
            self._fsync_directory(target.parent)
            self._fsync_directory(source.parent)
        except FileExistsError as exc:
            raise DatabaseError("target_exists") from exc
        except OSError as exc:
            try:
                if target.exists() and not source.exists():
                    os.unlink(target)
            except OSError:
                pass
            raise DatabaseError("file_move_failed") from exc

    def quarantine(self, source_key: str, *, token: UUID) -> str:
        """将图片移动到不可见隔离区并返回隔离 storage key。"""
        target_key = f".quarantine/{token.hex}.blob"
        self.link_move(source_key, target_key)
        return target_key

    def unlink(self, key: str) -> None:
        """只删除受控普通文件，拒绝符号链接和越界路径。"""
        target = self._key_path(key, must_exist=True)
        if target.is_symlink():
            raise DatabaseError("symlink_forbidden")
        try:
            os.unlink(target)
            self._fsync_directory(target.parent)
        except OSError as exc:
            raise DatabaseError("file_delete_failed") from exc

    def exists_with_identity(self, key: str, *, sha256: str | None = None, size_bytes: int | None = None) -> bool:
        """检查对象存在、非符号链接，并可选复核大小和 SHA。"""
        try:
            path = self._key_path(key, must_exist=True)
            stat = path.stat()
            if size_bytes is not None and stat.st_size != size_bytes:
                return False
            if sha256 is not None:
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != sha256:
                    return False
            return True
        except (DatabaseError, OSError):
            return False


class StorageCoordinator:
    """协调 PostgreSQL Meme 记录与 scope-bound 文件存储的可恢复操作。

    上传、重命名和删除都先写入 ``storage_operations``，再执行文件动作；恢复器依据
    记录中的指纹和状态矩阵继续提交、补偿或隔离异常组合，避免中间文件进入正常查询。
    """

    _ACTIVE = {"prepared", "file_applied"}
    _TRANSITIONS = {
        "prepared": {"file_applied", "compensated", "blocked"},
        "file_applied": {"completed", "compensated", "blocked"},
        "completed": set(),
        "compensated": set(),
        "blocked": set(),
    }

    def __init__(self, resources: "DatabaseResources", *, scope_id: str = SCOPE_LOCAL):
        self.resources = resources
        self.scope = ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(scope_id)

    def _set_status(self, operation: StorageOperation, status: str, *, error: dict[str, Any] | None = None, session: Session | None = None) -> None:
        """执行 storage operation 合法状态转移并保存诊断信息。"""
        if status == operation.status:
            return
        if status not in self._TRANSITIONS.get(operation.status, set()):
            raise DatabaseError("invalid_storage_transition")
        operation.status = status
        operation.error = error
        operation.updated_at = utcnow()
        (session or self._session).flush()

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        """创建当前 scope 的短数据库事务，并在退出时提交或回滚。"""
        with self.resources.factory() as session:
            self._session = session
            try:
                with session.begin():
                    yield session
            finally:
                self._session = None

    def upload(self, content: bytes, *, target_key: str, extension: str, context: dict[str, Any], provenance: dict[str, Any], meme_id: UUID | None = None) -> Meme:
        """暂存上传字节、创建 pending Meme，并在文件落位后完成 durable operation。"""
        try:
            validate_business_storage_key(target_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        target = self.blob_store._key_path(target_key)
        if target.exists() or target.is_symlink():
            raise DatabaseError("target_exists")
        digest = hashlib.sha256(content).hexdigest()
        token = uuid.uuid4()
        staging_key = self.blob_store.stage_bytes(content, token=token)
        try:
            with self._transaction() as session:
                existing = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.storage_key == target_key).with_for_update())
                if existing is not None:
                    raise DatabaseError("target_exists")
                record = Meme(id=meme_id or uuid.uuid4(), scope_id=self.scope.scope_id, storage_key=target_key, extension=extension.lower(), size_bytes=len(content), sha256=digest, context_status="pending", meme_context=context, provenance=provenance, extensions={}, revision=1)
                session.add(record)
                session.flush()
                session.add(StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="upload", operation_token=token, target_key=target_key, staging_key=staging_key, after_sha256=digest, after_size=len(content), status="prepared"))
                session.flush()
            self.blob_store.link_move(staging_key, target_key)
            with self._transaction() as session:
                operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token).with_for_update())
                if operation is None:
                    raise DatabaseError("storage_operation_missing")
                self._session = session
                self._set_status(operation, "file_applied", session=session)
                self._set_status(operation, "completed", session=session)
            return record
        except Exception:
            # 暂存文件没有数据库引用时可以安全清理；已写入 operation 的异常留给恢复器。
            try:
                if self.blob_store.exists_with_identity(staging_key, sha256=digest, size_bytes=len(content)):
                    self.blob_store.unlink(staging_key)
            except DatabaseError:
                pass
            raise

    def rename(self, meme_id: UUID | str, *, target_key: str) -> Meme:
        """记录重命名意图、原子移动文件并提交同一 Meme 的新 storage_key。"""
        try:
            validate_business_storage_key(target_key)
        except ValueError as exc:
            raise DatabaseError(str(exc)) from exc
        token = uuid.uuid4()
        with self._transaction() as session:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            if session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.storage_key == target_key, Meme.id != record.id)) is not None:
                raise DatabaseError("target_exists")
            target_path = self.blob_store._key_path(target_key)
            if target_path.exists() or target_path.is_symlink():
                raise DatabaseError("target_exists")
            operation = StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="rename", operation_token=token, source_key=record.storage_key, target_key=target_key, before_sha256=record.sha256, after_sha256=record.sha256, before_size=record.size_bytes, after_size=record.size_bytes, status="prepared")
            session.add(operation)
            session.flush()
        try:
            self.blob_store.link_move(record.storage_key, target_key)
        except Exception:
            raise
        with self._transaction() as session:
            operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token).with_for_update())
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if operation is None or record is None:
                raise DatabaseError("storage_operation_missing")
            self._session = session
            self._set_status(operation, "file_applied", session=session)
            record.storage_key = target_key
            record.revision += 1
            record.updated_at = utcnow()
            self._set_status(operation, "completed", session=session)
            session.flush()
            return record

    def delete(self, meme_id: UUID | str) -> None:
        """先将文件移入隔离区，再删除 Meme 记录并清理隔离对象。"""
        token = uuid.uuid4()
        with self._transaction() as session:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            operation = StorageOperation(scope_id=self.scope.scope_id, meme_id=record.id, operation_type="delete", operation_token=token, source_key=record.storage_key, target_key=f".quarantine/{token.hex}.blob", before_sha256=record.sha256, before_size=record.size_bytes, status="prepared")
            session.add(operation)
            session.flush()
            source_key = record.storage_key
        self.blob_store.quarantine(source_key, token=token)
        with self._transaction() as session:
            operation = session.scalar(select(StorageOperation).where(StorageOperation.operation_token == token).with_for_update())
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
            if operation is None or record is None:
                raise DatabaseError("storage_operation_missing")
            self._session = session
            self._set_status(operation, "file_applied", session=session)
            # 删除 Meme 前解除关联，保留 completed operation 作为恢复审计记录。
            operation.meme_id = None
            session.delete(record)
            session.flush()
            self._set_status(operation, "completed", session=session)
        try:
            self.blob_store.unlink(f".quarantine/{token.hex}.blob")
        except DatabaseError:
            # 清理失败不影响数据库权威删除，恢复扫描会继续处理隔离对象。
            pass

    def recover(self, *, limit: int = 100) -> dict[str, int]:
        """以 SKIP LOCKED 独占恢复未完成操作，并返回各状态处理计数。"""
        counts = {"completed": 0, "compensated": 0, "blocked": 0, "retried": 0}
        with self.resources.factory() as session:
            rows = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE))).order_by(StorageOperation.updated_at).with_for_update(skip_locked=True).limit(max(1, min(limit, 1000)))))
            for operation in rows:
                self._session = session
                try:
                    if operation.operation_type == "upload":
                        self._recover_upload(session, operation, counts)
                    elif operation.operation_type == "rename":
                        self._recover_rename(session, operation, counts)
                    elif operation.operation_type == "delete":
                        self._recover_delete(session, operation, counts)
                except DatabaseError as exc:
                    self._set_status(operation, "blocked", error={"error": exc.code, "message": str(exc)}, session=session)
                    counts["blocked"] += 1
            session.commit()
            self._session = None
        return counts

    def _recover_upload(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复上传暂存文件或补偿无文件的 Meme。"""
        assert operation.target_key and operation.staging_key
        target_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        stage_ok = self.blob_store.exists_with_identity(operation.staging_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        if operation.status == "prepared" and stage_ok and not target_ok:
            self.blob_store.link_move(operation.staging_key, operation.target_key)
            stage_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not stage_ok:
            self._set_status(operation, "file_applied", session=session)
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif not target_ok and not stage_ok:
            record = session.get(Meme, operation.meme_id) if operation.meme_id else None
            if record is not None:
                operation.meme_id = None
                self._session.delete(record)
            self._set_status(operation, "compensated", session=session)
            counts["compensated"] += 1
        else:
            raise DatabaseError("upload_recovery_ambiguous")

    def _recover_rename(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复重命名文件动作和数据库路径提交。"""
        assert operation.source_key and operation.target_key
        source_ok = self.blob_store.exists_with_identity(operation.source_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        target_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.after_sha256, size_bytes=operation.after_size)
        if operation.status == "prepared" and source_ok and not target_ok:
            self.blob_store.link_move(operation.source_key, operation.target_key)
            source_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not source_ok:
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
            if record is None:
                raise DatabaseError("meme_not_found")
            self._set_status(operation, "file_applied", session=session)
            record.storage_key = operation.target_key
            record.revision += 1
            record.updated_at = utcnow()
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif source_ok and not target_ok and operation.status == "file_applied":
            self._set_status(operation, "compensated", session=session)
            counts["compensated"] += 1
        else:
            raise DatabaseError("rename_recovery_ambiguous")

    def _recover_delete(self, session: Session, operation: StorageOperation, counts: dict[str, int]) -> None:
        """恢复隔离删除；冲突时阻断自动修改。"""
        assert operation.source_key and operation.target_key
        source_ok = self.blob_store.exists_with_identity(operation.source_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        quarantine_ok = self.blob_store.exists_with_identity(operation.target_key, sha256=operation.before_sha256, size_bytes=operation.before_size)
        if operation.status == "prepared" and source_ok and not quarantine_ok:
            self.blob_store.quarantine(operation.source_key, token=UUID(operation.operation_token.hex))
            source_ok, quarantine_ok = False, True
            counts["retried"] += 1
        if quarantine_ok and not source_ok:
            self._set_status(operation, "file_applied", session=session)
            record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
            if record is not None:
                operation.meme_id = None
                self._session.delete(record)
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
            try:
                self.blob_store.unlink(operation.target_key)
            except DatabaseError:
                pass
        elif not quarantine_ok and not source_ok:
            self._set_status(operation, "blocked", error={"error": "delete_blob_missing", "message": "源文件和隔离文件均不存在"}, session=session)
            counts["blocked"] += 1
        else:
            raise DatabaseError("delete_recovery_ambiguous")

    def flat_preflight(self) -> dict[str, Any]:
        """只读检查业务 key、嵌套图片和记录/文件一致性，供 migration 与启动门禁使用。"""
        report: dict[str, Any] = {"non_flat_keys": [], "nested_images": [], "orphan_files": [], "missing_files": [], "mismatched": [], "active_operations": []}
        with self.resources.factory() as session:
            records = list(session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id)))
            referenced: set[str] = set()
            for record in records:
                referenced.add(record.storage_key)
                try:
                    validate_business_storage_key(record.storage_key)
                except ValueError:
                    report["non_flat_keys"].append(record.storage_key)
                if not self.blob_store.exists_with_identity(record.storage_key):
                    report["missing_files"].append(str(record.id))
                elif not self.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes):
                    report["mismatched"].append(str(record.id))
            for path in self.blob_store.root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                    continue
                key = path.relative_to(self.blob_store.root).as_posix()
                if "/" in key and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["nested_images"].append(key)
                elif key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["orphan_files"].append(key)
            operations = list(session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE)))))
            for operation in operations:
                fields = []
                if operation.operation_type == "upload":
                    fields = [operation.target_key]
                elif operation.operation_type == "rename":
                    fields = [operation.source_key, operation.target_key]
                elif operation.operation_type == "delete":
                    fields = [operation.source_key]
                for value in fields:
                    if value:
                        try:
                            validate_business_storage_key(value)
                        except ValueError:
                            report["non_flat_keys"].append(value)
            report["active_operations"] = [str(item.id) for item in operations]
        return report

    def integrity_scan(self) -> dict[str, Any]:
        """双向核对数据库 Meme 与文件对象，标记缺失/指纹冲突并报告孤立文件。"""
        report: dict[str, Any] = {"orphan_files": [], "missing_files": [], "mismatched": [], "path_conflicts": [], "active_operations": []}
        with self.resources.factory() as session:
            records = list(session.scalars(select(Meme).where(Meme.scope_id == self.scope.scope_id)))
            referenced: set[str] = set()
            duplicate_keys: dict[str, list[str]] = {}
            for record in records:
                referenced.add(record.storage_key)
                duplicate_keys.setdefault(record.storage_key, []).append(str(record.id))
                if not self.blob_store.exists_with_identity(record.storage_key):
                    report["missing_files"].append(str(record.id))
                    record.context_status = "repair_required"
                    continue
                if not self.blob_store.exists_with_identity(record.storage_key, sha256=record.sha256, size_bytes=record.size_bytes):
                    report["mismatched"].append(str(record.id))
                    record.context_status = "repair_required"
            for path in self.blob_store.root.rglob("*"):
                if not path.is_file() or path.is_symlink() or path.is_relative_to(self.blob_store.staging_root) or path.is_relative_to(self.blob_store.quarantine_root):
                    continue
                key = path.relative_to(self.blob_store.root).as_posix()
                if key not in referenced and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    report["orphan_files"].append(key)
            report["path_conflicts"] = [ids for ids in duplicate_keys.values() if len(ids) > 1]
            report["active_operations"] = [str(item.id) for item in session.scalars(select(StorageOperation).where(StorageOperation.scope_id == self.scope.scope_id, StorageOperation.status.in_(tuple(self._ACTIVE))))]
            session.commit()
        return report


class DatabaseResources:
    """生命周期共享 Engine、Session 工厂、local scope 和 BlobStore。"""

    def __init__(self, engine: Engine, *, image_root: Path, data_root: Path, settings: Any | None = None):
        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False, class_=Session)
        self.image_root = image_root
        self.data_root = data_root
        self._scope_cache: dict[str, Scope] = {}
        self._lock = Lock()
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == SCOPE_LOCAL))
            if scope is None:
                raise DatabaseError("installation_required")
            self._scope_cache[SCOPE_LOCAL] = scope
            namespace = scope.storage_namespace if scope else None
        self.blob_store = BlobStore(root=image_root, scope=ScopeContext(SCOPE_LOCAL), storage_namespace=namespace, local=True)

    def environment(self, scope_id: str = SCOPE_LOCAL) -> DataEnvironment:
        """创建请求级 DataEnvironment；当前公共适配器只允许 local。"""
        return DataEnvironment(self.factory, ScopeContext(scope_id))

    def flat_preflight(self, scope_id: str = SCOPE_LOCAL) -> dict[str, Any]:
        """执行当前 scope 的扁平图片库只读预检。"""
        return StorageCoordinator(self, scope_id=scope_id).flat_preflight()

    def blob_store_for_scope(self, scope_id: str) -> BlobStore:
        """读取 scope 的不可变 storage_namespace 并创建绑定 BlobStore。"""
        if scope_id == SCOPE_LOCAL:
            return self.blob_store
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == scope_id))
            if scope is None:
                raise DatabaseError("scope_not_found")
            return BlobStore(root=self.data_root, scope=ScopeContext(scope_id), storage_namespace=scope.storage_namespace, local=False)
