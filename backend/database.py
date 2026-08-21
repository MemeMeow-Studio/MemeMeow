"""PostgreSQL 权威存储、scope 边界和同步数据环境。

该模块位于 API、任务 Worker 与领域服务之间。所有结构化业务数据通过这里创建的
SQLAlchemy Session 和已绑定 scope 的 repository 访问；图片字节仍由 BlobStore 保存。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import errno
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
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
from backend.storage_security import StorageRootError, validate_controlled_root
from backend.agent_resume import append_error_history, append_task_error_history, normalize_identifier, sanitize_error


EMBEDDING_DIMENSIONS = 1024
VISUAL_EMBEDDING_DIMENSIONS = 768
SCOPE_LOCAL = "local"
UTC = timezone.utc
# 当前代码要求的 Alembic head；数据库初始化脚本会显式传入同一 revision。
CURRENT_SCHEMA_REVISION = "0015_bind_agent_callback_request_ids"


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间，供数据库字段和租约计算共用。"""
    return datetime.now(UTC)


class DatabaseError(RuntimeError):
    """数据库边界错误，携带不会泄露连接凭据的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


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


# 这些表属于图片处理控制面；在已安装旧 revision 的部署启动时用 ``checkfirst``
# 幂等补齐，标准部署仍以 Alembic 0011 迁移作为唯一 schema 版本事实。
OPTIONAL_CONTROL_TABLES = (
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


Index("ix_tasks_active_dedupe", Task.scope_id, Task.task_type, Task.dedupe_key, unique=True, postgresql_where=text("status IN ('queued','running') AND dedupe_key IS NOT NULL"))
Index("ix_tasks_claimable", Task.scope_id, Task.status, Task.available_at, Task.lease_expires_at)
Index("ix_embeddings_generation_status", MemeEmbedding.scope_id, MemeEmbedding.generation_id, MemeEmbedding.item_status)
Index("ix_visual_embeddings_match", MemeVisualEmbedding.scope_id, MemeVisualEmbedding.model, MemeVisualEmbedding.preprocess_version, MemeVisualEmbedding.meme_id)
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


def ensure_optional_control_schema(engine: Engine) -> None:
    """幂等创建图片处理与 operation grant 控制面表。

    该兼容保证只处理新增 ORM 表，既不推进也不回退 Alembic revision；生产部署仍
    应先运行标准 migration，启动期只负责让控制面安全可用；约束升级必须显式重建，
    不能把旧的三阶段同名 CHECK 当作已经兼容。
    """
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('mememeow:optional-control-schema'))"))
            connection.execute(text("ALTER TABLE task_lane_slots ADD COLUMN IF NOT EXISTS claim_generation BIGINT"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS submission_mode VARCHAR(16)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS image_stage VARCHAR(32)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS processing_job_id UUID"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_available BOOLEAN NOT NULL DEFAULT FALSE"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_reason VARCHAR(64)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_session_id VARCHAR(255)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS executor_attempt_id VARCHAR(255)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS workspace_selector VARCHAR(128)"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_attempt_count INTEGER NOT NULL DEFAULT 0"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS resume_started_at TIMESTAMPTZ"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS first_error JSONB"))
            connection.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS error_history JSONB NOT NULL DEFAULT '[]'::jsonb"))
            connection.execute(text("UPDATE tasks SET resume_attempt_count = COALESCE(resume_attempt_count, 0), error_history = COALESCE(error_history, '[]'::jsonb)"))
            connection.execute(text("ALTER TABLE tasks ALTER COLUMN resume_available SET DEFAULT FALSE, ALTER COLUMN resume_available SET NOT NULL, ALTER COLUMN resume_attempt_count SET DEFAULT 0, ALTER COLUMN resume_attempt_count SET NOT NULL, ALTER COLUMN error_history SET DEFAULT '[]'::jsonb, ALTER COLUMN error_history SET NOT NULL"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS executor_attempt_id VARCHAR(255)"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS resume_of_attempt_id VARCHAR(255)"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS workspace_selector VARCHAR(128)"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS processing_config_hash VARCHAR(64)"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS resume_available BOOLEAN NOT NULL DEFAULT FALSE"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS resume_reason VARCHAR(64)"))
            connection.execute(text("ALTER TABLE image_processing_attempts ADD COLUMN IF NOT EXISTS error JSONB"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_image_processing_attempt_executor_id ON image_processing_attempts(executor_attempt_id) WHERE executor_attempt_id IS NOT NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_image_processing_attempts_resume ON image_processing_attempts(scope_id, task_id, resume_available, updated_at)"))
            connection.execute(text("ALTER TABLE image_processing_jobs ADD COLUMN IF NOT EXISTS auto_name BOOLEAN NOT NULL DEFAULT FALSE"))
            # 旧兼容表可能已经有可空列；不能让 NULL 继续绕过 Job 选项契约。
            connection.execute(text("UPDATE image_processing_jobs SET auto_name = FALSE WHERE auto_name IS NULL"))
            connection.execute(text("ALTER TABLE image_processing_jobs ALTER COLUMN auto_name SET DEFAULT FALSE"))
            connection.execute(text("ALTER TABLE image_processing_jobs ALTER COLUMN auto_name SET NOT NULL"))
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS expected_revision BIGINT"))
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS claim_generation BIGINT"))
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS attempt INTEGER"))
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS task_id VARCHAR(255)"))
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS expected_title_fingerprint VARCHAR(64)"))
            Base.metadata.create_all(connection, tables=list(OPTIONAL_CONTROL_TABLES), checkfirst=True)
            # 旧安装可能已经存在同名三阶段约束。先删后建，确保启动兼容路径和
            # Alembic migration 对阶段/Task 映射使用完全相同的集合。
            connection.execute(text("""
                ALTER TABLE tasks DROP CONSTRAINT IF EXISTS fk_tasks_processing_job;
                ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_submission_mode;
                ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_image_stage;
                ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_submission_job_exclusivity;
                ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_image_stage_type;
                ALTER TABLE tasks ADD CONSTRAINT fk_tasks_processing_job
                    FOREIGN KEY (scope_id, processing_job_id)
                    REFERENCES image_processing_jobs(scope_id, id) ON DELETE CASCADE;
                ALTER TABLE tasks ADD CONSTRAINT ck_task_submission_mode
                    CHECK (submission_mode IS NULL OR submission_mode IN ('pipeline','standalone'));
                ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage
                    CHECK (image_stage IS NULL OR image_stage IN ('visual','agent','auto_rename','text_embedding'));
                ALTER TABLE tasks ADD CONSTRAINT ck_task_submission_job_exclusivity
                    CHECK (submission_mode IS NULL OR (submission_mode = 'standalone' AND processing_job_id IS NULL) OR (submission_mode = 'pipeline' AND processing_job_id IS NOT NULL));
                ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage_type
                    CHECK (task_type NOT IN ('visual_embedding_generation','meme_context_generation','image_auto_rename','text_embedding_generation') OR submission_mode IS NULL OR (image_stage IS NOT NULL AND ((task_type = 'visual_embedding_generation' AND image_stage = 'visual') OR (task_type = 'meme_context_generation' AND image_stage = 'agent') OR (task_type = 'image_auto_rename' AND image_stage = 'auto_rename') OR (task_type = 'text_embedding_generation' AND image_stage = 'text_embedding'))));
                ALTER TABLE image_processing_stages DROP CONSTRAINT IF EXISTS ck_image_processing_stage_name;
                ALTER TABLE image_processing_stages DROP CONSTRAINT IF EXISTS ck_image_processing_stage_status;
                ALTER TABLE image_processing_stages ADD CONSTRAINT ck_image_processing_stage_name
                    CHECK (stage IN ('visual','agent','auto_rename','text_embedding'));
                ALTER TABLE image_processing_stages ADD CONSTRAINT ck_image_processing_stage_status
                    CHECK (status IN ('queued','running','succeeded','failed','blocked','unknown_execution','skipped','warning'));
                ALTER TABLE image_processing_jobs DROP CONSTRAINT IF EXISTS ck_image_processing_policy;
                ALTER TABLE image_processing_jobs ADD CONSTRAINT ck_image_processing_policy
                    CHECK (reverse_image_policy IN ('forbid','auto'));
                ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_expected_revision;
                ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_claim_generation;
                ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_attempt;
                ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_title_fingerprint;
                ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_expected_revision
                    CHECK (expected_revision IS NULL OR expected_revision >= 1);
                ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_claim_generation
                    CHECK (claim_generation IS NULL OR claim_generation > 0);
                ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_attempt
                    CHECK (attempt IS NULL OR attempt > 0);
                ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_title_fingerprint
                    CHECK (expected_title_fingerprint IS NULL OR length(expected_title_fingerprint) = 64);
            """))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_tasks_image_submission ON tasks(scope_id, submission_mode, image_stage, processing_job_id, created_at)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_storage_operations_task ON storage_operations(scope_id, task_id, updated_at)"))
            connection.execute(text("ALTER TABLE image_processing_jobs ADD COLUMN IF NOT EXISTS processing_config JSONB NOT NULL DEFAULT '{}'::jsonb"))
            connection.execute(text("ALTER TABLE reverse_image_usage_events ADD COLUMN IF NOT EXISTS claim_generation BIGINT"))
            connection.execute(text("ALTER TABLE reverse_image_usage_events ADD COLUMN IF NOT EXISTS attempt INTEGER"))
            connection.execute(text("ALTER TABLE reverse_image_usage_events ADD COLUMN IF NOT EXISTS operation VARCHAR(128)"))
            connection.execute(text("ALTER TABLE reverse_image_usage_events ADD COLUMN IF NOT EXISTS target_sha256 VARCHAR(64)"))
            connection.execute(text("ALTER TABLE reverse_image_usage_events ADD COLUMN IF NOT EXISTS input_digest VARCHAR(64)"))
            connection.execute(text("ALTER TABLE search_migration_states ADD COLUMN IF NOT EXISTS model VARCHAR(255)"))
            connection.execute(text("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS source VARCHAR(64)"))
            connection.execute(text("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS units INTEGER"))
            connection.execute(text("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64)"))
            connection.execute(text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_source') THEN
                        ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_source
                            CHECK(source IS NULL OR (length(source) > 0 AND length(source) <= 64));
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_units') THEN
                        ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_units
                            CHECK(units IS NULL OR units > 0);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_fingerprint') THEN
                        ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_fingerprint
                            CHECK(request_fingerprint IS NULL OR length(request_fingerprint) = 64);
                    END IF;
                END $$;
            """))
    except SQLAlchemyError as exc:
        raise DatabaseError("control_schema_unavailable") from exc


