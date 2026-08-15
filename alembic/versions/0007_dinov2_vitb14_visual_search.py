"""把视觉向量空间切换到官方 DINOv2 ViT-B/14 的 768 维。"""

from alembic import op


revision = "0007_dinov2_vitb14_visual_search"
down_revision = "0006_scope_local_visual_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留旧 H+/16 表并创建独立 DINOv2 表，禁止不同维度向量混存。"""
    op.execute(
        """
        ALTER TABLE meme_visual_embeddings
            RENAME TO meme_visual_embeddings_dinov3_vith16plus;
        ALTER INDEX uq_visual_embedding_identity
            RENAME TO uq_visual_embedding_dinov3_vith16plus;
        ALTER INDEX ix_visual_embeddings_match
            RENAME TO ix_visual_embeddings_dinov3_vith16plus_match;
        CREATE TABLE meme_visual_embeddings (
            scope_id VARCHAR(128) NOT NULL,
            meme_id UUID NOT NULL,
            model VARCHAR(255) NOT NULL,
            preprocess_version VARCHAR(128) NOT NULL,
            dimensions INTEGER NOT NULL,
            image_sha256 VARCHAR(64) NOT NULL,
            embedding vector(768) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_visual_embedding_identity PRIMARY KEY(scope_id, meme_id, model, preprocess_version),
            CONSTRAINT fk_visual_embedding_scope FOREIGN KEY(scope_id)
                REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_visual_embedding_meme FOREIGN KEY(scope_id, meme_id)
                REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_visual_embedding_dimensions CHECK(dimensions = 768),
            CONSTRAINT ck_visual_embedding_sha256 CHECK(length(image_sha256) = 64)
        );
        CREATE INDEX ix_visual_embeddings_match
            ON meme_visual_embeddings(scope_id, model, preprocess_version, meme_id);
        UPDATE installation_state
           SET schema_revision = '0007_dinov2_vitb14_visual_search'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免把当前 DINOv2 空间隐式覆盖回旧模型空间。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
