"""为 Meme 物化七字段语义 hash，供增量检索和列表投影使用。"""

from alembic import op


revision = "0021_search_metadata_hash"
down_revision = "0020_visual_match_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空 hash 列和当前检索过滤索引。历史行由回填任务按语义字段补齐。"""
    op.execute(
        """
        ALTER TABLE memes
            ADD COLUMN IF NOT EXISTS search_metadata_hash VARCHAR(64);
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_memes_search_metadata_hash') THEN
                ALTER TABLE memes ADD CONSTRAINT ck_memes_search_metadata_hash
                    CHECK (search_metadata_hash IS NULL OR length(search_metadata_hash) = 64);
            END IF;
        END $$;
        CREATE INDEX IF NOT EXISTS ix_memes_scope_search_hash
            ON memes (scope_id, search_metadata_hash, sha256, id);
        UPDATE installation_state
           SET schema_revision = '0021_search_metadata_hash'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """项目 schema 只允许前向升级，避免删除检索一致性字段。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
