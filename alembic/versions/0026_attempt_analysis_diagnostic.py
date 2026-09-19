# 保存 Agent attempt 的受控分析诊断。

from alembic import op


revision = "0026_attempt_analysis_diagnostic"
down_revision = "0025_agent_analysis_diagnostics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加受控 JSONB 字段，并限制为固定检查阶段和稳定触发类别。"""
    op.execute(
        """
        ALTER TABLE image_processing_attempts
            ADD COLUMN IF NOT EXISTS analysis_diagnostic JSONB;
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'image_processing_attempts'::regclass
                   AND conname = 'ck_attempt_analysis_diagnostic'
            ) THEN
                ALTER TABLE image_processing_attempts
                    ADD CONSTRAINT ck_attempt_analysis_diagnostic
                    CHECK (
                        analysis_diagnostic IS NULL
                        OR analysis_diagnostic IN (
                            '{"check_stage":"analysis_monitor","trigger_reason":"agent_analysis_usage_unavailable"}'::jsonb,
                            '{"check_stage":"analysis_monitor","trigger_reason":"agent_analysis_reminder_plugin_unavailable"}'::jsonb,
                            '{"check_stage":"analysis_monitor","trigger_reason":"agent_maximum_analysis_depth_exceeded"}'::jsonb
                        )
                    );
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0026_attempt_analysis_diagnostic'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除任务诊断事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
