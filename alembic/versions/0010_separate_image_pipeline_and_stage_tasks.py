"""为图片阶段任务安装显式提交来源、Job 关联和模式隔离去重事实。"""

from alembic import op


revision = "0010_separate_image_pipeline_and_stage_tasks"
down_revision = "0009_dinov2_vitb14_visual_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建图片控制面表并以追加方式扩展现有 tasks 表。

    0009 的兼容启动路径曾用 ORM ``create_all`` 补齐控制面表，因此本迁移使用
    ``IF NOT EXISTS`` 保持已运行环境可重复演练；新安装仍由同一 DDL 获得完整约束。
    """
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS image_processing_jobs (
            id UUID PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL,
            meme_id UUID NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            image_sha256 VARCHAR(64) NOT NULL,
            metadata_hash VARCHAR(64),
            processing_config_hash VARCHAR(64) NOT NULL,
            processing_config JSONB NOT NULL DEFAULT '{}'::jsonb,
            reverse_image_policy VARCHAR(16) NOT NULL DEFAULT 'forbid',
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            current_stage VARCHAR(64),
            error JSONB,
            retry_at TIMESTAMPTZ,
            lease_owner VARCHAR(255),
            lease_expires_at TIMESTAMPTZ,
            claim_generation BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_image_processing_job_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_image_processing_job_meme FOREIGN KEY(scope_id, meme_id) REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_image_processing_jobs_scope_id UNIQUE(scope_id, id),
            CONSTRAINT uq_image_processing_jobs_revision UNIQUE(scope_id, meme_id, image_sha256, revision),
            CONSTRAINT ck_image_processing_policy CHECK(reverse_image_policy IN ('forbid','auto')),
            CONSTRAINT ck_image_processing_status CHECK(status IN ('queued','running','succeeded','failed','blocked','unknown_execution')),
            CONSTRAINT ck_image_processing_generation CHECK(claim_generation >= 0)
        );
        CREATE INDEX IF NOT EXISTS ix_image_processing_jobs_active
            ON image_processing_jobs(scope_id, meme_id, image_sha256, status);

        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS submission_mode VARCHAR(16);
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS image_stage VARCHAR(32);
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS processing_job_id UUID;

        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tasks_processing_job') THEN
                ALTER TABLE tasks ADD CONSTRAINT fk_tasks_processing_job
                    FOREIGN KEY(scope_id, processing_job_id)
                    REFERENCES image_processing_jobs(scope_id, id) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_submission_mode') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_submission_mode
                    CHECK(submission_mode IS NULL OR submission_mode IN ('pipeline','standalone'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_image_stage') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage
                    CHECK(image_stage IS NULL OR image_stage IN ('visual','agent','text_embedding'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_submission_job_exclusivity') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_submission_job_exclusivity
                    CHECK(submission_mode IS NULL OR (submission_mode = 'standalone' AND processing_job_id IS NULL) OR (submission_mode = 'pipeline' AND processing_job_id IS NOT NULL));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_image_stage_type') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage_type
                    CHECK(task_type NOT IN ('visual_embedding_generation','meme_context_generation','text_embedding_generation') OR submission_mode IS NULL OR (image_stage IS NOT NULL AND ((task_type = 'visual_embedding_generation' AND image_stage = 'visual') OR (task_type = 'meme_context_generation' AND image_stage = 'agent') OR (task_type = 'text_embedding_generation' AND image_stage = 'text_embedding'))));
            END IF;
        END $$;
        CREATE INDEX IF NOT EXISTS ix_tasks_image_submission
            ON tasks(scope_id, submission_mode, image_stage, processing_job_id, created_at);

        CREATE TABLE IF NOT EXISTS image_processing_stages (
            scope_id VARCHAR(128) NOT NULL,
            job_id UUID NOT NULL,
            stage VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued',
            task_id VARCHAR(255),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error JSONB,
            retry_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, job_id, stage),
            CONSTRAINT fk_image_processing_stage_job FOREIGN KEY(scope_id, job_id) REFERENCES image_processing_jobs(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_image_processing_stage_task FOREIGN KEY(scope_id, task_id) REFERENCES tasks(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_image_processing_stage_name CHECK(stage IN ('visual','agent','text_embedding')),
            CONSTRAINT ck_image_processing_stage_status CHECK(status IN ('queued','running','succeeded','failed','blocked','unknown_execution'))
        );
        CREATE INDEX IF NOT EXISTS ix_image_processing_stages_task ON image_processing_stages(scope_id, task_id);

        CREATE TABLE IF NOT EXISTS image_processing_attempts (
            scope_id VARCHAR(128) NOT NULL,
            task_id VARCHAR(255) NOT NULL,
            attempt INTEGER NOT NULL,
            attempt_id VARCHAR(255) NOT NULL UNIQUE,
            stage VARCHAR(64) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'prepared',
            request_id VARCHAR(255),
            session_id VARCHAR(255),
            input_digest VARCHAR(64) NOT NULL,
            target_sha256 VARCHAR(64) NOT NULL,
            claim_generation BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, task_id, attempt),
            CONSTRAINT fk_image_processing_attempt_task FOREIGN KEY(scope_id, task_id) REFERENCES tasks(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_image_processing_attempt_state CHECK(state IN ('prepared','grant_committed','external_started','completed','failed','unknown_execution'))
        );
        CREATE INDEX IF NOT EXISTS ix_image_processing_attempts_task ON image_processing_attempts(scope_id, task_id, attempt);

        CREATE TABLE IF NOT EXISTS meme_text_embeddings (
            scope_id VARCHAR(128) NOT NULL,
            meme_id UUID NOT NULL,
            image_sha256 VARCHAR(64) NOT NULL,
            metadata_hash VARCHAR(64) NOT NULL,
            embedding_model_version VARCHAR(255) NOT NULL,
            dimensions INTEGER NOT NULL DEFAULT 1024,
            semantic_document VARCHAR(6000) NOT NULL,
            embedding vector(1024),
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, meme_id, image_sha256, metadata_hash, embedding_model_version),
            CONSTRAINT fk_meme_text_embedding_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_meme_text_embedding_meme FOREIGN KEY(scope_id, meme_id) REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_meme_text_embedding_dimensions CHECK(dimensions = 1024),
            CONSTRAINT ck_meme_text_embedding_status CHECK(status IN ('pending','ready','failed'))
        );
        CREATE INDEX IF NOT EXISTS ix_meme_text_embeddings_current
            ON meme_text_embeddings(scope_id, meme_id, image_sha256, metadata_hash, embedding_model_version, status);

        CREATE TABLE IF NOT EXISTS search_migration_states (
            scope_id VARCHAR(128) PRIMARY KEY,
            model VARCHAR(255),
            mode VARCHAR(32) NOT NULL DEFAULT 'legacy_only',
            epoch BIGINT NOT NULL DEFAULT 0,
            legacy_generation_id UUID,
            completed_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_search_migration_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT ck_search_migration_mode CHECK(mode IN ('legacy_only','backfill','incremental_only'))
        );

        CREATE TABLE IF NOT EXISTS operation_grants (
            scope_id VARCHAR(128) NOT NULL,
            operation VARCHAR(128) NOT NULL,
            idempotency_key VARCHAR(255) NOT NULL,
            grant_id VARCHAR(255) NOT NULL UNIQUE,
            task_id VARCHAR(255),
            resource_id VARCHAR(255),
            state VARCHAR(32) NOT NULL DEFAULT 'acquired',
            attempt_id VARCHAR(255),
            input_digest VARCHAR(64),
            retry_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, operation, idempotency_key),
            CONSTRAINT fk_operation_grant_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_operation_grant_task FOREIGN KEY(scope_id, task_id) REFERENCES tasks(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_operation_grant_operation CHECK(operation IN ('image.upload','analysis.agent','analysis.reverse_image_search','image.delete')),
            CONSTRAINT ck_operation_grant_state CHECK(state IN ('acquired','committed','released','unknown'))
        );
        CREATE INDEX IF NOT EXISTS ix_operation_grants_scope_task ON operation_grants(scope_id, task_id);

        CREATE TABLE IF NOT EXISTS agent_callback_requests (
            scope_id VARCHAR(128) NOT NULL,
            request_id VARCHAR(128) NOT NULL,
            task_id VARCHAR(255) NOT NULL,
            claim_generation BIGINT NOT NULL,
            attempt INTEGER NOT NULL,
            operation VARCHAR(128) NOT NULL,
            target_sha256 VARCHAR(64) NOT NULL,
            input_digest VARCHAR(64) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'started',
            result JSONB,
            error JSONB,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            PRIMARY KEY(scope_id, request_id),
            CONSTRAINT fk_agent_callback_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_agent_callback_task FOREIGN KEY(scope_id, task_id) REFERENCES tasks(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_agent_callback_generation CHECK(claim_generation > 0),
            CONSTRAINT ck_agent_callback_attempt CHECK(attempt > 0),
            CONSTRAINT ck_agent_callback_target_sha CHECK(length(target_sha256) = 64),
            CONSTRAINT ck_agent_callback_input_digest CHECK(length(input_digest) = 64),
            CONSTRAINT ck_agent_callback_state CHECK(state IN ('started','completed','failed','unknown_execution'))
        );
        CREATE INDEX IF NOT EXISTS ix_agent_callback_requests_scope_task ON agent_callback_requests(scope_id, task_id, created_at);
        UPDATE installation_state SET schema_revision = '0010_separate_image_pipeline_and_stage_tasks' WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，保留任务来源、授权和执行审计事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
