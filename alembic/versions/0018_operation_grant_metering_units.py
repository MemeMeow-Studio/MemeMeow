"""为公共 operation grant 增加独立的非负计量单位事实。"""

from alembic import op


revision = "0018_operation_grant_metering_units"
down_revision = "0017_derived_image_thumbnails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空历史兼容列；新运行时会明确写入零或正整数。"""
    op.execute("ALTER TABLE operation_grants ADD COLUMN IF NOT EXISTS metering_units INTEGER")
    op.execute(
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_operation_grant_metering_units') THEN
                ALTER TABLE operation_grants ADD CONSTRAINT ck_operation_grant_metering_units
                    CHECK(metering_units IS NULL OR metering_units >= 0);
            END IF;
        END $$;
        UPDATE installation_state
           SET schema_revision = '0018_operation_grant_metering_units'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除已参与幂等判定的事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
