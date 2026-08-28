"""为 Agent lane 增加通用资源槽位和资源维度公平事实。"""

from alembic import op


revision = "0019_task_lane_resource_scheduling"
down_revision = "0018_operation_grant_metering_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """回填旧任务并安装全局加资源双重 claim 所需的表和索引。"""
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS lane_resource_key VARCHAR(128)
                NOT NULL DEFAULT '__global__';
        UPDATE tasks
           SET lane_resource_key = '__global__'
         WHERE lane_resource_key IS NULL OR length(trim(lane_resource_key)) = 0;
        ALTER TABLE tasks
            ALTER COLUMN lane_resource_key SET DEFAULT '__global__',
            ALTER COLUMN lane_resource_key SET NOT NULL;
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_lane_resource_key') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_lane_resource_key
                    CHECK (length(lane_resource_key) > 0);
            END IF;
        END $$;

        ALTER TABLE task_lane_fairness
            ADD COLUMN IF NOT EXISTS resource_key VARCHAR(128)
                NOT NULL DEFAULT '__global__';
        UPDATE task_lane_fairness
           SET resource_key = '__global__'
         WHERE resource_key IS NULL OR length(trim(resource_key)) = 0;
        ALTER TABLE task_lane_fairness
            ALTER COLUMN resource_key SET DEFAULT '__global__',
            ALTER COLUMN resource_key SET NOT NULL;
        ALTER TABLE task_lane_fairness
            DROP CONSTRAINT IF EXISTS task_lane_fairness_pkey;
        ALTER TABLE task_lane_fairness
            ADD CONSTRAINT task_lane_fairness_pkey PRIMARY KEY (lane, resource_key, scope_id);
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_lane_fairness_resource_key') THEN
                ALTER TABLE task_lane_fairness ADD CONSTRAINT ck_task_lane_fairness_resource_key
                    CHECK (length(resource_key) > 0);
            END IF;
        END $$;
        DROP INDEX IF EXISTS ix_task_lane_fairness_dispatch;
        CREATE INDEX IF NOT EXISTS ix_task_lane_fairness_dispatch
            ON task_lane_fairness(lane, resource_key, last_dispatch_sequence, scope_id);

        CREATE TABLE IF NOT EXISTS task_lane_resource_slots (
            lane VARCHAR(64) NOT NULL,
            resource_key VARCHAR(128) NOT NULL,
            slot_number INTEGER NOT NULL,
            task_scope_id VARCHAR(128),
            task_id VARCHAR(255),
            lease_owner VARCHAR(255),
            claim_generation BIGINT,
            lease_expires_at TIMESTAMPTZ,
            PRIMARY KEY (lane, resource_key, slot_number),
            CONSTRAINT fk_lane_resource_slot_scope
                FOREIGN KEY (task_scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_lane_resource_slot_task
                FOREIGN KEY (task_scope_id, task_id) REFERENCES tasks(scope_id, id),
            CONSTRAINT uq_task_lane_resource_slot_task
                UNIQUE (task_scope_id, task_id),
            CONSTRAINT ck_resource_slot_number CHECK (slot_number >= 0),
            CONSTRAINT ck_task_lane_resource_key_value CHECK (length(resource_key) > 0)
        );
        CREATE INDEX IF NOT EXISTS ix_task_lane_resource_slot_dispatch
            ON task_lane_resource_slots(lane, resource_key, slot_number);
        CREATE INDEX IF NOT EXISTS ix_tasks_lane_resource_claimable
            ON tasks(lane, lane_resource_key, status, available_at, created_at);

        UPDATE installation_state
           SET schema_revision = '0019_task_lane_resource_scheduling'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除仍被租约恢复读取的资源事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
