"""恢复首发 DINOv3 ViT-H+/16 视觉向量空间。

0007 曾将 0006 创建的 1280 维表保留为历史表，并把 768 维 DINOv2 表放在
``meme_visual_embeddings``。本迁移只调整表名和索引名，不删除任一模型的向量，
使应用重新使用原有的 1280 维 DINOv3 表。
"""

from alembic import op


revision = "0008_dinov3_vith16plus"
down_revision = "0007_dinov2_vitb14_visual_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """把 DINOv2 表归档并恢复 DINOv3 表为应用的活动表。"""
    op.execute(
        """
        ALTER TABLE meme_visual_embeddings
            RENAME TO meme_visual_embeddings_dinov2_vitb14;
        ALTER INDEX uq_visual_embedding_identity
            RENAME TO uq_visual_embedding_dinov2_vitb14;
        ALTER INDEX ix_visual_embeddings_match
            RENAME TO ix_visual_embeddings_dinov2_vitb14_match;
        ALTER TABLE meme_visual_embeddings_dinov3_vith16plus
            RENAME TO meme_visual_embeddings;
        ALTER INDEX uq_visual_embedding_dinov3_vith16plus
            RENAME TO uq_visual_embedding_identity;
        ALTER INDEX ix_visual_embeddings_dinov3_vith16plus_match
            RENAME TO ix_visual_embeddings_match;
        UPDATE installation_state
           SET schema_revision = '0008_dinov3_vith16plus'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免把活动视觉空间隐式切回已废弃的 DINOv2。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
