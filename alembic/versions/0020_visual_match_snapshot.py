"""为 Agent 任务增加版本化视觉候选 snapshot 及 attempt 摘要。"""

from alembic import op


revision = "0020_visual_match_snapshot"
down_revision = "0019_task_lane_resource_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空历史兼容字段，并为新任务提供 snapshot 完整性约束。"""
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN IF NOT EXISTS visual_match_snapshot JSONB,
            ADD COLUMN IF NOT EXISTS visual_snapshot_sha256 VARCHAR(64),
            ADD COLUMN IF NOT EXISTS visual_snapshot_protocol_version INTEGER,
            ADD COLUMN IF NOT EXISTS visual_snapshot_matched_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS visual_snapshot_candidate_count INTEGER;
        ALTER TABLE image_processing_attempts
            ADD COLUMN IF NOT EXISTS visual_snapshot_sha256 VARCHAR(64),
            ADD COLUMN IF NOT EXISTS visual_snapshot_protocol_version INTEGER,
            ADD COLUMN IF NOT EXISTS visual_snapshot_matched_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS visual_snapshot_candidate_count INTEGER;
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_visual_snapshot_sha256') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_visual_snapshot_sha256
                    CHECK (visual_snapshot_sha256 IS NULL OR length(visual_snapshot_sha256) = 64);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_visual_snapshot_protocol_version') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_visual_snapshot_protocol_version
                    CHECK (visual_snapshot_protocol_version IS NULL OR visual_snapshot_protocol_version > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_visual_snapshot_candidate_count') THEN
                ALTER TABLE tasks ADD CONSTRAINT ck_task_visual_snapshot_candidate_count
                    CHECK (visual_snapshot_candidate_count IS NULL OR visual_snapshot_candidate_count >= 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_visual_snapshot_sha256') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_visual_snapshot_sha256
                    CHECK (visual_snapshot_sha256 IS NULL OR length(visual_snapshot_sha256) = 64);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_visual_snapshot_protocol_version') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_visual_snapshot_protocol_version
                    CHECK (visual_snapshot_protocol_version IS NULL OR visual_snapshot_protocol_version > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_attempt_visual_snapshot_candidate_count') THEN
                ALTER TABLE image_processing_attempts ADD CONSTRAINT ck_attempt_visual_snapshot_candidate_count
                    CHECK (visual_snapshot_candidate_count IS NULL OR visual_snapshot_candidate_count >= 0);
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0020_visual_match_snapshot'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除恢复所需的 snapshot 事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
