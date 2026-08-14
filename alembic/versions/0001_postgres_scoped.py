"""固定 MemeMeow 首版 PostgreSQL schema，不依赖未来 ORM metadata。"""

from alembic import op

revision = "0001_postgres_scoped"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """安装 pgvector 并创建初始 scope、Meme、检索和任务表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE scopes (
            id VARCHAR(128) PRIMARY KEY,
            storage_namespace UUID NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE installation_state (
            key VARCHAR(64) PRIMARY KEY,
            schema_revision VARCHAR(128) NOT NULL,
            initialized_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE memes (
            id UUID PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            storage_key VARCHAR(1024) NOT NULL,
            extension VARCHAR(16) NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            metadata_schema_version INTEGER NOT NULL,
            context_status VARCHAR(32) NOT NULL,
            meme_context JSONB NOT NULL,
            provenance JSONB NOT NULL,
            extensions JSONB NOT NULL,
            revision BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_memes_scope_id UNIQUE(scope_id, id),
            CONSTRAINT uq_memes_scope_storage UNIQUE(scope_id, storage_key),
            CONSTRAINT ck_memes_size_nonnegative CHECK(size_bytes >= 0),
            CONSTRAINT ck_memes_context_status CHECK(context_status IN ('pending','partial','ready','repair_required'))
        );
        CREATE TABLE storage_operations (
            id UUID PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            meme_id UUID,
            operation_type VARCHAR(32) NOT NULL,
            operation_token UUID NOT NULL UNIQUE,
            source_key VARCHAR(1024),
            target_key VARCHAR(1024),
            staging_key VARCHAR(1024),
            before_sha256 VARCHAR(64),
            after_sha256 VARCHAR(64),
            before_size BIGINT,
            after_size BIGINT,
            status VARCHAR(32) NOT NULL,
            error JSONB,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_storage_operation_meme FOREIGN KEY(scope_id, meme_id) REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_storage_operation_token UNIQUE(scope_id, meme_id, operation_token),
            CONSTRAINT ck_storage_operation_type CHECK(operation_type IN ('upload','rename','delete')),
            CONSTRAINT ck_storage_operation_status CHECK(status IN ('prepared','file_applied','completed','compensated','blocked'))
        );
        CREATE TABLE search_generations (
            id UUID PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL,
            model VARCHAR(255) NOT NULL,
            dimensions INTEGER NOT NULL,
            status VARCHAR(32) NOT NULL,
            source_snapshot_hash VARCHAR(64) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            activated_at TIMESTAMPTZ,
            CONSTRAINT fk_search_generation_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT uq_generations_scope_id UNIQUE(scope_id, id),
            CONSTRAINT uq_generations_scope_model_id UNIQUE(scope_id, model, id),
            CONSTRAINT ck_generation_dimensions CHECK(dimensions = 1024),
            CONSTRAINT ck_generation_status CHECK(status IN ('building','ready','active','failed','retired'))
        );
        CREATE TABLE search_heads (
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            model VARCHAR(255) NOT NULL,
            active_generation_id UUID,
            active_generation_model VARCHAR(255),
            PRIMARY KEY(scope_id, model),
            CONSTRAINT fk_search_head_generation FOREIGN KEY(scope_id, active_generation_model, active_generation_id) REFERENCES search_generations(scope_id, model, id)
        );
        CREATE TABLE meme_embeddings (
            scope_id VARCHAR(128) NOT NULL,
            generation_id UUID NOT NULL,
            meme_id UUID NOT NULL,
            embedding vector(1024),
            semantic_document VARCHAR(6000) NOT NULL,
            semantic_document_hash VARCHAR(64) NOT NULL,
            metadata_hash VARCHAR(64) NOT NULL,
            image_sha256 VARCHAR(64) NOT NULL,
            meme_revision BIGINT NOT NULL,
            item_status VARCHAR(32) NOT NULL,
            PRIMARY KEY(scope_id, generation_id, meme_id),
            CONSTRAINT fk_embedding_scope FOREIGN KEY(scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_embedding_generation FOREIGN KEY(scope_id, generation_id) REFERENCES search_generations(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_embedding_meme FOREIGN KEY(scope_id, meme_id) REFERENCES memes(scope_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_embedding_item_status CHECK(item_status IN ('pending','ready','failed'))
        );
        CREATE TABLE tasks (
            id VARCHAR(255) PRIMARY KEY,
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            task_type VARCHAR(128) NOT NULL,
            lane VARCHAR(64) NOT NULL,
            payload JSONB NOT NULL,
            dedupe_key VARCHAR(1024),
            status VARCHAR(32) NOT NULL,
            progress DOUBLE PRECISION,
            message VARCHAR(500),
            result JSONB,
            error JSONB,
            settings_version VARCHAR(128),
            lease_owner VARCHAR(255),
            lease_expires_at TIMESTAMPTZ,
            claim_generation BIGINT NOT NULL,
            attempt_count INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            available_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT ck_task_status CHECK(status IN ('queued','running','succeeded','failed')),
            CONSTRAINT ck_task_attempts CHECK(attempt_count >= 0 AND max_attempts > 0),
            CONSTRAINT uq_task_scope_id UNIQUE(scope_id, id)
        );
        CREATE TABLE task_batches (
            scope_id VARCHAR(128) NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
            batch_id VARCHAR(255) NOT NULL,
            finalizer_state VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            finalized_at TIMESTAMPTZ,
            PRIMARY KEY(scope_id, batch_id),
            CONSTRAINT ck_batch_finalizer_state CHECK(finalizer_state IN ('pending','submitted','complete'))
        );
        CREATE TABLE task_batch_items (
            scope_id VARCHAR(128) NOT NULL,
            batch_id VARCHAR(255) NOT NULL,
            task_id VARCHAR(255) NOT NULL,
            PRIMARY KEY(scope_id, batch_id, task_id),
            CONSTRAINT fk_batch_item_batch FOREIGN KEY(scope_id, batch_id) REFERENCES task_batches(scope_id, batch_id) ON DELETE CASCADE,
            CONSTRAINT fk_batch_item_task FOREIGN KEY(scope_id, task_id) REFERENCES tasks(scope_id, id) ON DELETE CASCADE
        );
        CREATE TABLE task_lane_slots (
            lane VARCHAR(64) NOT NULL,
            slot_number INTEGER NOT NULL,
            task_scope_id VARCHAR(128),
            task_id VARCHAR(255),
            lease_owner VARCHAR(255),
            lease_expires_at TIMESTAMPTZ,
            PRIMARY KEY(lane, slot_number),
            CONSTRAINT fk_lane_slot_scope FOREIGN KEY(task_scope_id) REFERENCES scopes(id) ON DELETE CASCADE,
            CONSTRAINT fk_lane_slot_task FOREIGN KEY(task_scope_id, task_id) REFERENCES tasks(scope_id, id),
            CONSTRAINT uq_task_lane_slot_task UNIQUE(task_scope_id, task_id),
            CONSTRAINT ck_slot_number CHECK(slot_number >= 0)
        );
        CREATE INDEX ix_embeddings_generation_status ON meme_embeddings(scope_id, generation_id, item_status);
        CREATE UNIQUE INDEX uq_search_generation_building ON search_generations(scope_id, model) WHERE status = 'building';
        CREATE INDEX ix_storage_operations_recovery ON storage_operations(scope_id, status, updated_at);
        CREATE UNIQUE INDEX uq_storage_operation_active ON storage_operations(scope_id, meme_id) WHERE status IN ('prepared','file_applied') AND meme_id IS NOT NULL;
        CREATE INDEX ix_tasks_active_dedupe ON tasks(scope_id, task_type, dedupe_key) WHERE status IN ('queued','running') AND dedupe_key IS NOT NULL;
        CREATE INDEX ix_tasks_claimable ON tasks(scope_id, status, available_at, lease_expires_at);
        INSERT INTO scopes(id, storage_namespace, created_at) VALUES ('local', gen_random_uuid(), now());
        INSERT INTO installation_state(key, schema_revision, initialized_at) VALUES ('local', '0001_postgres_scoped', now());
        """
    )


def downgrade() -> None:
    """拒绝回滚，避免破坏唯一 PostgreSQL 业务存储。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
