"""安装 scope-bound Meme 合集及成员关系表。"""

from alembic import op

revision = "0004_meme_collections"
down_revision = "0003_flat_meme_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建合集、复合外键、级联约束和分页索引。"""
    op.execute(
        """
        CREATE TABLE meme_collections (
            id UUID PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_meme_collections_scope_id UNIQUE(scope_id, id),
            CONSTRAINT uq_meme_collections_scope_name UNIQUE(scope_id, name)
        );
        CREATE TABLE meme_collection_items (
            scope_id VARCHAR(128) NOT NULL,
            collection_id UUID NOT NULL,
            meme_id UUID NOT NULL,
            added_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, collection_id, meme_id),
            CONSTRAINT fk_collection_item_collection FOREIGN KEY(scope_id, collection_id) REFERENCES meme_collections(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_collection_item_meme FOREIGN KEY(scope_id, meme_id) REFERENCES memes(scope_id, id) ON DELETE CASCADE
        );
        CREATE INDEX ix_meme_collection_items_page ON meme_collection_items(scope_id, collection_id, added_at, meme_id);
        UPDATE installation_state SET schema_revision = '0004_meme_collections' WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，保持 schema 只前向升级。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
