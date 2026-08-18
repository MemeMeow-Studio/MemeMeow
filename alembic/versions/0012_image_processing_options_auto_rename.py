"""为图片处理 Job 增加自动命名选项和第四阶段约束。"""

from alembic import op


revision = "0012_image_processing_options_auto_rename"
down_revision = "0011_harden_operation_grant_association"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """显式升级旧三阶段 CHECK，并追加自动重命名 CAS 所需的操作事实。"""
    op.execute(
        """
        ALTER TABLE image_processing_jobs
            ADD COLUMN IF NOT EXISTS auto_name BOOLEAN NOT NULL DEFAULT FALSE;
        UPDATE image_processing_jobs
           SET auto_name = FALSE
         WHERE auto_name IS NULL;
        ALTER TABLE image_processing_jobs
            ALTER COLUMN auto_name SET DEFAULT FALSE,
            ALTER COLUMN auto_name SET NOT NULL;

        ALTER TABLE storage_operations
            ADD COLUMN IF NOT EXISTS expected_revision BIGINT,
            ADD COLUMN IF NOT EXISTS claim_generation BIGINT,
            ADD COLUMN IF NOT EXISTS attempt INTEGER,
            ADD COLUMN IF NOT EXISTS task_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS expected_title_fingerprint VARCHAR(64);

        ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_image_stage;
        ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_task_image_stage_type;
        ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage
            CHECK(image_stage IS NULL OR image_stage IN ('visual','agent','auto_rename','text_embedding'));
        ALTER TABLE tasks ADD CONSTRAINT ck_task_image_stage_type
            CHECK(
                task_type NOT IN ('visual_embedding_generation','meme_context_generation','image_auto_rename','text_embedding_generation')
                OR submission_mode IS NULL
                OR (
                    image_stage IS NOT NULL
                    AND (
                        (task_type = 'visual_embedding_generation' AND image_stage = 'visual')
                        OR (task_type = 'meme_context_generation' AND image_stage = 'agent')
                        OR (task_type = 'image_auto_rename' AND image_stage = 'auto_rename')
                        OR (task_type = 'text_embedding_generation' AND image_stage = 'text_embedding')
                    )
                )
            );

        ALTER TABLE image_processing_stages DROP CONSTRAINT IF EXISTS ck_image_processing_stage_name;
        ALTER TABLE image_processing_stages DROP CONSTRAINT IF EXISTS ck_image_processing_stage_status;
        ALTER TABLE image_processing_stages ADD CONSTRAINT ck_image_processing_stage_name
            CHECK(stage IN ('visual','agent','auto_rename','text_embedding'));
        ALTER TABLE image_processing_stages ADD CONSTRAINT ck_image_processing_stage_status
            CHECK(status IN ('queued','running','succeeded','failed','blocked','unknown_execution','skipped','warning'));

        ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_expected_revision;
        ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_claim_generation;
        ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_attempt;
        ALTER TABLE storage_operations DROP CONSTRAINT IF EXISTS ck_storage_operation_title_fingerprint;
        ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_expected_revision
            CHECK(expected_revision IS NULL OR expected_revision >= 1);
        ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_claim_generation
            CHECK(claim_generation IS NULL OR claim_generation > 0);
        ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_attempt
            CHECK(attempt IS NULL OR attempt > 0);
        ALTER TABLE storage_operations ADD CONSTRAINT ck_storage_operation_title_fingerprint
            CHECK(expected_title_fingerprint IS NULL OR length(expected_title_fingerprint) = 64);
        CREATE INDEX IF NOT EXISTS ix_storage_operations_task
            ON storage_operations(scope_id, task_id, updated_at);

        UPDATE installation_state
           SET schema_revision = '0012_image_processing_options_auto_rename'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免重新启用三阶段约束。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
