"""数据库连接、启动检查和本地安装初始化。

该模块位于持久化模型与应用启动装配之间，提供进程级 SQLAlchemy Engine 的创建，
以及 PostgreSQL、pgvector、Alembic revision 和 local 安装标记的启动门禁。它不
依赖兼容 facade、Repository 或文件存储实现，避免持久化基础设施形成反向导入。
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.persistence.models import (
    Base,
    InstallationState,
    OPTIONAL_CONTROL_TABLES,
    Scope,
)


SCOPE_LOCAL = "local"
# 当前代码要求的 Alembic head；数据库初始化脚本会显式传入同一 revision。
CURRENT_SCHEMA_REVISION = "0018_operation_grant_metering_units"


class DatabaseError(RuntimeError):
    """数据库边界错误，携带不会泄露连接凭据的稳定错误码。"""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


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
            connection.execute(text("ALTER TABLE storage_operations ADD COLUMN IF NOT EXISTS thumbnail_keys JSONB NOT NULL DEFAULT '[]'::jsonb"))
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
            connection.execute(text("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS metering_units INTEGER"))
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
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_metering_units') THEN
                        ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_metering_units
                            CHECK(metering_units IS NULL OR metering_units >= 0);
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