def check_database(engine: Engine, *, expected_revision: str | None = None, require_local_installation: bool = True) -> dict[str, Any]:
    """执行连接、pgvector 和 Alembic revision 检查，并按部署模式校验 local 安装标记。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            vector_enabled = bool(connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar())
            revision = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
            installed = connection.execute(text("SELECT schema_revision FROM installation_state WHERE key='local'")).scalar() if require_local_installation else None
    except SQLAlchemyError as exc:
        raise DatabaseError("database_unavailable") from exc
    if not vector_enabled:
        raise DatabaseError("pgvector_missing")
    if revision is None:
        raise DatabaseError("schema_revision_missing")
    if expected_revision and revision != expected_revision:
        raise DatabaseError("schema_revision_mismatch")
    if require_local_installation:
        if installed is None:
            raise DatabaseError("installation_required")
        if installed != revision or (expected_revision and installed != expected_revision):
            raise DatabaseError("installation_revision_mismatch")
    return {"revision": revision, "installed": installed, "pgvector": True}


def initialize_local(engine: Engine, *, revision: str = CURRENT_SCHEMA_REVISION) -> None:
    """幂等创建 ``local`` scope 与安装标记，不扫描图片或扩展业务归属。"""
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

    def migration_state(self, model: str | None = None) -> SearchMigrationState | None:
        """读取当前 scope 的迁移状态，并避免跨模型复用 epoch。"""
        state = self.session.scalar(select(SearchMigrationState).where(SearchMigrationState.scope_id == self.scope.scope_id))
        if state is None or model is None or state.model is None or state.model == model:
            return state
        return None

    def begin_incremental_backfill(self, model: str, *, total_count: int = 0, legacy_generation_id: UUID | str | None = None) -> SearchMigrationState:
        """以新 epoch 开始当前 scope 的增量向量回填，并冻结旧 generation 来源。"""
        if not isinstance(model, str) or not model.strip():
            raise DatabaseError("migration_model_invalid")
        model = model.strip()
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise DatabaseError("migration_count_invalid")
        normalized_total = total_count
        state = self.migration_state()
        if state is None:
            state = SearchMigrationState(scope_id=self.scope.scope_id, model=model, mode="backfill", epoch=1, total_count=normalized_total)
            self.session.add(state)
        else:
            state.model = model
            state.mode = "backfill"
            state.epoch += 1
            state.completed_count = 0
            state.total_count = normalized_total
        # 新 epoch 没有合法旧 generation 时必须清掉上一次模型的引用，避免
        # 空 generation 边界下把旧模型的行当作当前迁移来源。
        state.legacy_generation_id = None
        if legacy_generation_id is None:
            head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model).with_for_update())
            legacy_generation_id = head.active_generation_id if head is not None else None
        if legacy_generation_id is not None:
            try:
                identifier = UUID(str(legacy_generation_id))
            except (TypeError, ValueError) as exc:
                raise DatabaseError("migration_generation_invalid") from exc
            generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == identifier, SearchGeneration.model == model, SearchGeneration.status == "active"))
            if generation is None:
                raise DatabaseError("migration_generation_invalid")
            state.legacy_generation_id = identifier
        state.updated_at = utcnow()
        self.session.flush()
        return state

    def record_incremental_backfill(self, *, epoch: int, completed_count: int, total_count: int | None = None, model: str | None = None) -> bool:
        """仅更新同一迁移 epoch 的回填进度，拒绝旧 Worker 覆盖新 epoch。"""
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return False
        requested_epoch = epoch
        if requested_epoch < 1:
            return False
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return False
        filters = [SearchMigrationState.scope_id == self.scope.scope_id, SearchMigrationState.mode == "backfill", SearchMigrationState.epoch == requested_epoch]
        if model is not None:
            filters.append(SearchMigrationState.model == str(model).strip())
        state = self.session.scalar(select(SearchMigrationState).where(*filters).with_for_update())
        if state is None:
            return False
        if isinstance(completed_count, bool) or not isinstance(completed_count, int) or completed_count < 0:
            return False
        requested_count = completed_count
        if total_count is not None and (isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0):
            return False
        requested_total = total_count if total_count is not None else state.total_count
        if not isinstance(requested_total, int) or requested_total < 0 or requested_count > requested_total:
            return False
        if state.completed_count < 0 or state.total_count < 0:
            return False
        # 回填进度只能前进；旧 worker 不能把新 epoch 的已完成计数回拨。
        if requested_count < state.completed_count:
            return False
        if total_count is not None and requested_total < state.total_count:
            return False
        state.completed_count = requested_count
        if total_count is not None:
            state.total_count = requested_total
        state.updated_at = utcnow()
        self.session.flush()
        return True

    def switch_incremental_only(self, *, epoch: int, model: str | None = None) -> bool:
        """在同一事务中将完成的回填 epoch 原子切换为增量唯一来源。"""
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            return False
        requested_epoch = epoch
        if requested_epoch < 1:
            return False
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return False
        filters = [SearchMigrationState.scope_id == self.scope.scope_id, SearchMigrationState.mode == "backfill", SearchMigrationState.epoch == requested_epoch]
        if model is not None:
            filters.append(SearchMigrationState.model == str(model).strip())
        state = self.session.scalar(select(SearchMigrationState).where(*filters).with_for_update())
        if state is None or state.completed_count < state.total_count:
            return False
        state.mode = "incremental_only"
        state.updated_at = utcnow()
        self.session.flush()
        return True

    @staticmethod
    def _metadata_hash(meme: Meme) -> str | None:
        """按数据库语境模型计算可 embedding metadata hash。"""
        try:
            from backend.metadata import MemeContext, Provenance, SidecarMetadata

            payload: dict[str, Any] = {
                "schema_version": meme.metadata_schema_version,
                "image": {
                    "relative_path": meme.storage_key,
                    "extension": meme.extension,
                    "size_bytes": meme.size_bytes,
                    "sha256": meme.sha256,
                },
                "context_status": meme.context_status,
                "meme_context": MemeContext.model_validate(meme.meme_context or {}).model_dump(mode="json", exclude_none=False),
                "provenance": Provenance.model_validate(meme.provenance or {}).model_dump(mode="json", exclude_none=False),
            }
            payload.update(meme.extensions or {})
            metadata = SidecarMetadata.model_validate(payload)
            serialized = json.dumps(metadata.model_dump(mode="json", exclude_none=False), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _incremental_rows(self, model: str) -> list[tuple[MemeTextEmbedding, Meme]]:
        """读取当前 scope 中通过 SHA、语境和 metadata hash 校验的单图向量。"""
        rows = self.session.execute(
            select(MemeTextEmbedding, Meme)
            .join(Meme, (Meme.scope_id == MemeTextEmbedding.scope_id) & (Meme.id == MemeTextEmbedding.meme_id))
            .where(
                MemeTextEmbedding.scope_id == self.scope.scope_id,
                MemeTextEmbedding.embedding_model_version == model,
                MemeTextEmbedding.dimensions == EMBEDDING_DIMENSIONS,
                MemeTextEmbedding.status == "ready",
                MemeTextEmbedding.embedding.is_not(None),
                Meme.context_status.in_(("partial", "ready")),
                Meme.sha256 == MemeTextEmbedding.image_sha256,
            )
            # 不在去重和 metadata 校验前截断结果；历史 hash 或损坏行可能占据
            # 前部，固定 limit 会让后面的有效 Meme 永远无法参与查询。
            .order_by(MemeTextEmbedding.meme_id.asc(), MemeTextEmbedding.updated_at.desc())
        ).all()
        valid: list[tuple[MemeTextEmbedding, Meme]] = []
        seen: set[UUID] = set()
        for row, meme in rows:
            if meme.id in seen or self._metadata_hash(meme) != row.metadata_hash:
                continue
            try:
                if len(row.embedding or []) != EMBEDDING_DIMENSIONS:
                    continue
            except TypeError:
                continue
            seen.add(meme.id)
            valid.append((row, meme))
        return valid

    def _legacy_rows(self, model: str) -> list[tuple[MemeEmbedding, Meme]]:
        """逐条校验迁移回退 generation 的 scope、版本、语境和安全 storage key。"""
        # 迁移状态一旦存在就代表旧 generation 来源已经被控制面冻结；即使
        # legacy_generation_id 为空，也不能重新读取会随时变化的 SearchHead。
        state = self.session.scalar(select(SearchMigrationState).where(SearchMigrationState.scope_id == self.scope.scope_id))
        if state is not None:
            if state.model is not None and state.model != model:
                return []
            generation_id = state.legacy_generation_id
        else:
            head = self.session.scalar(select(SearchHead).where(SearchHead.scope_id == self.scope.scope_id, SearchHead.model == model))
            generation_id = head.active_generation_id if head is not None else None
        if generation_id is None:
            return []
        generation = self.session.scalar(select(SearchGeneration).where(SearchGeneration.scope_id == self.scope.scope_id, SearchGeneration.id == generation_id, SearchGeneration.model == model, SearchGeneration.status == "active"))
        if generation is None or generation.dimensions != EMBEDDING_DIMENSIONS:
            return []
        rows = self.session.execute(
            select(MemeEmbedding, Meme)
            .join(Meme, (Meme.scope_id == MemeEmbedding.scope_id) & (Meme.id == MemeEmbedding.meme_id))
            .where(
                MemeEmbedding.scope_id == self.scope.scope_id,
                MemeEmbedding.generation_id == generation_id,
                MemeEmbedding.item_status == "ready",
                MemeEmbedding.meme_revision == Meme.revision,
                MemeEmbedding.image_sha256 == Meme.sha256,
                Meme.context_status.in_(("partial", "ready")),
            )
            # generation 与 meme_id 是复合主键；不能引用不存在的单列 id。
            .order_by(MemeEmbedding.meme_id.asc())
        ).all()
        valid: list[tuple[MemeEmbedding, Meme]] = []
        seen: set[UUID] = set()
        for row, meme in rows:
            if meme.id in seen or not isinstance(meme.storage_key, str):
                continue
            try:
                validate_business_storage_key(meme.storage_key)
                if row.dimensions != EMBEDDING_DIMENSIONS or len(row.embedding or []) != EMBEDDING_DIMENSIONS:
                    continue
            except (TypeError, ValueError):
                continue
            if self._metadata_hash(meme) != row.metadata_hash:
                continue
            seen.add(meme.id)
            valid.append((row, meme))
        return valid

    def source_mode(self, model: str) -> str:
        """选择一次查询唯一的数据来源，不混合 legacy 与增量向量。"""
        state = self.migration_state(model)
        if state is not None:
            return "incremental" if state.mode == "incremental_only" else "legacy"
        # 新控制面尚未建立迁移行时，以实际存在的增量向量作为安全来源；
        # 没有增量向量则兼容读取旧 active generation。
        return "incremental" if self._incremental_rows(model) else "legacy"

    def has_incremental(self, model: str) -> bool:
        """判断当前 scope 是否至少有一条可检索的单图向量。"""
        return bool(self._incremental_rows(model))

    def has_legacy(self, model: str) -> bool:
        """判断迁移回退 generation 是否仍有逐条校验通过的条目。"""
        return bool(self._legacy_rows(model))

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
        if self.source_mode(model) == "incremental":
            return self.query_incremental(model, vector, limit)
        ranked = self._query_legacy_validated(model, vector, limit)
        if not ranked:
            raise DatabaseError("cache_not_ready")
        return ranked

    def _query_legacy_validated(self, model: str, vector: Sequence[float], limit: int) -> list[tuple[UUID, float]]:
        """对通过旧 generation 逐条校验的向量执行单一来源排序。"""
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise DatabaseError("embedding_zero_norm")
        ranked: list[tuple[UUID, float]] = []
        for row, _meme in self._legacy_rows(model):
            try:
                candidate = [float(item) for item in row.embedding or []]
                candidate_norm = math.sqrt(sum(item * item for item in candidate))
                if len(candidate) != EMBEDDING_DIMENSIONS or not math.isfinite(candidate_norm) or candidate_norm <= 0:
                    continue
                score = sum(left * right for left, right in zip(values, candidate)) / (norm * candidate_norm)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            ranked.append((row.meme_id, float(score)))
        ranked.sort(key=lambda item: (-item[1], str(item[0])))
        return ranked[: max(1, min(int(limit), 100))]

    def query_incremental(self, model: str, vector: Sequence[float], limit: int = 5) -> list[tuple[UUID, float]]:
        """对当前有效单图向量执行稳定余弦排序，历史 hash 自动排除。"""
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise DatabaseError("embedding_dimensions_mismatch")
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isfinite(norm) or norm <= 0:
            raise DatabaseError("embedding_zero_norm")
        ranked: list[tuple[UUID, float]] = []
        for row, meme in self._incremental_rows(model):
            try:
                candidate = [float(item) for item in row.embedding or []]
                candidate_norm = math.sqrt(sum(item * item for item in candidate))
                if len(candidate) != EMBEDDING_DIMENSIONS or not math.isfinite(candidate_norm) or candidate_norm <= 0:
                    continue
                score = sum(left * right for left, right in zip(values, candidate)) / (norm * candidate_norm)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            ranked.append((meme.id, float(score)))
        if not ranked:
            raise DatabaseError("cache_not_ready")
        ranked.sort(key=lambda item: (-item[1], str(item[0])))
        return ranked[: max(1, min(int(limit), 100))]


def validate_visual_vector(vector: Sequence[float], *, dimensions: int = VISUAL_EMBEDDING_DIMENSIONS) -> list[float]:
    """校验视觉向量维度、有限性和范数，并返回 L2 归一化的普通列表。"""
    try:
        values = [float(value) for value in vector]
    except (TypeError, ValueError) as exc:
        raise DatabaseError("visual_embedding_invalid") from exc
    if len(values) != int(dimensions):
        raise DatabaseError("visual_embedding_dimensions_mismatch")
    if not all(math.isfinite(value) for value in values):
        raise DatabaseError("visual_embedding_non_finite")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0:
        raise DatabaseError("visual_embedding_zero_norm")
    return [value / norm for value in values]


class VisualEmbeddingRepository:
    """绑定 scope 的视觉向量写入和精确 cosine 查询 repository。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    @staticmethod
    def _identity(model: str, preprocess_version: str, dimensions: int) -> tuple[str, str, int]:
        """规范化向量空间身份，避免空模型或错误维度进入查询。"""
        if not isinstance(model, str) or not model.strip() or not isinstance(preprocess_version, str) or not preprocess_version.strip():
            raise DatabaseError("visual_model_not_configured")
        if int(dimensions) <= 0:
            raise DatabaseError("visual_embedding_dimensions_mismatch")
        return model.strip(), preprocess_version.strip(), int(dimensions)

    def get(
        self,
        meme_id: UUID | str,
        *,
        model: str,
        preprocess_version: str,
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
        image_sha256: str | None = None,
        for_update: bool = False,
    ) -> MemeVisualEmbedding | None:
        """读取当前 scope、模型空间和可选图片 SHA 对应的视觉向量。"""
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError):
            return None
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        filters = [
            MemeVisualEmbedding.scope_id == self.scope.scope_id,
            MemeVisualEmbedding.meme_id == identifier,
            MemeVisualEmbedding.model == model,
            MemeVisualEmbedding.preprocess_version == preprocess_version,
            MemeVisualEmbedding.dimensions == dimensions,
        ]
        if image_sha256 is not None:
            filters.append(MemeVisualEmbedding.image_sha256 == image_sha256)
        statement = select(MemeVisualEmbedding).where(*filters)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def upsert(
        self,
        meme_id: UUID | str,
        *,
        model: str,
        preprocess_version: str,
        image_sha256: str,
        embedding: Sequence[float],
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
    ) -> MemeVisualEmbedding:
        """校验并幂等写入当前图片版本的视觉向量。"""
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        normalized = validate_visual_vector(embedding, dimensions=dimensions)
        try:
            identifier = UUID(str(meme_id))
        except (ValueError, TypeError) as exc:
            raise DatabaseError("meme_not_found") from exc
        if not isinstance(image_sha256, str) or len(image_sha256) != 64 or any(character not in "0123456789abcdefABCDEF" for character in image_sha256):
            raise DatabaseError("visual_embedding_sha256_invalid")
        meme = self.session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == identifier).with_for_update())
        if meme is None:
            raise DatabaseError("meme_not_found")
        if meme.sha256 != image_sha256:
            raise DatabaseError("visual_embedding_sha256_mismatch")
        row = self.get(identifier, model=model, preprocess_version=preprocess_version, dimensions=dimensions, for_update=True)
        if row is None:
            row = MemeVisualEmbedding(scope_id=self.scope.scope_id, meme_id=identifier, model=model, preprocess_version=preprocess_version, dimensions=dimensions, image_sha256=image_sha256, embedding=normalized)
            self.session.add(row)
        else:
            row.image_sha256 = image_sha256
            row.embedding = normalized
            row.dimensions = dimensions
            row.updated_at = utcnow()
        self.session.flush()
        return row

    @staticmethod
    def agent_ready(meme: Meme) -> bool:
        """验证候选图片具有当前 SHA 对应的 research Agent provenance。"""
        if meme.context_status != "ready":
            return False
        summary = (meme.provenance or {}).get("agent_context")
        if not isinstance(summary, dict):
            return False
        return bool(
            summary.get("task_id")
            and summary.get("model")
            and summary.get("skill_hash")
            and summary.get("completed_at")
            and summary.get("image_sha256") == meme.sha256
        )

    def match(
        self,
        vector: Sequence[float],
        *,
        model: str,
        preprocess_version: str,
        dimensions: int = VISUAL_EMBEDDING_DIMENSIONS,
        limit: int = 20,
        exclude_meme_id: UUID | str | None = None,
    ) -> list[tuple[MemeVisualEmbedding, Meme, float]]:
        """在当前 scope 内精确查询合格候选并按分数和 Meme UUID 稳定排序。"""
        model, preprocess_version, dimensions = self._identity(model, preprocess_version, dimensions)
        normalized = validate_visual_vector(vector, dimensions=dimensions)
        limit = max(1, min(int(limit), 50))
        excluded: UUID | None = None
        if exclude_meme_id is not None:
            try:
                excluded = UUID(str(exclude_meme_id))
            except (ValueError, TypeError):
                excluded = None
        active_operation = select(StorageOperation.id).where(
            StorageOperation.scope_id == self.scope.scope_id,
            StorageOperation.meme_id == Meme.id,
            StorageOperation.status.in_(("prepared", "file_applied")),
        ).exists()
        distance = MemeVisualEmbedding.embedding.cosine_distance(normalized)
        statement = (
            select(MemeVisualEmbedding, Meme, (1 - distance).label("score"))
            .join(Meme, (Meme.scope_id == MemeVisualEmbedding.scope_id) & (Meme.id == MemeVisualEmbedding.meme_id))
            .where(
                MemeVisualEmbedding.scope_id == self.scope.scope_id,
                MemeVisualEmbedding.model == model,
                MemeVisualEmbedding.preprocess_version == preprocess_version,
                MemeVisualEmbedding.dimensions == dimensions,
                MemeVisualEmbedding.image_sha256 == Meme.sha256,
                ~active_operation,
            )
            .order_by(distance.asc(), MemeVisualEmbedding.meme_id.asc())
            .limit(max(limit * 8, 50))
        )
        if excluded is not None:
            statement = statement.where(MemeVisualEmbedding.meme_id != excluded)
        rows: list[tuple[MemeVisualEmbedding, Meme, float]] = []
        for embedding, meme, score in self.session.execute(statement).all():
            if not self.agent_ready(meme):
                continue
            rows.append((embedding, meme, float(score)))
            if len(rows) >= limit:
                break
        rows.sort(key=lambda item: (-item[2], str(item[1].id)))
        return rows

    def query(self, vector: Sequence[float], **kwargs: Any) -> list[tuple[UUID, float]]:
        """返回与文本 SearchRepository 对齐的 ``(meme_id, score)`` 结果。"""
        return [(meme.id, score) for _embedding, meme, score in self.match(vector, **kwargs)]


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

    def submit(
        self,
        *,
        task_type: str,
        payload: dict[str, Any],
        lane: str = "default",
        dedupe_key: str | None = None,
        settings_version: str | None = None,
        max_attempts: int = 3,
        task_id: str | None = None,
        lane_backpressure: int | None = None,
        submission_mode: str | None = None,
        image_stage: str | None = None,
        processing_job_id: UUID | str | None = None,
    ) -> Task:
        """在事务中插入或复用活动任务，并保存图片提交来源事实。

        ``submission_mode`` 等参数只应由图片控制面传入。为兼容无法回填来源的
        历史测试任务，未提供来源时保留 NULL，但任何带来源的新图片任务都会在
        这里校验阶段、Job 关联和模式互斥关系。
        """
        image_task_stages = {
            "visual_embedding_generation": "visual",
            "meme_context_generation": "agent",
            "image_auto_rename": "auto_rename",
            "text_embedding_generation": "text_embedding",
        }
        expected_stage = image_task_stages.get(task_type)
        if expected_stage is not None:
            submission_mode = submission_mode or (str(payload.get("submission_mode")) if payload.get("submission_mode") in {"pipeline", "standalone"} else None)
            if submission_mode not in {None, "pipeline", "standalone"}:
                raise DatabaseError("image_submission_mode_invalid")
            requested_stage = payload.get("stage")
            if requested_stage is not None and requested_stage != expected_stage:
                raise DatabaseError("image_stage_mismatch")
            candidate_job = processing_job_id or payload.get("job_id")
            if candidate_job is not None:
                try:
                    processing_job_id = UUID(str(candidate_job))
                except (TypeError, ValueError) as exc:
                    raise DatabaseError("image_processing_job_invalid") from exc
            # 无来源且无显式阶段的旧任务保留 NULL 阶段，供迁移脚本区分
            # “仍可兼容执行的旧 facade 任务”和“已经明确为历史图片阶段”的记录。
            explicit_source = image_stage is not None or requested_stage is not None or submission_mode is not None or processing_job_id is not None
            if explicit_source:
                image_stage = image_stage or (str(requested_stage) if requested_stage is not None else expected_stage)
            else:
                image_stage = None
            if image_stage is not None and image_stage != expected_stage:
                raise DatabaseError("image_stage_mismatch")
            if submission_mode == "pipeline" and processing_job_id is None:
                raise DatabaseError("image_processing_job_required")
            if submission_mode == "standalone" and processing_job_id is not None:
                raise DatabaseError("image_task_job_conflict")
            if submission_mode == "standalone" and payload.get("job_id") is not None:
                raise DatabaseError("image_task_job_conflict")
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
        task = Task(
            id=task_id or uuid.uuid4().hex,
            scope_id=self.scope.scope_id,
            task_type=task_type,
            submission_mode=submission_mode,
            image_stage=image_stage,
            processing_job_id=processing_job_id,
            lane=lane,
            payload=payload,
            dedupe_key=dedupe_key,
            settings_version=settings_version,
            max_attempts=max_attempts,
        )
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

    def _release_lane_slot(self, task_scope_id: str, task_id: str, *, owner: str | None = None, claim_generation: int | None = None) -> bool:
        """释放任务占用的数据库槽位；owner/generation 用于旧 Worker fencing。"""
        statement = select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == task_scope_id, TaskLaneSlot.task_id == task_id).with_for_update()
        slot = self.session.scalar(statement)
        if slot is None:
            return False
        if owner is not None and slot.lease_owner not in {None, owner}:
            return False
        if claim_generation is not None and slot.claim_generation not in {None, claim_generation}:
            return False
        slot.task_scope_id = None
        slot.task_id = None
        slot.lease_owner = None
        slot.claim_generation = None
        slot.lease_expires_at = None
        return True

    def slot_for_task(self, task_id: str) -> TaskLaneSlot | None:
        """读取当前 scope 任务占用的槽位，用于安全摘要和诊断。"""
        return self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == self.scope.scope_id, TaskLaneSlot.task_id == task_id))

    def recover_expired(self, *, owner: str, limit: int = 1000, exclude_task_types: set[str] | frozenset[str] | None = None, include_task_types: set[str] | frozenset[str] | None = None) -> list[str]:
        """恢复失效租约；可按任务类型限制专用 Worker 的恢复范围。"""
        now = utcnow()
        filters = [Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now]
        if exclude_task_types:
            filters.append(~Task.task_type.in_(exclude_task_types))
        if include_task_types:
            filters.append(Task.task_type.in_(include_task_types))
        rows = list(
            self.session.scalars(
                select(Task)
                .where(*filters)
                .order_by(Task.lease_expires_at, Task.id)
                .with_for_update(skip_locked=True)
                .limit(max(1, min(int(limit), 5000)))
            )
        )
        queued: list[str] = []
        for task in rows:
            previous_owner = task.lease_owner
            previous_generation = task.claim_generation
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
            self._release_lane_slot(task.scope_id, task.id, owner=previous_owner, claim_generation=previous_generation)
        self.session.flush()
        return queued

    def interrupt_owner(self, owner: str) -> int:
        """将当前 Worker 无法继续管理的 running 任务标记为可诊断失败。"""
        now = utcnow()
        rows = list(self.session.scalars(select(Task).where(Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_owner == owner).with_for_update(skip_locked=True)))
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
        safe_error = append_task_error_history(
            task,
            error,
            attempt=task.attempt_count,
            executor_attempt_id=task.executor_attempt_id,
            session_id=task.resume_session_id,
            occurred_at=utcnow().isoformat(),
        )
        task.error = safe_error
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
        slot.claim_generation = None
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
            # 视觉任务重试或其事务内创建的 Agent 子任务可能晚于批次封口；只允许
            # 这两种阶段关系加入，其他外部任务继续拒绝，避免 finalizer 观察到错误成员。
            child = self.get(task_id)
            if child is None or child.task_type not in {"visual_embedding_generation", "meme_context_generation"}:
                raise DatabaseError("batch_sealed")
            # 视觉重试可能在旧 finalizer 完成后补入 Agent；重新打开收束状态，
            # 让该子任务终态后再次提交文本索引，而不是沿用旧批次快照。
            if batch.finalizer_state == "complete":
                batch.finalizer_state = "pending"
                batch.finalized_at = None
        if self.session.scalar(select(TaskBatchItem).where(TaskBatchItem.scope_id == self.scope.scope_id, TaskBatchItem.batch_id == batch_id, TaskBatchItem.task_id == task_id)) is None:
            self.session.add(TaskBatchItem(scope_id=self.scope.scope_id, batch_id=batch_id, task_id=task_id))
            self.session.flush()

    def context_task_for_target(self, meme_id: UUID | str, image_sha256: str) -> Task | None:
        """读取当前 scope、图片版本对应的最近 Agent 任务，供视觉完成幂等衔接使用。"""
        dedupe_key = f"context:{meme_id}:{image_sha256}"
        return self.session.scalar(
            select(Task)
            .where(
                Task.scope_id == self.scope.scope_id,
                Task.task_type == "meme_context_generation",
                (Task.dedupe_key == dedupe_key) | Task.dedupe_key.like(f"{dedupe_key}:%"),
            )
            .order_by(Task.created_at.desc(), Task.id.desc())
        )

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

    def claim(self, *, owner: str, lease_seconds: int = 120, lane: str | None = None, task_id: str | None = None, lane_capacity: int | None = None, exclude_task_types: set[str] | frozenset[str] | None = None) -> Task | None:
        """使用 FOR UPDATE SKIP LOCKED 原子认领一个到期任务并递增 claim generation。"""
        now = utcnow()
        self.recover_expired(owner=owner, exclude_task_types=exclude_task_types)
        if lane and lane_capacity:
            self._lock_lane(lane)
            self._ensure_lane_slots(lane, lane_capacity)
        filters = [Task.scope_id == self.scope.scope_id, Task.status == "queued", Task.available_at <= now]
        if lane:
            filters.append(Task.lane == lane)
        if task_id:
            filters.append(Task.id == task_id)
        if exclude_task_types:
            filters.append(~Task.task_type.in_(exclude_task_types))
        statement = select(Task).where(*filters).order_by(Task.available_at, Task.created_at, Task.id).with_for_update(skip_locked=True).limit(1)
        task = self.session.scalar(statement)
        if task is None:
            filters = [Task.scope_id == self.scope.scope_id, Task.status == "running", Task.lease_expires_at < now, Task.attempt_count < Task.max_attempts]
            if lane:
                filters.append(Task.lane == lane)
            if task_id:
                filters.append(Task.id == task_id)
            if exclude_task_types:
                filters.append(~Task.task_type.in_(exclude_task_types))
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
        if lane and lane_capacity:
            slot = self.session.scalar(select(TaskLaneSlot).where(TaskLaneSlot.task_scope_id == task.scope_id, TaskLaneSlot.task_id == task.id).with_for_update())
            if slot is not None and slot.lease_owner == owner:
                slot.claim_generation = task.claim_generation
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
        if slot is not None and slot.lease_owner == owner and slot.claim_generation in {None, claim_generation}:
            slot.lease_expires_at = expires_at
            self.session.flush()
        return True

    def fail_fenced(self, task_id: str, claim_generation: int, owner: str, *, error: dict[str, Any], message: str, retry: bool = True, result: Any | None = None, retry_delay_seconds: int = 0, resume_available: bool | None = None, resume_reason: str | None = None, session_id: str | None = None, executor_attempt_id: str | None = None) -> tuple[bool, bool]:
        """在 fencing 条件下失败或重新排队任务，并追加有限错误历史。"""
        now = utcnow()
        task = self.session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
        if task is None or task.status != "running" or task.claim_generation != claim_generation or task.lease_owner != owner or not task.lease_expires_at or task.lease_expires_at <= now:
            return False, False
        safe_error = sanitize_error(error)
        task.first_error = sanitize_error(task.first_error) if isinstance(task.first_error, dict) else safe_error
        task.error_history = append_error_history(
            task.error_history,
            safe_error,
            attempt=task.attempt_count,
            executor_attempt_id=executor_attempt_id or task.executor_attempt_id,
            session_id=session_id or task.resume_session_id,
            occurred_at=now.isoformat(),
        )
        if session_id:
            task.resume_session_id = session_id
        if executor_attempt_id:
            task.executor_attempt_id = executor_attempt_id
        if resume_available is not None:
            task.resume_available = bool(
                resume_available
                and normalize_identifier(task.resume_session_id, kind="session")
                and normalize_identifier(task.executor_attempt_id, kind="attempt")
            )
            task.resume_reason = resume_reason
            if task.resume_available and task.resume_started_at is None:
                task.resume_started_at = now
        should_retry = bool(retry and task.attempt_count < task.max_attempts)
        if should_retry:
            task.status = "queued"
            task.available_at = now + timedelta(seconds=max(0, min(int(retry_delay_seconds), 3600)))
            task.message = message
            task.error = safe_error
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
            task.error = safe_error
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
            if requested_status == "succeeded":
                # 首次/历史错误由 first_error 和 error_history 保留；当前成功
                # 终态的主错误字段必须清空，避免 API 把成功显示为失败。
                values["error"] = None
                values["resume_available"] = False
                values["resume_reason"] = None
        values["updated_at"] = now
        result = self.session.execute(update(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id, Task.claim_generation == claim_generation, Task.lease_owner == owner, Task.status == "running", Task.lease_expires_at > now).values(**values))
        if result.rowcount != 1:
            return False
        if requested_status in {"succeeded", "failed"}:
            self._release_lane_slot(self.scope.scope_id, task_id, owner=owner, claim_generation=claim_generation)
            self.session.flush()
        return True

    def complete_fenced_with_provenance(self, task_id: str, claim_generation: int, owner: str, *, result: Any) -> bool:
        """原子提交图片 Agent 成功终态及反向图片 provenance。

        图片任务的审计结果和 Meme provenance 必须与 claim fencing 处于同一
        事务，否则查询方可能在任务已显示成功时读到尚未写入的 provenance。
        只有当前 owner、generation 和租约仍有效时才会修改两者。
        """
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
            return False
        task.status = "succeeded"
        task.progress = 1.0
        task.message = "任务完成"
        task.result = result
        task.error = None
        task.resume_available = False
        task.resume_reason = None
        task.completed_at = now
        task.updated_at = now
        task.lease_expires_at = None
        if task.task_type == "meme_context_generation":
            meme_id = (task.payload or {}).get("meme_id")
            if meme_id:
                try:
                    meme_id_value = UUID(str(meme_id))
                except (TypeError, ValueError):
                    meme_id_value = None
                if meme_id_value is not None:
                    try:
                        meme = self.session.scalar(
                            select(Meme)
                            .where(Meme.scope_id == self.scope.scope_id, Meme.id == meme_id_value)
                            .with_for_update()
                        )
                        if meme is not None:
                            # provenance 是可重建的附属事实；任务成功不能因为
                            # 历史用量行损坏而被回滚。
                            audit = ReverseImageUsageRepository(self.session, self.scope).aggregate_task(task_id)
                            provenance = dict(meme.provenance or {})
                            provenance["reverse_image"] = {
                                "policy": str((task.payload or {}).get("reverse_image_policy") or "forbid"),
                                **audit,
                            }
                            meme.provenance = provenance
                            meme.updated_at = now
                    except Exception:  # noqa: BLE001 - 附属写回失败不回滚任务终态
                        pass
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


