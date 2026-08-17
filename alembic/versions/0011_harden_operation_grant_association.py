"""为 operation grant 关联增加服务端请求事实和可校验指纹。"""

from alembic import op


revision = "0011_harden_operation_grant_association"
down_revision = "0010_separate_image_pipeline_and_stage_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """追加 grant 请求事实列；历史行缺少事实时由 repository fail-closed。"""
    op.execute("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS source VARCHAR(64)")
    op.execute("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS units INTEGER")
    op.execute("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS request_fingerprint VARCHAR(64)")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_source') THEN
                ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_source
                    CHECK(source IS NULL OR (length(source) > 0 AND length(source) <= 64));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_units') THEN
                ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_units
                    CHECK(units IS NULL OR units > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_fingerprint') THEN
                ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_fingerprint
                    CHECK(request_fingerprint IS NULL OR length(request_fingerprint) = 64);
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0011_harden_operation_grant_association'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免删除已经用于安全判定的 grant 请求事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
