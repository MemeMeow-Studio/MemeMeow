"""不依赖外部数据库的 ORM 约束和 migration 入口检查。"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_single_forward_migration_head():
    """仓库只暴露一个首版 revision，回滚由 migration 明确拒绝。"""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0003_flat_meme_storage"]
    assert (Path("alembic/versions/0001_postgres_scoped.py")).is_file()
