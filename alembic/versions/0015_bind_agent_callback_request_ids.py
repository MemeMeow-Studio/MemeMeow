"""为 Agent callback 逻辑请求安装前向唯一约束并阻断历史重复事实。"""

from alembic import op


revision = "0015_bind_agent_callback_request_ids"
down_revision = "0014_scope_aware_opencode_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """先检查历史绑定完整性和重复逻辑键，再安装唯一索引。"""
    op.execute(
        """
        DO $$
        DECLARE
            duplicate_count BIGINT;
            incomplete_count BIGINT;
        BEGIN
            SELECT count(*)
              INTO incomplete_count
              FROM agent_callback_requests
             WHERE scope_id IS NULL
                OR task_id IS NULL
                OR claim_generation IS NULL
                OR attempt IS NULL
                OR operation IS NULL
                OR target_sha256 IS NULL
                OR input_digest IS NULL;
            IF incomplete_count > 0 THEN
                RAISE EXCEPTION 'callback 绑定字段不完整，迁移已停止';
            END IF;

            SELECT count(*)
              INTO duplicate_count
              FROM (
                    SELECT scope_id, task_id, claim_generation, attempt,
                           operation, target_sha256, input_digest
                      FROM agent_callback_requests
                     GROUP BY scope_id, task_id, claim_generation, attempt,
                              operation, target_sha256, input_digest
                    HAVING count(*) > 1
                   ) AS duplicate_groups;
            IF duplicate_count > 0 THEN
                RAISE EXCEPTION 'callback 存在 % 组历史重复逻辑绑定，迁移已停止', duplicate_count;
            END IF;
        END $$;

        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_callback_requests_logical
            ON agent_callback_requests(
                scope_id,
                task_id,
                claim_generation,
                attempt,
                operation,
                target_sha256,
                input_digest
            );
        UPDATE installation_state
           SET schema_revision = '0015_bind_agent_callback_request_ids'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除 callback 幂等边界。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
