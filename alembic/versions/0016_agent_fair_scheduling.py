"""为 Agent lane 安装跨 scope 的持久公平调度状态。"""

from alembic import op


revision = "0016_agent_fair_scheduling"
down_revision = "0015_bind_agent_callback_request_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建公平状态表和候选排序索引，不回写历史任务的调度序号。"""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS task_lane_fairness (
            lane VARCHAR(64) NOT NULL,
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            last_dispatch_sequence BIGINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (lane, scope_id),
            CONSTRAINT ck_task_lane_fairness_sequence CHECK (last_dispatch_sequence >= 0)
        );
        CREATE INDEX IF NOT EXISTS ix_task_lane_fairness_dispatch
            ON task_lane_fairness(lane, last_dispatch_sequence, scope_id);
        UPDATE installation_state
           SET schema_revision = '0016_agent_fair_scheduling'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免回滚时伪造公平状态。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
