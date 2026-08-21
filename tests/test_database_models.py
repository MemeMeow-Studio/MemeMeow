"""不依赖外部数据库的 ORM 约束和 migration 入口检查。"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.database import AgentCallbackRequest, AgentCallbackRequestRepository, DatabaseError, ScopeContext


def test_single_forward_migration_head():
    """仓库只暴露一个前向 revision head，回滚由 migration 明确拒绝。"""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0015_bind_agent_callback_request_ids"]
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


def test_callback_binding_migration_is_forward_only_and_fail_closed_on_history_conflicts():
    """callback 迁移先检查缺失/重复事实，再安装逻辑唯一索引。"""
    migration = Path("alembic/versions/0015_bind_agent_callback_request_ids.py").read_text(encoding="utf-8")
    assert 'down_revision = "0014_scope_aware_opencode_workspace"' in migration
    assert "incomplete_count" in migration
    assert "duplicate_count" in migration
    assert "历史重复逻辑绑定" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_callback_requests_logical" in migration
    assert "raise RuntimeError" in migration


def test_callback_model_keeps_request_id_and_logic_unique_facts():
    """ORM callback 表同时保留旧 request ID 主键和新的复合逻辑唯一约束。"""
    table = AgentCallbackRequest.__table__
    assert {column.name for column in table.primary_key.columns} == {"scope_id", "request_id"}
    assert any(constraint.name == "uq_agent_callback_requests_logical" for constraint in table.constraints)


def test_callback_repository_fails_closed_without_postgres_schema():
    """callback repository 不回退到非 PostgreSQL 或 request-ID-only 事实层。"""
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        repository = AgentCallbackRequestRepository(session, ScopeContext("local"))
        with pytest.raises(DatabaseError, match="callback_binding_schema_unavailable"):
            repository.ensure_schema_ready()