class ReverseImageUsageRepository:
    """按 scope 读写反向图片用量事件并生成任务级审计摘要。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def get(self, request_id: str, *, for_update: bool = False) -> ReverseImageUsageEvent | None:
        """幂等读取当前 scope 的请求记录；全局 request_id 仍不会跨 scope 产生统计。"""
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_binding(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> ReverseImageUsageEvent | None:
        """按完整 callback 绑定读取旧 usage 事实，拒绝不确定的历史别名。

        callback 迁移只为 callback 表安装逻辑唯一约束；旧 usage 可能在进程崩溃窗口
        中先于 callback 行落盘。若完整绑定命中多条 usage，调用方不能猜测权威 ID，
        因此以稳定冲突错误停止恢复。
        """
        statement = select(ReverseImageUsageEvent).where(
            ReverseImageUsageEvent.scope_id == self.scope.scope_id,
            ReverseImageUsageEvent.task_id == task_id,
            ReverseImageUsageEvent.claim_generation == claim_generation,
            ReverseImageUsageEvent.attempt == attempt,
            ReverseImageUsageEvent.operation == operation,
            ReverseImageUsageEvent.target_sha256 == target_sha256,
            ReverseImageUsageEvent.input_digest == input_digest,
        ).order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
        if for_update:
            statement = statement.with_for_update()
        try:
            rows = list(self.session.scalars(statement))
        except SQLAlchemyError as exc:
            raise DatabaseError("usage_binding_unavailable") from exc
        if len(rows) > 1:
            raise DatabaseError("usage_request_conflict")
        return rows[0] if rows else None

    @staticmethod
    def _same_request(
        event: ReverseImageUsageEvent,
        *,
        task_id: str,
        cache_key: str,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """校验 request id 的完整执行绑定，防止旧 claim 借新输入复用事实。"""
        if event.task_id != task_id or event.cache_key != cache_key:
            raise DatabaseError("usage_request_conflict")
        expected = (claim_generation, attempt, operation, target_sha256, input_digest)
        actual = (event.claim_generation, event.attempt, event.operation, event.target_sha256, event.input_digest)
        # 旧事件的全空绑定仍可由旧兼容入口读取；一旦事件或本次请求带有
        # 执行绑定，就必须逐字段一致，不能用缺省字段绕过 request id 约束。
        if (any(value is not None for value in expected) or any(value is not None for value in actual)) and actual != expected:
            raise DatabaseError("usage_request_conflict")
        return event

    def create(
        self,
        *,
        request_id: str,
        task_id: str,
        meme_id: UUID | str | None,
        cache_key: str,
        cache_status: str,
        provider: str | None = None,
        claim_generation: int | None = None,
        attempt: int | None = None,
        operation: str | None = None,
        target_sha256: str | None = None,
        input_digest: str | None = None,
    ) -> ReverseImageUsageEvent:
        """创建一次逻辑检索事件；重复 request_id 返回现有事件以保持幂等。"""
        parsed_meme_id: UUID | None = None
        if meme_id:
            try:
                parsed_meme_id = UUID(str(meme_id))
            except (ValueError, TypeError) as exc:
                raise DatabaseError("meme_not_found") from exc
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        event = ReverseImageUsageEvent(
            request_id=request_id,
            scope_id=self.scope.scope_id,
            task_id=task_id,
            meme_id=parsed_meme_id,
            cache_key=cache_key,
            cache_status=cache_status,
            provider=provider,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            # 保存点只回滚这次幂等插入，不能撤销调用方 UOW 中已经完成的任务写入。
            with self.session.begin_nested():
                self.session.add(event)
                self.session.flush()
        except IntegrityError:
            existing = self.get(request_id, for_update=True)
            if existing is None:
                raise DatabaseError("usage_event_conflict")
            return self._same_request(
                existing,
                task_id=task_id,
                cache_key=cache_key,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
        return event

    def mark_provider_started(self, request_id: str) -> ReverseImageUsageEvent:
        """原子标记供应商逻辑调用已开始，计数只会从 false 变为 true 一次。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        if event.completed_at is not None:
            return event
        if not event.provider_called:
            event.provider_called = True
            event.provider_started_at = utcnow()
            event.outcome = "started"
            self.session.flush()
        return event

    def finish(
        self,
        request_id: str,
        *,
        cache_status: str | None = None,
        outcome: str,
        retryable: bool = False,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> ReverseImageUsageEvent:
        """写入供应商或缓存终态；失败事件保留以供恢复和审计。"""
        event = self.get(request_id, for_update=True)
        if event is None:
            raise DatabaseError("usage_event_not_found")
        # 已完成事件是幂等终态；重试不能用另一份响应覆盖原始审计事实。
        if event.completed_at is not None:
            return event
        if cache_status is not None:
            event.cache_status = cache_status
        event.outcome = outcome
        event.retryable = retryable
        event.result = result
        event.error = error
        event.completed_at = utcnow()
        self.session.flush()
        return event

    def aggregate_task(self, task_id: str) -> dict[str, Any]:
        """按当前 scope 聚合任务事件，返回不含图片身份和供应商秘密的摘要。"""
        rows = list(
            self.session.scalars(
                select(ReverseImageUsageEvent)
                .where(
                    ReverseImageUsageEvent.scope_id == self.scope.scope_id,
                    ReverseImageUsageEvent.task_id == task_id,
                )
                .order_by(ReverseImageUsageEvent.created_at.asc(), ReverseImageUsageEvent.id.asc())
            )
        )
        completed = sorted(
            (row for row in rows if row.completed_at is not None),
            key=lambda row: (row.completed_at, row.created_at, row.id),
        )
        return {
            "attempted": bool(rows),
            "used": any(row.outcome in {"success", "empty"} and bool((row.result or {}).get("used", True)) for row in rows),
            "cache_hits": sum(1 for row in rows if row.cache_status == "hit"),
            "provider_calls": sum(1 for row in rows if row.provider_called),
            "outcome": completed[-1].outcome if completed else ("started" if rows else "not_requested"),
            "request_count": len(rows),
        }


def _validate_callback_binding_values(
    *,
    request_id: str | None,
    task_id: str,
    claim_generation: int,
    attempt: int,
    operation: str,
    target_sha256: str,
    input_digest: str,
) -> None:
    """在事实层拒绝缺失或不完整的 callback 绑定字段。

    API 层已经验证过 callback 输入，但内存夹具、迁移工具和其它宿主适配器也可能
    直接调用 repository；这些路径不能因为绕过 HTTP 校验而退回 request-ID-only
    语义。数据库约束负责并发唯一性，这里负责在进入索引前保持两种事实层一致。
    """
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for char in request_id)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")
    if (
        not isinstance(task_id, str)
        or not task_id
        or len(task_id) > 255
        or isinstance(claim_generation, bool)
        or not isinstance(claim_generation, int)
        or claim_generation < 1
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt < 1
        or not isinstance(operation, str)
        or not operation
        or len(operation) > 128
        or not isinstance(target_sha256, str)
        or len(target_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in target_sha256)
        or not isinstance(input_digest, str)
        or len(input_digest) != 64
        or any(char not in "0123456789abcdef" for char in input_digest)
    ):
        raise DatabaseError("callback_binding_schema_unavailable")


