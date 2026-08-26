"""为原图安装 scope-safe、可重建的缩略图派生事实。"""

from alembic import op


revision = "0017_derived_image_thumbnails"
down_revision = "0016_agent_fair_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建缩略图事实表和当前版本索引。"""
    op.execute(
        """
        ALTER TABLE storage_operations
            ADD COLUMN IF NOT EXISTS thumbnail_keys JSONB NOT NULL DEFAULT '[]'::jsonb;
        CREATE TABLE IF NOT EXISTS derived_image_thumbnails (
            scope_id VARCHAR(128) NOT NULL,
            meme_id UUID NOT NULL,
            source_sha256 VARCHAR(64) NOT NULL,
            source_size_bytes BIGINT NOT NULL,
            profile VARCHAR(128) NOT NULL,
            output_key VARCHAR(1024),
            output_sha256 VARCHAR(64),
            output_size_bytes BIGINT,
            width INTEGER,
            height INTEGER,
            media_type VARCHAR(128),
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            diagnostic JSONB,
            generated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (scope_id, meme_id, source_sha256, source_size_bytes, profile),
            CONSTRAINT fk_thumbnail_meme
                FOREIGN KEY (scope_id, meme_id)
                REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_thumbnail_source_sha256
                CHECK (length(source_sha256) = 64),
            CONSTRAINT ck_thumbnail_source_size_nonnegative
                CHECK (source_size_bytes >= 0),
            CONSTRAINT ck_thumbnail_status
                CHECK (status IN ('available','pending','failed','stale')),
            CONSTRAINT ck_thumbnail_output_sha256
                CHECK (output_sha256 IS NULL OR length(output_sha256) = 64),
            CONSTRAINT ck_thumbnail_output_size_nonnegative
                CHECK (output_size_bytes IS NULL OR output_size_bytes >= 0),
            CONSTRAINT ck_thumbnail_width
                CHECK (width IS NULL OR width > 0),
            CONSTRAINT ck_thumbnail_height
                CHECK (height IS NULL OR height > 0)
        );
        CREATE INDEX IF NOT EXISTS ix_thumbnail_current
            ON derived_image_thumbnails(scope_id, meme_id, profile, status, updated_at);
        UPDATE installation_state
           SET schema_revision = '0017_derived_image_thumbnails'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免回滚时丢失派生事实。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
