"""持久化层的 SQLAlchemy ORM 声明和 scope 边界。

该模块是数据库模型的唯一声明来源。backend.database 仅负责兼容导出和
engine、Repository、文件存储及资源装配；模型模块不反向依赖这些运行时组件。
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


EMBEDDING_DIMENSIONS = 1024
VISUAL_EMBEDDING_DIMENSIONS = 768
UTC = timezone.utc


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供模型默认值和持久化租约计算共用。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ScopeContext:
    """不可变且不可为空的数据范围上下文。

    scope ID 由宿主 resolver 或持久任务记录提供，不能包含路径分隔符、控制字符
    或超出数据库列长度；文件 namespace 由数据库 ``Scope.storage_namespace``
    决定，而不是由这个外部标识直接拼接。
    """

    scope_id: str

    def __post_init__(self) -> None:
        """在创建边界拒绝空值、路径片段和不可存储的 scope 标识。"""
        if not isinstance(self.scope_id, str):
            raise ValueError("scope_required")
        value = self.scope_id.strip()
        if not value:
            raise ValueError("scope_required")
        if len(value) > 128 or value in {".", ".."} or "/" in value or "\\" in value or any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError("scope_invalid")
        object.__setattr__(self, "scope_id", value)


class Base(DeclarativeBase):
    """应用全部 PostgreSQL 表的声明式基类。"""


class Scope(Base):
    """数据范围和不可变文件命名空间。"""

    __tablename__ = "scopes"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    storage_namespace: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InstallationState(Base):
    """单实例安装门禁；不表示具体调用方或访问主体。"""

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


class DerivedImageThumbnail(Base):
    """与原图版本绑定的缩略图派生事实。

    该表独立于图片处理 Job 和 Meme 语境，复合主键包含 scope、稳定 Meme 身份、
    源内容指纹和固定 profile；派生文件只在 ``available`` 且指纹复核通过时可访问。
    """

    __tablename__ = "derived_image_thumbnails"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_size_bytes: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile: Mapped[str] = mapped_column(String(128), primary_key=True)
    output_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    diagnostic: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        CheckConstraint("length(source_sha256) = 64", name="ck_thumbnail_source_sha256"),
        CheckConstraint("source_size_bytes >= 0", name="ck_thumbnail_source_size_nonnegative"),
        CheckConstraint("status IN ('available','pending','failed','stale')", name="ck_thumbnail_status"),
        CheckConstraint("output_sha256 IS NULL OR length(output_sha256) = 64", name="ck_thumbnail_output_sha256"),
        CheckConstraint("output_size_bytes IS NULL OR output_size_bytes >= 0", name="ck_thumbnail_output_size_nonnegative"),
        CheckConstraint("width IS NULL OR width > 0", name="ck_thumbnail_width"),
        CheckConstraint("height IS NULL OR height > 0", name="ck_thumbnail_height"),
        Index("ix_thumbnail_current", "scope_id", "meme_id", "profile", "status", "updated_at"),
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
    # 删除 operation 在 Meme 行解除关联后仍需保留派生文件 key，供恢复器重试清理。
    thumbnail_keys: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    before_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    after_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    after_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expected_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_title_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        CheckConstraint("expected_revision IS NULL OR expected_revision >= 1", name="ck_storage_operation_expected_revision"),
        CheckConstraint("claim_generation IS NULL OR claim_generation > 0", name="ck_storage_operation_claim_generation"),
        CheckConstraint("attempt IS NULL OR attempt > 0", name="ck_storage_operation_attempt"),
        CheckConstraint("expected_title_fingerprint IS NULL OR length(expected_title_fingerprint) = 64", name="ck_storage_operation_title_fingerprint"),
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


class MemeVisualEmbedding(Base):
    """当前 scope 中一张图片的版本化视觉向量产物。

    视觉向量与文本 generation 完全分表；复合主键把模型和预处理身份纳入
    产物地址，模型切换时不会把两个向量空间混在同一次查询里。当前表由
    当前发布迁移固定为 DINOv2 ViT-B/14 的 768 维；历史模型必须通过独立迁移启用。
    """

    __tablename__ = "meme_visual_embeddings"
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    preprocess_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=VISUAL_EMBEDDING_DIMENSIONS)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(VISUAL_EMBEDDING_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        CheckConstraint(f"dimensions = {VISUAL_EMBEDDING_DIMENSIONS}", name="ck_visual_embedding_dimensions"),
        CheckConstraint("length(image_sha256) = 64", name="ck_visual_embedding_sha256"),
        UniqueConstraint("scope_id", "meme_id", "model", "preprocess_version", name="uq_visual_embedding_identity"),
    )


class Task(Base):
    """字符串任务 ID、图片提交来源、状态、去重键和 Worker 租约。

    ``submission_mode``、``image_stage`` 和 ``processing_job_id`` 是图片阶段任务
    的持久化归属事实。历史记录允许三者为空，但新建图片阶段必须由受信控制面
    写入完整来源，不能以普通 payload 推断 Job 或独立任务归属。Agent 失败时，
    ``resume_*`` 字段和 ``error_history`` 记录同 scope、同输入 attempt 的有限恢复
    摘要，供任务详情和恢复 Worker 使用。
    """

    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    submission_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    image_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processing_job_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lane: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[float | None] = mapped_column(nullable=True, default=0.0)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Agent session 恢复摘要只保存稳定标识和脱敏错误，不保存 prompt/transcript。
    resume_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    resume_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    executor_attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_selector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resume_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    resume_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
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
        ForeignKeyConstraint(["scope_id", "processing_job_id"], ["image_processing_jobs.scope_id", "image_processing_jobs.id"], ondelete="CASCADE"),
        CheckConstraint("status IN ('queued','running','succeeded','failed')", name="ck_task_status"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_task_attempts"),
        CheckConstraint("submission_mode IS NULL OR submission_mode IN ('pipeline','standalone')", name="ck_task_submission_mode"),
        CheckConstraint("image_stage IS NULL OR image_stage IN ('visual','agent','auto_rename','text_embedding')", name="ck_task_image_stage"),
        CheckConstraint("submission_mode IS NULL OR (submission_mode = 'standalone' AND processing_job_id IS NULL) OR (submission_mode = 'pipeline' AND processing_job_id IS NOT NULL)", name="ck_task_submission_job_exclusivity"),
        CheckConstraint("task_type NOT IN ('visual_embedding_generation','meme_context_generation','image_auto_rename','text_embedding_generation') OR (submission_mode IS NULL OR (image_stage IS NOT NULL AND ((task_type = 'visual_embedding_generation' AND image_stage = 'visual') OR (task_type = 'meme_context_generation' AND image_stage = 'agent') OR (task_type = 'image_auto_rename' AND image_stage = 'auto_rename') OR (task_type = 'text_embedding_generation' AND image_stage = 'text_embedding'))))", name="ck_task_image_stage_type"),
        UniqueConstraint("scope_id", "id", name="uq_task_scope_id"),
        Index("ix_tasks_image_submission", "scope_id", "submission_mode", "image_stage", "processing_job_id", "created_at"),
    )


class ReverseImageUsageEvent(Base):
    """反向图片检索的逐请求审计流水。

    事件以 ``scope_id`` 绑定任务和图片，``request_id`` 负责幂等恢复；
    ``provider_called`` 只在真正开始供应商逻辑检索时置为真，供任务终态聚合。
    """

    __tablename__ = "reverse_image_usage_events"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    meme_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    cache_key: Mapped[str] = mapped_column(String(128), nullable=False)
    cache_status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_called: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    provider_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        CheckConstraint("cache_status IN ('hit','miss','refresh')", name="ck_reverse_usage_cache_status"),
        CheckConstraint("outcome IN ('started','success','empty','failed','forbidden')", name="ck_reverse_usage_outcome"),
        CheckConstraint("provider_called = false OR provider_started_at IS NOT NULL", name="ck_reverse_usage_provider_started"),
        CheckConstraint("claim_generation IS NULL OR claim_generation > 0", name="ck_reverse_usage_claim_generation"),
        CheckConstraint("attempt IS NULL OR attempt > 0", name="ck_reverse_usage_attempt"),
        CheckConstraint("target_sha256 IS NULL OR length(target_sha256) = 64", name="ck_reverse_usage_target_sha"),
        Index("ix_reverse_usage_scope_created", "scope_id", "created_at"),
        Index("ix_reverse_usage_scope_task", "scope_id", "task_id", "created_at"),
    )


class AgentCallbackRequest(Base):
    """内部 callback 的请求绑定和幂等结果事实。"""

    __tablename__ = "agent_callback_requests"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False)
    claim_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
        CheckConstraint("claim_generation > 0", name="ck_agent_callback_request_generation"),
        CheckConstraint("attempt > 0", name="ck_agent_callback_request_attempt"),
        CheckConstraint("length(target_sha256) = 64", name="ck_agent_callback_request_target_sha"),
        CheckConstraint("length(input_digest) = 64", name="ck_agent_callback_request_input_digest"),
        CheckConstraint("state IN ('started','completed','failed','unknown_execution')", name="ck_agent_callback_request_state"),
        Index("ix_agent_callback_requests_scope_task", "scope_id", "task_id", "created_at"),
        UniqueConstraint(
            "scope_id",
            "task_id",
            "claim_generation",
            "attempt",
            "operation",
            "target_sha256",
            "input_digest",
            name="uq_agent_callback_requests_logical",
        ),
    )


class OperationGrant(Base):
    """服务端 operation grant 关联事实，不接受客户端 payload 覆盖。

    开源 allow-all 主要在进程内完成幂等；该表为适配宿主和任务恢复提供同一
    个 scope-safe 持久化边界，grant 本身仍是不透明引用。
    """

    __tablename__ = "operation_grants"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metering_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="acquired")
    attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
        CheckConstraint("operation IN ('image.upload','analysis.agent','analysis.reverse_image_search','image.delete')", name="ck_operation_grant_operation"),
        CheckConstraint("state IN ('acquired','committed','released','unknown')", name="ck_operation_grant_state"),
        CheckConstraint("source IS NULL OR (length(source) > 0 AND length(source) <= 64)", name="ck_operation_grant_source"),
        CheckConstraint("units IS NULL OR units > 0", name="ck_operation_grant_units"),
        CheckConstraint("metering_units IS NULL OR metering_units >= 0", name="ck_operation_grant_metering_units"),
        CheckConstraint("request_fingerprint IS NULL OR length(request_fingerprint) = 64", name="ck_operation_grant_fingerprint"),
        Index("ix_operation_grants_scope_task", "scope_id", "task_id"),
    )


class ImageProcessingJob(Base):
    """一张图片一个 revision 的统一处理控制面，冻结联网与自动命名选项。"""

    __tablename__ = "image_processing_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(128), nullable=False)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reverse_image_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="forbid")
    auto_name: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        UniqueConstraint("scope_id", "id", name="uq_image_processing_jobs_scope_id"),
        UniqueConstraint("scope_id", "meme_id", "image_sha256", "revision", name="uq_image_processing_jobs_revision"),
        CheckConstraint("reverse_image_policy IN ('forbid','auto')", name="ck_image_processing_policy"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','blocked','unknown_execution')", name="ck_image_processing_status"),
        CheckConstraint("claim_generation >= 0", name="ck_image_processing_generation"),
        Index("ix_image_processing_jobs_active", "scope_id", "meme_id", "image_sha256", "status"),
    )


class ImageProcessingStage(Base):
    """图片处理 job 的视觉、Agent、自动命名和文本 embedding 阶段状态。"""

    __tablename__ = "image_processing_stages"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    stage: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "job_id"], ["image_processing_jobs.scope_id", "image_processing_jobs.id"], ondelete="CASCADE"),
        # scope_id 是阶段主键，不能被复合外键的 SET NULL 一并置空；任务删除时
        # 由同 scope 处理阶段事实一起级联清理。
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
        CheckConstraint("stage IN ('visual','agent','auto_rename','text_embedding')", name="ck_image_processing_stage_name"),
        CheckConstraint("status IN ('queued','running','succeeded','failed','blocked','unknown_execution','skipped','warning')", name="ck_image_processing_stage_status"),
        Index("ix_image_processing_stages_task", "scope_id", "task_id"),
    )


class ImageProcessingAttempt(Base):
    """外部叶子 Task 的输入摘要和未知执行恢复事实。

    每行对应一个业务 Task attempt；session、executor attempt、配置哈希和目标
    SHA 共同构成恢复绑定，claim generation 用于拒绝旧 Worker 的迟到写回。
    """

    __tablename__ = "image_processing_attempts"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="prepared")
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    executor_attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resume_of_attempt_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_selector: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processing_config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    resume_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    target_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id", "task_id"], ["tasks.scope_id", "tasks.id"], ondelete="CASCADE"),
        CheckConstraint("state IN ('prepared','grant_committed','external_started','completed','failed','unknown_execution')", name="ck_image_processing_attempt_state"),
        Index("ix_image_processing_attempts_task", "scope_id", "task_id", "attempt"),
        Index(
            "uq_image_processing_attempt_executor_id",
            "executor_attempt_id",
            unique=True,
            postgresql_where=text("executor_attempt_id IS NOT NULL"),
        ),
        Index("ix_image_processing_attempts_resume", "scope_id", "task_id", "resume_available", "updated_at"),
    )


class MemeTextEmbedding(Base):
    """单图增量文本向量，唯一键包含图片、语境和模型指纹。"""

    __tablename__ = "meme_text_embeddings"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    meme_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    image_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    metadata_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_model_version: Mapped[str] = mapped_column(String(255), primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=EMBEDDING_DIMENSIONS)
    semantic_document: Mapped[str] = mapped_column(String(6000), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["scope_id", "meme_id"], ["memes.scope_id", "memes.id"], ondelete="CASCADE"),
        CheckConstraint(f"dimensions = {EMBEDDING_DIMENSIONS}", name="ck_meme_text_embedding_dimensions"),
        CheckConstraint("status IN ('pending','ready','failed')", name="ck_meme_text_embedding_status"),
        Index("ix_meme_text_embeddings_current", "scope_id", "meme_id", "image_sha256", "metadata_hash", "embedding_model_version", "status"),
    )


class SearchMigrationState(Base):
    """按 scope 和当前 embedding model 记录单一迁移来源。"""

    __tablename__ = "search_migration_states"

    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 一个 scope 同时可能保留多个模型的历史 generation；该字段用于防止
    # 新模型误用旧模型的 epoch 和 legacy generation。旧安装没有此列时按空值兼容读取。
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="legacy_only")
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    legacy_generation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        CheckConstraint("mode IN ('legacy_only','backfill','incremental_only')", name="ck_search_migration_mode"),
    )


# 这些表属于可选控制面；在已安装旧 revision 的部署启动时用 ``checkfirst``
# 幂等补齐，标准部署仍以对应 Alembic 迁移作为 schema 版本事实。
OPTIONAL_CONTROL_TABLES = (
    DerivedImageThumbnail.__table__,
    OperationGrant.__table__,
    ImageProcessingJob.__table__,
    ImageProcessingStage.__table__,
    ImageProcessingAttempt.__table__,
    MemeTextEmbedding.__table__,
    SearchMigrationState.__table__,
    AgentCallbackRequest.__table__,
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
    claim_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["task_scope_id"], ["scopes.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["task_scope_id", "task_id"], ["tasks.scope_id", "tasks.id"]),
        UniqueConstraint("task_scope_id", "task_id", name="uq_task_lane_slot_task"),
        CheckConstraint("slot_number >= 0", name="ck_slot_number"),
    )


class TaskLaneFairness(Base):
    """按 lane/scope 持久化最近一次成功调度序号。

    该表是跨进程 Agent 公平 claim 的唯一轮询事实。``last_dispatch_sequence``
    只在任务、槽位和租约同一事务成功提交时推进，不能由客户端 payload 或
    Worker 进程内 cursor 替代。
    """

    __tablename__ = "task_lane_fairness"
    lane: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_dispatch_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["scope_id"], ["scopes.id"], ondelete="CASCADE"),
        CheckConstraint("last_dispatch_sequence >= 0", name="ck_task_lane_fairness_sequence"),
        Index("ix_task_lane_fairness_dispatch", "lane", "last_dispatch_sequence", "scope_id"),
    )


Index("ix_tasks_active_dedupe", Task.scope_id, Task.task_type, Task.dedupe_key, unique=True, postgresql_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"))
Index("ix_tasks_claimable", Task.scope_id, Task.status, Task.available_at, Task.lease_expires_at)
Index("ix_embeddings_generation_status", MemeEmbedding.scope_id, MemeEmbedding.generation_id, MemeEmbedding.item_status)
Index("ix_visual_embeddings_match", MemeVisualEmbedding.scope_id, MemeVisualEmbedding.model, MemeVisualEmbedding.preprocess_version, MemeVisualEmbedding.meme_id)
Index("uq_search_generation_building", SearchGeneration.scope_id, SearchGeneration.model, unique=True, postgresql_where=text("status = 'building'"))
Index("ix_storage_operations_recovery", StorageOperation.scope_id, StorageOperation.status, StorageOperation.updated_at)
Index("uq_storage_operation_active", StorageOperation.scope_id, StorageOperation.meme_id, unique=True, postgresql_where=text("status IN ('prepared','file_applied') AND meme_id IS NOT NULL"))
