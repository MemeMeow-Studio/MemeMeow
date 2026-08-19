"""为 Task 和图片 attempt 持久化 opaque OpenCode workspace selector。"""

from alembic import op


revision = "0014_scope_aware_opencode_workspace"
down_revision = "0013_resume_opencode_session_after_failure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """追加可空 selector，旧 local 任务继续由兼容 provider 解析。"""
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS workspace_selector VARCHAR(128);
        ALTER TABLE image_processing_attempts
            ADD COLUMN IF NOT EXISTS workspace_selector VARCHAR(128);
        ALTER TABLE installation_state
            ADD COLUMN IF NOT EXISTS schema_revision VARCHAR(128);
        UPDATE installation_state
           SET schema_revision = '0014_scope_aware_opencode_workspace'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免恢复任务丢失 workspace 绑定。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
