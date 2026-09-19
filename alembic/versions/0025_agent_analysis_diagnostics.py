"""持久化 Agent 分析用量摘要和进程终止诊断。"""

from alembic import op


revision = "0025_agent_analysis_diagnostics"
down_revision = "0024_image_processing_retry_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加历史兼容字段，并建立金额和终止原因约束。"""
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS analysis_policy JSONB,
            ADD COLUMN IF NOT EXISTS observed_cost NUMERIC(20, 8),
            ADD COLUMN IF NOT EXISTS usage_checked_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS termination_reason VARCHAR(64),
            ADD COLUMN IF NOT EXISTS termination_signal VARCHAR(16);
        ALTER TABLE image_processing_attempts
            ADD COLUMN IF NOT EXISTS analysis_policy JSONB,
            ADD COLUMN IF NOT EXISTS observed_cost NUMERIC(20, 8),
            ADD COLUMN IF NOT EXISTS usage_checked_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN NOT NULL DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS termination_reason VARCHAR(64),
            ADD COLUMN IF NOT EXISTS termination_signal VARCHAR(16),
            ADD COLUMN IF NOT EXISTS process_reaped BOOLEAN;
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_observed_cost') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_observed_cost
                    CHECK (observed_cost IS NULL OR observed_cost >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_termination_reason') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_termination_reason
                    CHECK (termination_reason IS NULL OR termination_reason IN ('analysis_cost_limit','unknown_execution','timeout','cancelled','process_failed'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_termination_signal') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_termination_signal
                    CHECK (termination_signal IS NULL OR termination_signal IN ('SIGTERM','SIGKILL'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_observed_cost') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_observed_cost
                    CHECK (observed_cost IS NULL OR observed_cost >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_termination_reason') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_termination_reason
                    CHECK (termination_reason IS NULL OR termination_reason IN ('analysis_cost_limit','unknown_execution','timeout','cancelled','process_failed'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_termination_signal') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_termination_signal
                    CHECK (termination_signal IS NULL OR termination_signal IN ('SIGTERM','SIGKILL'));
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0025_agent_analysis_diagnostics'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除任务诊断事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
