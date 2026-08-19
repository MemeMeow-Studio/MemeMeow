"""为 OpenCode session 续跑补充 attempt、错误历史和 rollout 字段。"""

from alembic import op


revision = "0013_resume_opencode_session_after_failure"
down_revision = "0012_image_processing_options_auto_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """以可选默认值扩展旧任务，历史行默认不可自动续跑。"""
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS resume_available BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS resume_reason VARCHAR(64),
            ADD COLUMN IF NOT EXISTS resume_session_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS executor_attempt_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS resume_attempt_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN IF NOT EXISTS resume_started_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS first_error JSONB,
            ADD COLUMN IF NOT EXISTS error_history JSONB NOT NULL DEFAULT '[]'::jsonb;
        UPDATE tasks
           SET resume_available = FALSE,
               resume_attempt_count = COALESCE(resume_attempt_count, 0),
               error_history = COALESCE(error_history, '[]'::jsonb)
         WHERE resume_available IS NULL OR resume_attempt_count IS NULL OR error_history IS NULL;
        ALTER TABLE tasks
            ALTER COLUMN resume_available SET DEFAULT FALSE,
            ALTER COLUMN resume_available SET NOT NULL,
            ALTER COLUMN resume_attempt_count SET DEFAULT 0,
            ALTER COLUMN resume_attempt_count SET NOT NULL,
            ALTER COLUMN error_history SET DEFAULT '[]'::jsonb,
            ALTER COLUMN error_history SET NOT NULL;
        ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_resume_attempt_count;
        ALTER TABLE tasks ADD CONSTRAINT ck_tasks_resume_attempt_count CHECK(resume_attempt_count >= 0);

        ALTER TABLE image_processing_attempts
            ADD COLUMN IF NOT EXISTS executor_attempt_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS resume_of_attempt_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS processing_config_hash VARCHAR(64),
            ADD COLUMN IF NOT EXISTS resume_available BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS resume_reason VARCHAR(64),
            ADD COLUMN IF NOT EXISTS error JSONB;
        UPDATE image_processing_attempts
           SET resume_available = FALSE
         WHERE resume_available IS NULL;
        ALTER TABLE image_processing_attempts
            ALTER COLUMN resume_available SET DEFAULT FALSE,
            ALTER COLUMN resume_available SET NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS uq_image_processing_attempt_executor_id
            ON image_processing_attempts(executor_attempt_id)
            WHERE executor_attempt_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS ix_image_processing_attempts_resume
            ON image_processing_attempts(scope_id, task_id, resume_available, updated_at);

        UPDATE installation_state
           SET schema_revision = '0013_resume_opencode_session_after_failure'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免回滚后产生可误用的 session 字段。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
