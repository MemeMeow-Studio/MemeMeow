"""安装 scope 隔离的 DINOv3 视觉向量表。"""

from alembic import op


revision = "0006_scope_local_visual_search"
down_revision = "0005_reverse_image_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建独立 1280 维视觉向量空间及精确查询索引。"""
    op.execute(
        """
        CREATE TABLE meme_visual_embeddings (
            scope_id VARCHAR(128) NOT NULL,
            meme_id UUID NOT NULL,
            model VARCHAR(255) NOT NULL,
            preprocess_version VARCHAR(128) NOT NULL,
            dimensions INTEGER NOT NULL,
            image_sha256 VARCHAR(64) NOT NULL,
            embedding vector(1280) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(scope_id, meme_id, model, preprocess_version),
            CONSTRAINT fk_visual_embedding_scope FOREIGN KEY(scope_id)
                REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_visual_embedding_meme FOREIGN KEY(scope_id, meme_id)
                REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_visual_embedding_dimensions CHECK(dimensions = 1280),
            CONSTRAINT ck_visual_embedding_sha256 CHECK(length(image_sha256) = 64),
            CONSTRAINT uq_visual_embedding_identity UNIQUE(scope_id, meme_id, model, preprocess_version)
        );
        CREATE INDEX ix_visual_embeddings_match
            ON meme_visual_embeddings(scope_id, model, preprocess_version, meme_id);
        UPDATE installation_state
           SET schema_revision = '0006_scope_local_visual_search'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免丢失已生成视觉产物。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
