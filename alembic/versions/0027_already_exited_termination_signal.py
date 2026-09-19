"""允许记录分析终止时发现进程已经退出的诊断信号。"""

from alembic import op


revision = "0027_already_exited_termination_signal"
down_revision = "0026_attempt_analysis_diagnostic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展任务和 attempt 的终止信号约束，保留历史字段及安装版本。"""
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'tasks'::regclass
                   AND conname = 'ck_task_termination_signal'
            ) THEN
                ALTER TABLE tasks DROP CONSTRAINT ck_task_termination_signal;
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'image_processing_attempts'::regclass
                   AND conname = 'ck_attempt_termination_signal'
            ) THEN
                ALTER TABLE image_processing_attempts DROP CONSTRAINT ck_attempt_termination_signal;
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'tasks'::regclass
                   AND conname = 'ck_task_termination_signal'
            ) THEN
                ALTER TABLE tasks
                    ADD CONSTRAINT ck_task_termination_signal
                    CHECK (
                        termination_signal IS NULL
                        OR termination_signal IN ('SIGTERM','SIGKILL','already_exited')
                    );
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM pg_constraint
                 WHERE conrelid = 'image_processing_attempts'::regclass
                   AND conname = 'ck_attempt_termination_signal'
            ) THEN
                ALTER TABLE image_processing_attempts
                    ADD CONSTRAINT ck_attempt_termination_signal
                    CHECK (
                        termination_signal IS NULL
                        OR termination_signal IN ('SIGTERM','SIGKILL','already_exited')
                    );
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0027_already_exited_termination_signal'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除终止诊断事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