class AgentCallbackRequestRepository:
    """按 scope 保存 callback request 绑定，供只读和副作用 callback 复用。"""

    def __init__(self, session: Session, scope: ScopeContext):
        self.session, self.scope = session, scope

    def ensure_schema_ready(self) -> bool:
        """确认 callback 复合唯一索引和绑定字段已经安装。

        callback 是可选的内部路径，不能因为旧安装尚未迁移而阻断 local 直连；但一旦
        请求进入该 repository，缺失索引、可空事实或表结构异常必须 fail-closed。
        """
        try:
            bind = self.session.get_bind()
            if bind is None or bind.dialect.name != "postgresql":
                raise DatabaseError("callback_binding_schema_unavailable")
            required_columns = (
                "scope_id",
                "task_id",
                "claim_generation",
                "attempt",
                "operation",
                "target_sha256",
                "input_digest",
            )
            columns_sql = ", ".join(f"'{column}'" for column in required_columns)
            row = self.session.execute(
                text(
                    f"""
                    SELECT
                        EXISTS(
                            SELECT 1
                              FROM pg_indexes
                             WHERE schemaname = current_schema()
                               AND tablename = 'agent_callback_requests'
                               AND indexname = 'uq_agent_callback_requests_logical'
                        ),
                        (
                            SELECT count(*)
                              FROM information_schema.columns
                             WHERE table_schema = current_schema()
                               AND table_name = 'agent_callback_requests'
                               AND column_name IN ({columns_sql})
                               AND is_nullable = 'NO'
                        )
                    """
                )
            ).one()
        except (SQLAlchemyError, DatabaseError) as exc:
            if isinstance(exc, DatabaseError):
                raise
            raise DatabaseError("callback_binding_schema_unavailable") from exc
        if not bool(row[0]) or int(row[1]) != len(required_columns):
            raise DatabaseError("callback_binding_schema_unavailable")
        return True

    # 启动检查和隐藏测试使用的显式命名，均保持同一事实来源。
    is_schema_ready = ensure_schema_ready

    def get(self, request_id: str, *, for_update: bool = False) -> AgentCallbackRequest | None:
        """读取当前 scope 的 request 事实，不让 request id 跨 scope 产生命中。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.request_id == request_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_by_logical(
        self,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
        for_update: bool = False,
    ) -> AgentCallbackRequest | None:
        """按完整执行绑定和规范化输入读取唯一权威 callback 事实。"""
        statement = select(AgentCallbackRequest).where(
            AgentCallbackRequest.scope_id == self.scope.scope_id,
            AgentCallbackRequest.task_id == task_id,
            AgentCallbackRequest.claim_generation == claim_generation,
            AgentCallbackRequest.attempt == attempt,
            AgentCallbackRequest.operation == operation,
            AgentCallbackRequest.target_sha256 == target_sha256,
            AgentCallbackRequest.input_digest == input_digest,
        )
        if for_update:
            statement = statement.with_for_update()
        try:
            return self.session.scalar(statement)
        except SQLAlchemyError as exc:
            # 历史重复逻辑键在迁移前不能猜测哪一行权威，读取也必须停止。
            raise DatabaseError("callback_binding_schema_unavailable") from exc

    @staticmethod
    def _same(
        row: AgentCallbackRequest,
        *,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """比较 callback request 的完整可信绑定，拒绝改绑重放。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def create(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """创建或复用 callback 逻辑事实，request ID 仅作为兼容性提示。"""
        return self.resolve(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )

    def resolve(
        self,
        *,
        request_id: str | None,
        task_id: str,
        claim_generation: int,
        attempt: int,
        operation: str,
        target_sha256: str,
        input_digest: str,
    ) -> AgentCallbackRequest:
        """按 request ID 和完整逻辑键解析唯一权威 callback 事实。

        解析顺序先比较当前 scope 下的 request ID，再查逻辑唯一键，最后才插入。并发
        插入遇到复合唯一约束冲突时重新读取竞争者事实，绝不通过新 ID 生成第三行。
        """
        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        self.ensure_schema_ready()
        existing = self.get(request_id, for_update=True) if request_id is not None else None
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        authoritative = self.get_by_logical(
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
            for_update=True,
        )
        if authoritative is not None:
            return authoritative
        if request_id is None:
            from backend.callbacks import canonical_callback_request_id

            request_id = canonical_callback_request_id(input_digest)
        existing = self.get(request_id, for_update=True)
        if existing is not None:
            return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
        row = AgentCallbackRequest(
            scope_id=self.scope.scope_id,
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError as exc:
            authoritative = self.get_by_logical(
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
                for_update=True,
            )
            if authoritative is not None:
                return authoritative
            existing = self.get(request_id, for_update=True)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            raise DatabaseError("callback_request_conflict") from exc
        return row

    def finish(
        self,
        request_id: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> AgentCallbackRequest:
        """以幂等终态保存 callback 结果，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        row = self.get(request_id, for_update=True)
        if row is None:
            raise DatabaseError("callback_request_not_found")
        if row.completed_at is not None:
            # ``unknown_execution`` 只表示外部调用当时无法确认；随后若持久 usage
            # 事实已经明确成功或失败，可以收束为对应终态，但普通重试不会重新触发
            # provider，因为调用方会先读取该终态。
            if row.state != "unknown_execution" or state not in {"completed", "failed"}:
                return row
        row.state = state
        row.result = result
        row.error = error
        row.completed_at = utcnow()
        row.updated_at = row.completed_at
        self.session.flush()
        return row


@dataclass(slots=True)
class InMemoryCallbackRequest:
    """单进程 callback 事实夹具，字段与 PostgreSQL 模型保持一致。"""

    scope_id: str
    request_id: str
    task_id: str
    claim_generation: int
    attempt: int
    operation: str
    target_sha256: str
    input_digest: str
    state: str = "started"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = None  # type: ignore[assignment]
    updated_at: datetime = None  # type: ignore[assignment]
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """为内存夹具补齐与 ORM 默认值等价的创建时间。"""
        now = utcnow()
        if self.created_at is None:
            self.created_at = now
        if self.updated_at is None:
            self.updated_at = self.created_at


class InMemoryAgentCallbackRequestRepository:
    """提供与 PostgreSQL 相同双索引语义的单进程 callback repository。

    该夹具用于快速单元测试；生产并发门禁仍由 PostgreSQL 复合唯一索引提供。锁内
    同时维护 request ID 和完整逻辑键，确保不同 ID 的首次竞态只能得到同一事实。
    """

    def __init__(self, scope: ScopeContext | str = SCOPE_LOCAL):
        self.scope = scope if isinstance(scope, ScopeContext) else ScopeContext(scope)
        self._by_id: dict[str, InMemoryCallbackRequest] = {}
        self._by_logical: dict[tuple[str, int, int, str, str, str], InMemoryCallbackRequest] = {}
        self._lock = RLock()

    @staticmethod
    def _logical_key(*, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> tuple[str, int, int, str, str, str]:
        """构造与数据库唯一约束字段顺序一致的内存索引键。"""
        return (task_id, claim_generation, attempt, operation, target_sha256, input_digest)

    @staticmethod
    def _same(row: InMemoryCallbackRequest, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """比较完整绑定，拒绝 request ID 改绑。"""
        actual = (row.task_id, row.claim_generation, row.attempt, row.operation, row.target_sha256, row.input_digest)
        expected = (task_id, claim_generation, attempt, operation, target_sha256, input_digest)
        if actual != expected:
            raise DatabaseError("callback_request_conflict")
        return row

    def get(self, request_id: str) -> InMemoryCallbackRequest | None:
        """读取当前 scope 的 request ID 事实。"""
        with self._lock:
            return self._by_id.get(request_id)

    def get_by_logical(self, *, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest | None:
        """按完整逻辑键读取权威事实。"""
        with self._lock:
            return self._by_logical.get(self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest))

    def resolve(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """按 ID、逻辑键和确定性 ID 的顺序解析或创建 callback 事实。"""
        from backend.callbacks import canonical_callback_request_id

        _validate_callback_binding_values(
            request_id=request_id,
            task_id=task_id,
            claim_generation=claim_generation,
            attempt=attempt,
            operation=operation,
            target_sha256=target_sha256,
            input_digest=input_digest,
        )
        with self._lock:
            existing = self._by_id.get(request_id) if request_id is not None else None
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            key = self._logical_key(task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            authoritative = self._by_logical.get(key)
            if authoritative is not None:
                return authoritative
            selected_id = request_id or canonical_callback_request_id(input_digest)
            existing = self._by_id.get(selected_id)
            if existing is not None:
                return self._same(existing, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)
            row = InMemoryCallbackRequest(
                scope_id=self.scope.scope_id,
                request_id=selected_id,
                task_id=task_id,
                claim_generation=claim_generation,
                attempt=attempt,
                operation=operation,
                target_sha256=target_sha256,
                input_digest=input_digest,
            )
            self._by_id[selected_id] = row
            self._by_logical[key] = row
            return row

    def create(self, *, request_id: str | None, task_id: str, claim_generation: int, attempt: int, operation: str, target_sha256: str, input_digest: str) -> InMemoryCallbackRequest:
        """兼容旧调用名，执行双索引 resolve。"""
        return self.resolve(request_id=request_id, task_id=task_id, claim_generation=claim_generation, attempt=attempt, operation=operation, target_sha256=target_sha256, input_digest=input_digest)

    def finish(self, request_id: str, *, state: str, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> InMemoryCallbackRequest:
        """幂等保存 callback 终态，不覆盖已完成事实。"""
        if state not in {"completed", "failed", "unknown_execution"}:
            raise DatabaseError("callback_request_state_invalid")
        with self._lock:
            row = self._by_id.get(request_id)
            if row is None:
                raise DatabaseError("callback_request_not_found")
            if row.completed_at is not None and (row.state != "unknown_execution" or state not in {"completed", "failed"}):
                return row
            row.state = state
            row.result = result
            row.error = error
            row.completed_at = utcnow()
            row.updated_at = row.completed_at
            return row


# 兼容不同测试和宿主命名，避免为 callback 事实引入第二套 repository。
InMemoryCallbackRequestRepository = InMemoryAgentCallbackRequestRepository


class DataEnvironment:
    """请求或任务级 scope-bound Session、repository 和 BlobStore 组合。"""

    def __init__(self, factory: sessionmaker[Session], scope: ScopeContext):
        self.uow = UnitOfWork(factory, scope)
        self.scope = scope
        self.memes = MemeRepository(self.uow.session, scope)
        self.collections = CollectionRepository(self.uow.session, scope)
        self.search = SearchRepository(self.uow.session, scope)
        self.visual = VisualEmbeddingRepository(self.uow.session, scope)
        # 兼容领域层较直白的命名，调用方不得绕过 scope-bound repository。
        self.visual_embeddings = self.visual
        self.tasks = TaskRepository(self.uow.session, scope)
        self.reverse_image_usage = ReverseImageUsageRepository(self.uow.session, scope)
        self.callback_requests = AgentCallbackRequestRepository(self.uow.session, scope)

    def __enter__(self) -> "DataEnvironment":
        self.uow.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.uow.__exit__(exc_type, exc, tb)


class BlobStore:
    """绑定 scope 的文件存储；local 使用现有图片根目录，其他 scope 独立命名空间。"""

    def __init__(self, *, root: Path, scope: ScopeContext, storage_namespace: UUID | None = None, local: bool = False):
        self.scope = scope
        try:
            base_root = validate_controlled_root(root, create=True, writable=True)
            candidate = base_root if local else base_root / "scopes" / str(storage_namespace or uuid.uuid4()) / "images"
            self.root = validate_controlled_root(candidate, create=True, writable=True)
        except StorageRootError as exc:
            raise DatabaseError(str(exc)) from exc
        self.staging_root = self.root / ".staging"
        self.quarantine_root = self.root / ".quarantine"
        try:
            self.staging_root = validate_controlled_root(self.staging_root, create=True, writable=True)
            self.quarantine_root = validate_controlled_root(self.quarantine_root, create=True, writable=True)
            # BlobStore 也可能被离线迁移工具直接构造，确保非 Compose 夹具仍遵守目录契约。
            os.chmod(self.root, 0o700)
            os.chmod(self.staging_root, 0o700)
            os.chmod(self.quarantine_root, 0o700)
        except (StorageRootError, OSError) as exc:
            raise DatabaseError("storage_root_permissions_invalid") from exc

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

    def __init__(self, resources: "DatabaseResources", *, scope_id: str | ScopeContext = SCOPE_LOCAL):
        """创建绑定 scope 的协调器；local 默认仅保留给开源兼容夹具。"""
        self.resources = resources
        self.scope = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        self.blob_store = resources.blob_store_for_scope(self.scope.scope_id)

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

    @staticmethod
    def _title_fingerprint(record: Meme) -> str:
        """按自动命名 handler 的规则计算当前语境标题指纹。"""
        context = record.meme_context if isinstance(record.meme_context, dict) else {}
        raw_title = context.get("title") if isinstance(context, dict) else None
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        return hashlib.sha256(title.encode("utf-8")).hexdigest()

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
                operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
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
            operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.operation_token == token,
                )
                .with_for_update()
            )
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

    def rename_if_current(
        self,
        meme_id: UUID | str,
        *,
        target_key: str,
        expected_source_key: str,
        expected_sha256: str,
        expected_revision: int,
        task_id: str,
        claim_generation: int,
        attempt: int,
        claim_owner: str,
        expected_title_fingerprint: str | None = None,
    ) -> Meme:
        """在任务 claim 与 Meme 事实仍匹配时执行一次 CAS 重命名。

        第一段事务锁定 Meme/Task 并记录 ``StorageOperation``，文件移动完成后第二段
        事务再次复核所有 fencing 输入。任一复核失败都会阻断操作恢复，避免未知文件
        副作用被当作普通命名警告；实际 claim owner 必须与当前 Task lease owner 完全
        一致，不能只依赖 generation 和 attempt。

        ``storage_key_changed`` 只表示同一 SHA 的 Meme 已经被人工改名，调用方可将其
        降级为 warning；SHA、revision、语境指纹、claim 或文件副作用无法确认时必须
        保持 blocked/unknown_execution。
        """
        try:
            validate_business_storage_key(target_key)
            validate_business_storage_key(expected_source_key)
        except ValueError as exc:
            raise DatabaseError("invalid_filename") from exc
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
            or not isinstance(task_id, str)
            or not task_id
            or not isinstance(claim_owner, str)
            or not claim_owner
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or not isinstance(claim_generation, int)
            or claim_generation < 1
            or not isinstance(attempt, int)
            or attempt < 1
        ):
            raise DatabaseError("target_changed")

        def current_title_fingerprint(record: Meme) -> str:
            """从数据库中的当前标题计算与 handler 一致的输入指纹。"""
            return self._title_fingerprint(record)

        def mark_blocked(error: str) -> None:
            """在文件副作用已发生但 finalize 不确定时持久化 blocked。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    if operation is not None and operation.status in self._ACTIVE:
                        # finalize 失败时不再复用可能已抛错的状态转移 helper，直接
                        # 持久化 blocked 事实，确保恢复器不会把副作用当作可重放。
                        operation.status = "blocked"
                        operation.error = {"error": error}
                        operation.updated_at = utcnow()
                        session.flush()
            except Exception:  # noqa: BLE001 - 数据库本身不可用时保留原始异常
                return

        def compensate_manual_replacement() -> bool:
            """识别文件移动前同图手动改名，并安全结束未执行的操作。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    record = session.scalar(
                        select(Meme)
                        .where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id)))
                        .with_for_update()
                    )
                    if operation is None or operation.status != "prepared" or record is None:
                        return False
                    title_matches = expected_title_fingerprint is None or current_title_fingerprint(record) == expected_title_fingerprint
                    same_image = record.sha256.lower() == expected_sha256.lower()
                    current_is_target = (
                        record.storage_key == target_key
                        and record.revision == expected_revision + 1
                        and self.blob_store.exists_with_identity(target_key, sha256=expected_sha256, size_bytes=record.size_bytes)
                    )
                    current_is_replacement = record.storage_key != source_key and not current_is_target
                    target_path = self.blob_store._key_path(target_key, must_exist=False)
                    target_absent = not target_path.exists() and not target_path.is_symlink()
                    if not (same_image and title_matches and current_is_replacement and target_absent):
                        if not (same_image and title_matches and current_is_target):
                            return False
                    self._set_status(operation, "compensated", error={"error": "storage_key_changed"}, session=session)
                    return True
            except Exception:  # noqa: BLE001 - 无法确认时必须保留 unknown 语义
                return False

        def compensate_unapplied_target_conflict() -> bool:
            """在目标文件于预检后被占用时补偿尚未发生的文件动作。"""
            try:
                with self._transaction() as session:
                    operation = session.scalar(
                        select(StorageOperation)
                        .where(
                            StorageOperation.scope_id == self.scope.scope_id,
                            StorageOperation.operation_token == token,
                        )
                        .with_for_update()
                    )
                    record = session.scalar(
                        select(Meme)
                        .where(
                            Meme.scope_id == self.scope.scope_id,
                            Meme.id == UUID(str(meme_id)),
                        )
                        .with_for_update()
                    )
                    if operation is None or operation.status != "prepared" or record is None:
                        return False
                    if (
                        record.storage_key != source_key
                        or record.revision != expected_revision
                        or record.sha256.lower() != expected_sha256.lower()
                        or (expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint)
                    ):
                        return False
                    source_ok = self.blob_store.exists_with_identity(
                        source_key,
                        sha256=expected_sha256,
                        size_bytes=record.size_bytes,
                    )
                    target_path = self.blob_store._key_path(target_key, must_exist=False)
                    if not source_ok or not target_path.exists() or target_path.is_symlink():
                        return False
                    self._set_status(operation, "compensated", error={"error": "target_exists"}, session=session)
                    return True
            except Exception:  # noqa: BLE001 - 无法证明未发生副作用时保留未知语义
                return False

        token = uuid.uuid4()
        source_key = expected_source_key
        expected_size: int | None = None
        try:
            with self._transaction() as session:
                record = session.scalar(
                    select(Meme).where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.id == UUID(str(meme_id)),
                    ).with_for_update()
                )
                task = session.scalar(
                    select(Task).where(
                        Task.scope_id == self.scope.scope_id,
                        Task.id == task_id,
                    ).with_for_update()
                )
                now = utcnow()
                if (
                    task is None
                    or task.task_type != "image_auto_rename"
                    or task.image_stage != "auto_rename"
                    or task.status != "running"
                    or task.claim_generation != claim_generation
                    or task.attempt_count != attempt
                    or task.lease_expires_at is None
                    or task.lease_expires_at <= now
                    or task.lease_owner != claim_owner
                ):
                    raise DatabaseError("claim_expired")
                if record is None or record.sha256.lower() != expected_sha256.lower():
                    raise DatabaseError("target_changed")
                if expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint:
                    raise DatabaseError("target_changed")
                if record.storage_key != expected_source_key:
                    raise DatabaseError("storage_key_changed")
                if record.revision != expected_revision:
                    raise DatabaseError("target_changed")
                # 同一 Meme 的既有存储操作可能仍有未确认副作用；即使本次派生结果
                # 与当前文件同名，也不能绕过 blocked/活动操作的 fail-closed 边界。
                unsettled_operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.meme_id == record.id,
                        StorageOperation.status.in_(("prepared", "file_applied", "blocked")),
                    )
                    .order_by(StorageOperation.updated_at.desc(), StorageOperation.id.desc())
                    .with_for_update()
                )
                if unsettled_operation is not None:
                    raise DatabaseError("storage_operation_unknown")
                if target_key == record.storage_key:
                    # 目标名已经符合派生结果时仍需经过上面的 Task claim/CAS
                    # 校验和文件身份复核；文件被外部替换时不能把旧路径当作成功。
                    if not self.blob_store.exists_with_identity(
                        record.storage_key,
                        sha256=expected_sha256,
                        size_bytes=record.size_bytes,
                    ):
                        raise DatabaseError("target_changed")
                    # 复核通过后避免无意义地创建 storage operation。
                    return record
                if session.scalar(
                    select(Meme.id).where(
                        Meme.scope_id == self.scope.scope_id,
                        Meme.storage_key == target_key,
                        Meme.id != record.id,
                    )
                ) is not None:
                    raise DatabaseError("target_exists")
                target_path = self.blob_store._key_path(target_key)
                if target_path.exists() or target_path.is_symlink():
                    raise DatabaseError("target_exists")
                expected_size = record.size_bytes
                operation = StorageOperation(
                    scope_id=self.scope.scope_id,
                    meme_id=record.id,
                    operation_type="rename",
                    operation_token=token,
                    source_key=record.storage_key,
                    target_key=target_key,
                    before_sha256=record.sha256,
                    after_sha256=record.sha256,
                    before_size=record.size_bytes,
                    after_size=record.size_bytes,
                    expected_revision=expected_revision,
                    claim_generation=claim_generation,
                    attempt=attempt,
                    task_id=task_id,
                    expected_title_fingerprint=expected_title_fingerprint,
                    status="prepared",
                )
                session.add(operation)
                session.flush()
            # 数据库锁不能阻止外部进程替换文件；移动前复核源对象身份，避免把
            # 同名但不同字节的文件绑定到当前 Meme。
            if not self.blob_store.exists_with_identity(
                source_key,
                sha256=expected_sha256,
                size_bytes=expected_size,
            ):
                mark_blocked("source_identity_changed")
                raise DatabaseError("target_changed")
            try:
                self.blob_store.link_move(source_key, target_key)
            except (DatabaseError, OSError) as exc:
                if compensate_manual_replacement():
                    raise DatabaseError("storage_key_changed") from exc
                if isinstance(exc, DatabaseError) and exc.code == "target_exists" and compensate_unapplied_target_conflict():
                    raise DatabaseError("target_exists") from exc
                # 预检通过后文件动作仍可能在 link/unlink 边界失败；此时不能把
                # 未知副作用当作普通目标冲突，必须留下 blocked 事实交给恢复器。
                mark_blocked("rename_file_move_unknown")
                raise DatabaseError("storage_operation_unknown") from exc
            try:
                target_verified = self.blob_store.exists_with_identity(
                    target_key,
                    sha256=expected_sha256,
                    size_bytes=expected_size,
                )
                source_path = self.blob_store._key_path(source_key, must_exist=False)
                source_absent = not source_path.exists() and not source_path.is_symlink()
            except (DatabaseError, OSError) as exc:
                mark_blocked("rename_file_identity_unknown")
                raise DatabaseError("storage_operation_unknown") from exc
            if not target_verified or not source_absent:
                mark_blocked("rename_file_identity_mismatch")
                raise DatabaseError("storage_operation_unknown")
        except Exception:
            # ``prepared`` 操作必须交给恢复器判断；不要删除可能已完成的文件移动。
            raise

        blocked_error: str | None = None
        try:
            with self._transaction() as session:
                operation = session.scalar(
                    select(StorageOperation)
                    .where(
                        StorageOperation.scope_id == self.scope.scope_id,
                        StorageOperation.operation_token == token,
                    )
                    .with_for_update()
                )
                record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))).with_for_update())
                task = session.scalar(select(Task).where(Task.scope_id == self.scope.scope_id, Task.id == task_id).with_for_update())
                now = utcnow()
                if operation is None or record is None:
                    blocked_error = "storage_operation_missing"
                elif (
                    task is None
                    or task.task_type != "image_auto_rename"
                    or task.image_stage != "auto_rename"
                    or task.status != "running"
                    or task.claim_generation != claim_generation
                    or task.attempt_count != attempt
                    or task.lease_expires_at is None
                    or task.lease_expires_at <= now
                    or task.lease_owner != claim_owner
                ):
                    blocked_error = "claim_expired"
                elif record.sha256.lower() != expected_sha256.lower():
                    blocked_error = "target_changed"
                elif expected_title_fingerprint is not None and current_title_fingerprint(record) != expected_title_fingerprint:
                    blocked_error = "target_changed"
                elif record.storage_key != expected_source_key:
                    blocked_error = "storage_key_changed" if record.sha256.lower() == expected_sha256.lower() else "target_changed"
                elif record.revision != expected_revision:
                    blocked_error = "target_changed"
                else:
                    target_verified = self.blob_store.exists_with_identity(
                        target_key,
                        sha256=expected_sha256,
                        size_bytes=expected_size,
                    )
                    source_path = self.blob_store._key_path(source_key, must_exist=False)
                    if not target_verified or source_path.exists() or source_path.is_symlink():
                        blocked_error = "rename_file_identity_mismatch"
                    else:
                        self._session = session
                        self._set_status(operation, "file_applied", session=session)
                        record.storage_key = target_key
                        record.revision += 1
                        record.updated_at = utcnow()
                        self._set_status(operation, "completed", session=session)
                        session.flush()
        except Exception as exc:  # noqa: BLE001 - 文件已移动，finalize 异常必须留痕
            mark_blocked("unknown_execution")
            raise DatabaseError("storage_operation_unknown") from exc
        if blocked_error is not None:
            # 文件已移动但数据库事实无法安全收束，operation 保持 blocked，由恢复/人工
            # 处置路径保留未知执行证据，调用方不能把它降级为 warning。
            mark_blocked(blocked_error)
            raise DatabaseError("storage_operation_unknown")
        with self.resources.factory() as session:
            return session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == UUID(str(meme_id))))

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
            operation = session.scalar(
                select(StorageOperation)
                .where(
                    StorageOperation.scope_id == self.scope.scope_id,
                    StorageOperation.operation_token == token,
                )
                .with_for_update()
            )
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
                    else:
                        # 数据库 CHECK 已禁止新值，但旧安装或人工修复可能留下
                        # 未知类型；恢复器必须停在 blocked，不能静默丢掉副作用事实。
                        self._set_status(operation, "blocked", error={"error": "storage_operation_unknown_type"}, session=session)
                        counts["blocked"] += 1
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
        record = session.scalar(select(Meme).where(Meme.scope_id == self.scope.scope_id, Meme.id == operation.meme_id).with_for_update())
        if record is None:
            raise DatabaseError("meme_not_found")
        if operation.task_id is not None:
            # 自动重命名的恢复必须仍属于创建 operation 时的叶子 claim。任务被
            # 重新认领、完成或租约过期后，不能让恢复器继续移动或 finalize 文件。
            task = session.scalar(
                select(Task)
                .where(Task.scope_id == self.scope.scope_id, Task.id == operation.task_id)
                .with_for_update()
            )
            now = utcnow()
            if (
                task is None
                or task.task_type != "image_auto_rename"
                or task.image_stage != "auto_rename"
                or task.status != "running"
                or operation.claim_generation is None
                or task.claim_generation != operation.claim_generation
                or operation.attempt is None
                or task.attempt_count != operation.attempt
                or task.lease_expires_at is None
                or task.lease_expires_at <= now
            ):
                raise DatabaseError("rename_claim_expired")
        if (
            not isinstance(operation.before_sha256, str)
            or len(operation.before_sha256) != 64
            or not isinstance(operation.after_sha256, str)
            or len(operation.after_sha256) != 64
        ):
            raise DatabaseError("rename_operation_invalid")
        same_image = record.sha256.lower() == operation.before_sha256.lower()
        title_matches = operation.expected_title_fingerprint is None or self._title_fingerprint(record) == operation.expected_title_fingerprint
        already_finalized = (
            record.storage_key == operation.target_key
            and operation.expected_revision is not None
            and record.revision == operation.expected_revision + 1
        )
        source_binding = (
            record.storage_key == operation.source_key
            and (operation.expected_revision is None or record.revision == operation.expected_revision)
        )
        if operation.status == "prepared":
            if not same_image or not title_matches:
                raise DatabaseError("rename_target_changed")
            if not source_binding and not already_finalized:
                # 文件动作尚未被本 operation 可靠确认，同图手动改名已经替换了
                # source key 时可以补偿操作；其它组合必须停在 blocked。
                if not source_ok and not target_ok:
                    self._set_status(operation, "compensated", error={"error": "storage_key_changed"}, session=session)
                    counts["compensated"] += 1
                    return
                raise DatabaseError("rename_target_changed")
        if operation.status == "prepared" and source_ok and not target_ok:
            self.blob_store.link_move(operation.source_key, operation.target_key)
            source_ok, target_ok = False, True
            counts["retried"] += 1
        if target_ok and not source_ok:
            # 只有数据库仍保留 operation 记录的 CAS 输入时才能补交 Meme；若
            # finalize 已经成功但连接在提交后断开，则识别已完成事实而不重复递增
            # revision；人工改名或 SHA 变化必须阻断，不能覆盖用户结果。
            if (
                record.storage_key == operation.target_key
                and operation.expected_revision is not None
                and record.revision == operation.expected_revision + 1
                and same_image
                and title_matches
            ):
                self._set_status(operation, "file_applied", session=session)
            elif (
                record.storage_key == operation.source_key
                and (operation.expected_revision is None or record.revision == operation.expected_revision)
                and same_image
                and title_matches
            ):
                self._set_status(operation, "file_applied", session=session)
                record.storage_key = operation.target_key
                record.revision += 1
                record.updated_at = utcnow()
            else:
                raise DatabaseError("rename_target_changed")
            self._set_status(operation, "completed", session=session)
            counts["completed"] += 1
        elif source_ok and not target_ok and operation.status == "file_applied":
            # ``file_applied`` 已经声明发生过文件副作用；源文件重新出现且目标
            # 消失无法证明是回滚还是外部修改，不能把这条事实静默标成 compensated。
            raise DatabaseError("rename_recovery_ambiguous")
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
    """生命周期共享 Engine、Session 工厂和按 scope 创建的 BlobStore。"""

    def __init__(self, engine: Engine, *, image_root: Path, data_root: Path, settings: Any | None = None, require_local_scope: bool = True):
        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False, class_=Session)
        self.image_root = image_root
        self.data_root = data_root
        self._scope_cache: dict[str, Scope] = {}
        self._lock = Lock()
        ensure_optional_control_schema(engine)
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == SCOPE_LOCAL))
            if scope is None and require_local_scope:
                raise DatabaseError("installation_required")
            if scope is not None:
                self._scope_cache[SCOPE_LOCAL] = scope
            namespace = scope.storage_namespace if scope else None
        # 适配宿主未必部署 local scope；此时保留 None，任何误用 local 都会稳定失败。
        self.blob_store = BlobStore(root=image_root, scope=ScopeContext(SCOPE_LOCAL), storage_namespace=namespace, local=True) if scope is not None or require_local_scope else None

    def environment(self, scope_id: str | ScopeContext | None = None) -> DataEnvironment:
        """创建指定 scope 的请求级环境；缺失 scope 时 fail-closed。"""
        if scope_id is None:
            raise DatabaseError("scope_required")
        context = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        return DataEnvironment(self.factory, context)

    def flat_preflight(self, scope_id: str | ScopeContext | None = None) -> dict[str, Any]:
        """执行指定 scope 的扁平图片库只读预检；缺失 scope 时 fail-closed。"""
        if scope_id is None:
            raise DatabaseError("scope_required")
        return StorageCoordinator(self, scope_id=scope_id).flat_preflight()

    def blob_store_for_scope(self, scope_id: str | ScopeContext) -> BlobStore:
        """读取 scope 的不可变 storage_namespace 并创建绑定 BlobStore。"""
        context = scope_id if isinstance(scope_id, ScopeContext) else ScopeContext(scope_id)
        scope_id = context.scope_id
        if scope_id == SCOPE_LOCAL:
            if self.blob_store is None:
                raise DatabaseError("scope_not_found")
            return self.blob_store
        with self.factory() as session:
            scope = session.scalar(select(Scope).where(Scope.id == scope_id))
            if scope is None:
                raise DatabaseError("scope_not_found")
            return BlobStore(root=self.data_root, scope=ScopeContext(scope_id), storage_namespace=scope.storage_namespace, local=False)
