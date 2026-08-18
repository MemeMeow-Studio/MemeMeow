"""不依赖外部数据库的 ORM 约束和 migration 入口检查。"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_single_forward_migration_head():
    """仓库只暴露一个前向 revision head，回滚由 migration 明确拒绝。"""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0012_image_processing_options_auto_rename"]
    assert (Path("alembic/versions/0001_postgres_scoped.py")).is_file()


def test_image_processing_migration_is_chained_and_rebuilds_legacy_checks():
    """0012 必须从 0011 升级，并显式重建旧三阶段约束。"""
    migration = Path("alembic/versions/0012_image_processing_options_auto_rename.py").read_text(encoding="utf-8")
    assert 'down_revision = "0011_harden_operation_grant_association"' in migration
    assert "ADD COLUMN IF NOT EXISTS auto_name BOOLEAN NOT NULL DEFAULT FALSE" in migration
    assert "DROP CONSTRAINT IF EXISTS ck_task_image_stage" in migration
    assert "DROP CONSTRAINT IF EXISTS ck_task_image_stage_type" in migration
    assert "DROP CONSTRAINT IF EXISTS ck_image_processing_stage_name" in migration
    assert "DROP CONSTRAINT IF EXISTS ck_image_processing_stage_status" in migration
    assert "image_auto_rename" in migration
    assert "'skipped','warning'" in migration


def test_startup_compatibility_ddl_closes_nullable_auto_name():
    """启动兼容路径必须把旧可空列收束为安全的非空默认值。"""
    database = Path("backend/database.py").read_text(encoding="utf-8")
    assert "UPDATE image_processing_jobs SET auto_name = FALSE WHERE auto_name IS NULL" in database
    assert "ALTER TABLE image_processing_jobs ALTER COLUMN auto_name SET NOT NULL" in database
