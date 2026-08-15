"""MemeMeow 应用级数据库初始化入口。

Alembic 负责 schema；本命令只验证当前 revision 并幂等创建开源版 ``local`` scope
和安装标记，不扫描图片、旧 sidecar、旧任务或旧搜索缓存。
"""

from __future__ import annotations

import argparse
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from backend.config import Settings
from backend.database import DatabaseError, create_engine_for_settings, initialize_local


def current_revision(engine) -> str | None:
    """读取 Alembic 当前 revision。"""
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()


def expected_revision() -> str:
    """读取仓库迁移头，防止初始化命令接受过期 schema。"""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    if len(heads) != 1:
        raise DatabaseError("schema_heads_invalid")
    return heads[0]


def main() -> int:
    """执行连接、revision、pgvector 和 local scope 幂等初始化。"""
    parser = argparse.ArgumentParser(description="初始化 MemeMeow PostgreSQL local scope")
    parser.parse_args()
    settings = Settings.from_env()
    engine = create_engine_for_settings(settings)
    revision = expected_revision()
    actual = current_revision(engine)
    if actual != revision:
        print(f"schema_revision_mismatch: expected={revision} actual={actual}", file=sys.stderr)
        return 2
    with engine.connect() as connection:
        if not connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='vector'")).scalar():
            print("pgvector_missing", file=sys.stderr)
            return 2
    initialize_local(engine, revision=revision)
    print(f"initialized scope=local revision={revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
