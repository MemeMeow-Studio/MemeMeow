# 名称来源与共享文本输入迁移，保留图片、任务及历史向量。

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from backend.embedding_input import build_text_input


revision = "0029_name_text_input"
down_revision = "0028_image_library_sort"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加名称保护和文本资格字段，按当前输入分批更新搜索指纹。"""
    op.add_column("memes", sa.Column("name_source", sa.String(16), nullable=False, server_default="unknown"))
    op.add_column("memes", sa.Column("text_input_status", sa.String(32), nullable=False, server_default="waiting_for_text"))
    op.add_column("task_batches", sa.Column("summary", JSONB(), nullable=True))
    op.create_check_constraint("ck_memes_name_source", "memes", "name_source IN ('uploaded','manual','automatic','unknown')")
    op.create_check_constraint("ck_memes_text_input_status", "memes", "text_input_status IN ('eligible','waiting_for_text','repair_required')")
    op.create_table(
        "image_naming_pools",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("cursor_scope_id", sa.String(128), nullable=True),
        sa.Column("next_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("capacity > 0", name="ck_image_naming_capacity"),
    )
    connection = op.get_bind()
    last_id = None
    while True:
        rows = connection.execute(sa.text(
            "SELECT id, display_name, meme_context, context_status FROM memes "
            "WHERE (CAST(:after_id AS uuid) IS NULL OR id > CAST(:after_id AS uuid)) ORDER BY id LIMIT 500"
        ), {"after_id": str(last_id) if last_id else None}).mappings().all()
        if not rows:
            break
        for row in rows:
            text_input = build_text_input(row["display_name"], row["meme_context"], row["context_status"])
            connection.execute(sa.text(
                "UPDATE memes SET search_metadata_hash = :fingerprint, text_input_status = :status WHERE id = :id"
            ), {"id": row["id"], "fingerprint": text_input.fingerprint, "status": text_input.status})
        last_id = rows[-1]["id"]
    op.execute("UPDATE installation_state SET schema_revision = '0029_name_text_input' WHERE key = 'local'")


def downgrade() -> None:
    """仅支持前向迁移，防止名称保护事实丢失。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
