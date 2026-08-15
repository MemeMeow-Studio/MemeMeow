"""将活动视觉向量空间切换为官方 DINOv2 ViT-B/14。

0008 恢复的 DINOv3 1280 维表改为历史表，0007 已创建的 DINOv2 768 维表改回
活动表。迁移只调整表名和索引名，不删除任一模型的向量或图片。
"""

from alembic import op


revision = "0009_dinov2_vitb14_visual_search"
down_revision = "0008_dinov3_vith16plus"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """交换两套视觉表的活动名称，确保 ORM 只访问 DINOv2 空间。"""
    op.execute(
        """
        ALTER TABLE meme_visual_embeddings
            RENAME TO meme_visual_embeddings_dinov3_vith16plus;
        ALTER INDEX uq_visual_embedding_identity
            RENAME TO uq_visual_embedding_dinov3_vith16plus;
        ALTER INDEX ix_visual_embeddings_match
            RENAME TO ix_visual_embeddings_dinov3_vith16plus_match;
        ALTER TABLE meme_visual_embeddings_dinov2_vitb14
            RENAME TO meme_visual_embeddings;
        ALTER INDEX uq_visual_embedding_dinov2_vitb14
            RENAME TO uq_visual_embedding_identity;
        ALTER INDEX ix_visual_embeddings_dinov2_vitb14_match
            RENAME TO ix_visual_embeddings_match;
        UPDATE installation_state
           SET schema_revision = '0009_dinov2_vitb14_visual_search'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """显式把活动空间切回保留的 DINOv3 表，不删除任一模型数据。

    该回切只允许运维在同步更新模型配置后执行；交换表名让 ORM 的固定活动表
    继续满足维度约束，避免仅修改环境变量造成 768/1280 维向量混用。
    """
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
