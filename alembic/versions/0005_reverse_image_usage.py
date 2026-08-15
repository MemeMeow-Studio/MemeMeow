"""创建按 scope 绑定的反向图片检索用量事件表。"""

from alembic import op


revision = "0005_reverse_image_usage"
down_revision = "0004_meme_collections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只向前安装幂等用量流水及统计索引。"""
    op.execute(
        """
        CREATE TABLE reverse_image_usage_events (
            id UUID PRIMARY KEY,
            request_id VARCHAR(128) NOT NULL UNIQUE,
            scope_id VARCHAR(128) NOT NULL,
            task_id VARCHAR(255) NOT NULL,
            meme_id UUID,
            cache_key VARCHAR(128) NOT NULL,
            cache_status VARCHAR(16) NOT NULL,
            provider_called BOOLEAN NOT NULL DEFAULT FALSE,
            provider VARCHAR(64),
            outcome VARCHAR(32) NOT NULL DEFAULT 'started',
            retryable BOOLEAN NOT NULL DEFAULT FALSE,
            result JSONB,
            error JSONB,
            created_at TIMESTAMPTZ NOT NULL,
            provider_started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_reverse_usage_task FOREIGN KEY(scope_id, task_id)
                REFERENCES tasks(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_reverse_usage_meme FOREIGN KEY(scope_id, meme_id)
                REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_reverse_usage_cache_status CHECK(cache_status IN ('hit','miss','refresh')),
            CONSTRAINT ck_reverse_usage_outcome CHECK(outcome IN ('started','success','empty','failed','forbidden')),
            CONSTRAINT ck_reverse_usage_provider_started CHECK(provider_called = false OR provider_started_at IS NOT NULL)
        );
        CREATE INDEX ix_reverse_usage_scope_created
            ON reverse_image_usage_events(scope_id, created_at);
        CREATE INDEX ix_reverse_usage_scope_task
            ON reverse_image_usage_events(scope_id, task_id, created_at);
        UPDATE installation_state
           SET schema_revision = '0005_reverse_image_usage'
         WHERE key = 'local';
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免丢失供应商调用审计。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
