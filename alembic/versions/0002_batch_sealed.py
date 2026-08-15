"""为批次 finalizer 增加持久化封口标记。"""

from alembic import op

revision = "0002_batch_sealed"
down_revision = "0001_postgres_scoped"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """补齐批次封口列，使 finalizer 不会观察到尚未提交的成员。"""
    op.execute("ALTER TABLE task_batches ADD COLUMN IF NOT EXISTS sealed BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("UPDATE installation_state SET schema_revision = '0002_batch_sealed' WHERE key = 'local'")


def downgrade() -> None:
    """拒绝回滚，保持 schema 只前向升级。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
