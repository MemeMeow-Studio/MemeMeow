# 使用真实 SQLite 连接验证 Executor 的主 session 金额读取及错误分类。

from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile

import pytest

from executor.analysis_usage import AnalysisUsageError, read_analysis_usage


@pytest.fixture
def database():
    """在项目测试目录创建独立 SQLite 数据，结束时释放连接和文件。"""
    root = Path(".pytest_cache/analysis_usage")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        path = Path(directory) / "opencode.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, cost REAL)")
        connection.executemany(
            "INSERT INTO session VALUES (?, ?, ?, ?)",
            [("main", None, "/workspace", 0.23), ("child", "main", "/workspace", 9),
             ("other", None, "/other", 8)],
        )
        connection.commit()
        connection.close()
        yield path


def test_bound_main_session_only(database):
    """读取指定主 session，忽略子 session 和其他目录的用量。"""
    usage = read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
    assert usage.observed_cost == Decimal("0.23")
    assert usage.checked_at.endswith("+00:00")


@pytest.mark.parametrize("session,reason", [
    ("missing", "session_missing"), ("child", "session_binding_mismatch"),
    ("other", "session_binding_mismatch"), ("", "session_binding_missing"),
])
def test_binding_errors(database, session, reason):
    """缺失绑定、子 session 和其他目录必须返回明确原因。"""
    with pytest.raises(AnalysisUsageError) as error:
        read_analysis_usage(database, session_id=session, directory=Path("/workspace"))
    assert error.value.code == "agent_analysis_usage_unavailable"
    assert error.value.reason == reason


def test_cost_cannot_decrease_on_resume(database):
    """恢复时金额减少不能被接受为新的用量起点。"""
    with pytest.raises(AnalysisUsageError, match="session_cost_decreased"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"), minimum_cost=Decimal("0.24"))


@pytest.mark.parametrize("value", [-1, float("inf"), "invalid", None])
def test_invalid_cost(database, value):
    """实际写入非法 SQLite 值，验证读取端不会将其当作零金额。"""
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE session SET cost = ? WHERE id = 'main'", (value,))
    with pytest.raises(AnalysisUsageError, match="session_cost_invalid"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"))


def test_missing_database_does_not_create_file(database):
    """只读打开失败不得创建空数据库。"""
    missing = database.with_name("missing.db")
    with pytest.raises(AnalysisUsageError, match="database_missing"):
        read_analysis_usage(missing, session_id="main", directory=Path("/workspace"))
    assert not missing.exists()


def test_schema_incompatible(database):
    """缺失控制字段必须识别为 schema 不兼容。"""
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE session DROP COLUMN cost")
    with pytest.raises(AnalysisUsageError, match="session_schema_incompatible"):
        read_analysis_usage(database, session_id="main", directory=Path("/workspace"))
