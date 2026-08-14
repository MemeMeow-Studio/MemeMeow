"""拒绝非扁平业务 key，并为 Meme storage_key 安装数据库最终约束。"""

from alembic import op

revision = "0003_flat_meme_storage"
down_revision = "0002_batch_sealed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """在 DDL 前只读扫描业务记录和活动操作，确认后添加扁平约束。"""
    op.execute(
        """
        DO $$
        DECLARE bad_count integer;
        BEGIN
            SELECT count(*) INTO bad_count
            FROM memes
            WHERE storage_key = ''
               OR storage_key IN ('.', '..')
               OR position('/' in storage_key) > 0
               OR position(chr(92) in storage_key) > 0
               OR storage_key ~ '[[:cntrl:]]'
               OR storage_key IN ('.staging', '.quarantine');
            SELECT bad_count + count(*) INTO bad_count
            FROM storage_operations
            WHERE status IN ('prepared', 'file_applied')
              AND (
                (operation_type = 'upload' AND target_key IS NOT NULL)
                OR (operation_type = 'rename' AND (source_key IS NOT NULL OR target_key IS NOT NULL))
                OR (operation_type = 'delete' AND source_key IS NOT NULL)
              )
              AND (
                (operation_type = 'upload' AND target_key !~ '^\\.staging/' AND target_key !~ '^\\.quarantine/' AND (position('/' in target_key) > 0 OR position(chr(92) in target_key) > 0 OR target_key IN ('.', '..')))
                OR (operation_type = 'rename' AND ((source_key IS NOT NULL AND source_key !~ '^\\.staging/' AND source_key !~ '^\\.quarantine/' AND (position('/' in source_key) > 0 OR position(chr(92) in source_key) > 0 OR source_key IN ('.', '..'))) OR (target_key IS NOT NULL AND target_key !~ '^\\.staging/' AND target_key !~ '^\\.quarantine/' AND (position('/' in target_key) > 0 OR position(chr(92) in target_key) > 0 OR target_key IN ('.', '..')))))
                OR (operation_type = 'delete' AND source_key !~ '^\\.staging/' AND source_key !~ '^\\.quarantine/' AND (position('/' in source_key) > 0 OR position(chr(92) in source_key) > 0 OR source_key IN ('.', '..')))
              );
            IF bad_count > 0 THEN
                RAISE EXCEPTION 'flat_meme_storage_preflight_failed: % invalid business keys', bad_count USING ERRCODE = 'check_violation';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE memes ADD CONSTRAINT ck_memes_storage_key_flat
        CHECK (
            storage_key <> ''
            AND storage_key NOT IN ('.', '..', '.staging', '.quarantine')
            AND position('/' in storage_key) = 0
            AND position(chr(92) in storage_key) = 0
            AND storage_key !~ '[[:cntrl:]]'
        )
        """
    )
    op.execute("UPDATE installation_state SET schema_revision = '0003_flat_meme_storage' WHERE key = 'local'")


def downgrade() -> None:
    """拒绝回滚，保持 schema 只前向升级。"""
    raise RuntimeError("本项目 schema 只允许前向升级")
