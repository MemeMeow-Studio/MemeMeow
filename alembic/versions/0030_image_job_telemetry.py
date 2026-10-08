# 图片处理持久追踪快照，复用 job 事务保存后台执行的调查关联。
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0030_image_job_telemetry"
down_revision = "0029_name_text_input"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加独立调查快照字段，历史 job 保留未知上下文。"""
    op.add_column("image_processing_jobs", sa.Column("telemetry_context", JSONB(), nullable=True))
    op.execute("UPDATE installation_state SET schema_revision = '0030_image_job_telemetry' WHERE key = 'local'")


def downgrade() -> None:
    """保持已保存的关联事实，仅允许前向升级。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
